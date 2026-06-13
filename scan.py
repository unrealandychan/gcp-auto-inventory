# -*- coding: utf-8 -*-
"""GCP Auto Inventory - Scan GCP resources across projects and regions.

This tool uses the GCP Discovery API (googleapiclient.discovery) to call
any GCP REST API, similar to how aws-auto-inventory uses boto3.

Usage:
    python scan.py -s scan/sample/compute.json -p my-project-id
    python scan.py -s scan/sample/all_services.json --all-projects
"""

import argparse
import concurrent.futures
import json
import logging
import os
import time
import traceback
from datetime import datetime
from typing import Any, Optional

import requests
from google.auth import default
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import Request
from googleapiclient import discovery
from googleapiclient.errors import HttpError

timestamp = datetime.now().isoformat(timespec="minutes").replace(":", "-")


class DateTimeEncoder(json.JSONEncoder):
    """Custom JSONEncoder that handles datetime objects."""

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


def get_json_from_url(url: str) -> Optional[list]:
    """Fetch a JSON scan file from a URL.

    Args:
        url: The URL to fetch the JSON file from.

    Returns:
        The parsed JSON data, or None if the fetch failed.
    """
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch JSON from {url}: {e}")
        return None
    except ValueError as e:
        print(f"Failed to parse JSON from {url}: {e}")
        return None


def setup_logging(log_dir: str, log_level: str) -> logging.Logger:
    """Set up file and console logging.

    Args:
        log_dir: Directory to write the log file.
        log_level: Logging level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).

    Returns:
        Configured logger instance.
    """
    os.makedirs(log_dir, exist_ok=True)
    log_filename = f"gcp_resources_{timestamp}.log"
    log_file = os.path.join(log_dir, log_filename)

    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)
    handler = logging.FileHandler(log_file)
    handler.setLevel(log_level)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logging.basicConfig(level=log_level)
    return logging.getLogger(__name__)


def display_time(seconds: float) -> str:
    """Format seconds into a human-readable duration string.

    Args:
        seconds: Duration in seconds.

    Returns:
        Formatted string like '0h:2m:34s'.
    """
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{int(hours)}h:{int(minutes)}m:{int(secs)}s"


def build_gcp_client(service_config: dict, credentials: Any) -> Optional[Any]:
    """Build a GCP Discovery API client for a given service config.

    Each service entry in the scan file must contain:
        api: str           — GCP API name, e.g. "compute"
        version: str       — API version, e.g. "v1"

    Args:
        service_config: A single scan-file service entry.
        credentials: Google OAuth2 credentials.

    Returns:
        A googleapiclient Resource object, or None on failure.
    """
    api = service_config["api"]
    version = service_config["version"]
    try:
        return discovery.build(api, version, credentials=credentials, cache_discovery=False)
    except Exception:  # noqa: BLE001
        return None


def _resolve_resource(client: Any, resource_path: str) -> Optional[Any]:
    """Walk a dot-separated resource path on a Discovery client.

    For example, resource_path="instances.list" on a compute client
    resolves client.instances().list (as a bound method).

    Args:
        client: The root googleapiclient Resource.
        resource_path: Dot-separated path, e.g. "instances" or "subnetworks".

    Returns:
        The resolved Resource object, or None if the path is invalid.
    """
    obj = client
    for part in resource_path.split("."):
        if not hasattr(obj, part):
            return None
        attr = getattr(obj, part)
        # Resource collections are callables that return a Resource
        obj = attr() if callable(attr) else attr
    return obj


def _call_with_retry(
    resource: Any,
    method: str,
    parameters: dict,
    max_retries: int,
    retry_delay: int,
) -> Optional[Any]:
    """Call a GCP API method with exponential-backoff retry.

    Retries on HTTP 429 (rate limit) and 5xx server errors.

    Args:
        resource: The resolved googleapiclient Resource object.
        method: The method name to call, e.g. "list" or "aggregatedList".
        parameters: Keyword arguments to pass to the method.
        max_retries: Maximum number of attempts.
        retry_delay: Base delay in seconds; delay = retry_delay ** attempt.

    Returns:
        The deserialized API response dict, or None if all retries failed.
    """
    for attempt in range(max_retries):
        try:
            call = getattr(resource, method)(**parameters)
            return call.execute()
        except HttpError as exc:
            status = exc.resp.status
            if status in (429, 500, 503):
                if attempt < max_retries - 1:
                    time.sleep(retry_delay ** attempt)
                continue
            # Non-retryable HTTP error (e.g. 403 permission denied)
            raise
        except Exception:  # noqa: BLE001
            if attempt < max_retries - 1:
                time.sleep(retry_delay ** attempt)
            continue
    return None


def _get_service_data(
    credentials: Any,
    project_id: str,
    service: dict,
    log: logging.Logger,
    max_retries: int,
    retry_delay: int,
) -> Optional[dict]:
    """Fetch data for a single GCP service/method in a project.

    Each scan-file entry may contain:
        api: str             — GCP API name (e.g. "compute")
        version: str         — API version (e.g. "v1")
        resource: str        — Dot-separated resource path (e.g. "instances")
        method: str          — Method to call (e.g. "aggregatedList")
        parameters: dict     — Extra kwargs; "project" is auto-injected.
        result_key: str      — Top-level key to extract from the response.

    Args:
        credentials: Google OAuth2 credentials.
        project_id: GCP project ID to scan.
        service: Single service entry from the scan file.
        log: Logger instance.
        max_retries: Maximum retries per API call.
        retry_delay: Base retry delay in seconds.

    Returns:
        Dict with keys {project, api, resource, method, result}, or None on error.
    """
    api = service["api"]
    version = service["version"]
    resource_path = service["resource"]
    method = service["method"]
    result_key = service.get("result_key", None)
    parameters = dict(service.get("parameters", {}))

    # Inject project unless explicitly overridden
    if "project" not in parameters:
        parameters["project"] = project_id

    log.info(
        "Scanning project=%s api=%s resource=%s method=%s",
        project_id, api, resource_path, method,
    )

    try:
        client = build_gcp_client(service, credentials)
        if client is None:
            log.error("Could not build client for api=%s version=%s", api, version)
            return None

        resource = _resolve_resource(client, resource_path)
        if resource is None:
            log.error(
                "Resource path '%s' not found on api=%s version=%s",
                resource_path, api, version,
            )
            return None

        response = _call_with_retry(resource, method, parameters, max_retries, retry_delay)

        if response is None:
            log.info("No data returned for api=%s resource=%s method=%s", api, resource_path, method)
            return None

        if result_key:
            response = response.get(result_key, response)

    except HttpError as exc:
        log.error(
            "HttpError for api=%s resource=%s method=%s project=%s: %s",
            api, resource_path, method, project_id, exc,
        )
        log.debug(traceback.format_exc())
        return None
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Unexpected error for api=%s resource=%s method=%s project=%s: %s",
            api, resource_path, method, project_id, type(exc).__name__,
        )
        log.error(traceback.format_exc())
        return None

    log.info("Finished: api=%s resource=%s method=%s project=%s", api, resource_path, method, project_id)
    return {
        "project": project_id,
        "api": api,
        "resource": resource_path,
        "method": method,
        "result": response,
    }


def process_project(
    project_id: str,
    services: list,
    credentials: Any,
    log: logging.Logger,
    max_retries: int,
    retry_delay: int,
    concurrent_services: Optional[int],
) -> list:
    """Scan all services in a single GCP project concurrently.

    Args:
        project_id: GCP project ID.
        services: List of service entries from the scan file.
        credentials: Google OAuth2 credentials.
        log: Logger instance.
        max_retries: Max retries per API call.
        retry_delay: Base retry delay in seconds.
        concurrent_services: Thread pool size; None means unbounded.

    Returns:
        List of result dicts (one per successful service call).
    """
    log.info("Started scanning project: %s", project_id)
    project_results = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_services) as executor:
        future_to_service = {
            executor.submit(
                _get_service_data,
                credentials,
                project_id,
                service,
                log,
                max_retries,
                retry_delay,
            ): service
            for service in services
        }
        for future in concurrent.futures.as_completed(future_to_service):
            service = future_to_service[future]
            try:
                result = future.result()
                if result is not None and result["result"]:
                    project_results.append(result)
                    log.info(
                        "Scanned: api=%s resource=%s method=%s",
                        service["api"], service["resource"], service["method"],
                    )
                else:
                    log.info(
                        "No data: api=%s resource=%s method=%s",
                        service["api"], service["resource"], service["method"],
                    )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Exception for service=%s: %s",
                    service.get("api", "?"), exc,
                )
                log.error(traceback.format_exc())

    log.info("Finished scanning project: %s", project_id)
    return project_results


def check_gcp_credentials() -> Optional[Any]:
    """Verify ADC credentials and print the authenticated identity.

    Uses Application Default Credentials (ADC). Set credentials via:
        gcloud auth application-default login
    or by setting GOOGLE_APPLICATION_CREDENTIALS to a service account key.

    Returns:
        Refreshed credentials object, or None if authentication failed.
    """
    try:
        credentials, project = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(Request())
        print(f"Authenticated. Default project: {project or '(not set)'}")
        return credentials
    except DefaultCredentialsError as exc:
        print(f"GCP credential error: {exc}")
        print("Run: gcloud auth application-default login")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected auth error: {exc}")
        return None


def main(
    scan: str,
    projects: Optional[list],
    output_dir: str,
    log_level: str,
    max_retries: int,
    retry_delay: int,
    concurrent_projects: Optional[int],
    concurrent_services: Optional[int],
    credentials: Any = None,
) -> None:
    """Run the GCP resource inventory scan.

    Args:
        scan: Path or URL to the JSON scan file.
        projects: List of GCP project IDs to scan; if None, uses ADC default.
        output_dir: Directory to write results and logs.
        log_level: Logging level string.
        max_retries: Max retries per API call.
        retry_delay: Base retry delay in seconds.
        concurrent_projects: Max concurrent project threads.
        concurrent_services: Max concurrent service threads per project.
        credentials: Pre-built credentials; resolved via ADC if None.
    """
    if credentials is None:
        credentials = check_gcp_credentials()
    if credentials is None:
        print("Invalid GCP credentials. Please configure ADC.")
        return

    log = setup_logging(output_dir, log_level)

    if scan.startswith("http://") or scan.startswith("https://"):
        services = get_json_from_url(scan)
        if services is None:
            print(f"Failed to load scan file from {scan}. Exiting.")
            return
    else:
        with open(scan, "r") as f:
            services = json.load(f)

    if not projects:
        _, project = default()
        if project:
            projects = [project]
        else:
            print(
                "No projects specified and no default project found.\n"
                "Use -p <project-id> or set a default: gcloud config set project <id>"
            )
            return

    print(f"Scanning {len(projects)} project(s): {', '.join(projects)}")
    start_time = time.time()

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_projects) as executor:
        future_to_project = {
            executor.submit(
                process_project,
                project_id,
                services,
                credentials,
                log,
                max_retries,
                retry_delay,
                concurrent_services,
            ): project_id
            for project_id in projects
        }
        for future in concurrent.futures.as_completed(future_to_project):
            project_id = future_to_project[future]
            try:
                project_results = future.result()
                results.extend(project_results)
                for service_result in project_results:
                    directory = os.path.join(
                        output_dir, timestamp, service_result["project"]
                    )
                    os.makedirs(directory, exist_ok=True)
                    filename = (
                        f"{service_result['api']}"
                        f"-{service_result['resource'].replace('.', '_')}"
                        f"-{service_result['method']}.json"
                    )
                    with open(os.path.join(directory, filename), "w") as f:
                        json.dump(service_result["result"], f, cls=DateTimeEncoder, indent=2)
            except Exception as exc:  # noqa: BLE001
                log.error("Project %r raised: %s", project_id, exc)
                log.error(traceback.format_exc())

    elapsed = time.time() - start_time
    print(f"Total elapsed time: {display_time(elapsed)}")
    print(f"Results stored in: {output_dir}/{timestamp}/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scan GCP resources across projects and write results to JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scan.py -s scan/sample/compute.json -p my-project-id
  python scan.py -s scan/sample/all_services.json -p proj-a proj-b
  python scan.py -s scan/sample/all_services.json --all-projects
  python scan.py -s https://example.com/scan.json -p my-project
        """,
    )
    parser.add_argument(
        "-s", "--scan",
        required=True,
        help="Path or URL to the JSON scan file defining which GCP APIs to call.",
    )
    parser.add_argument(
        "-p", "--projects",
        nargs="+",
        help="GCP project IDs to scan. Defaults to the ADC default project.",
    )
    parser.add_argument(
        "--all-projects",
        action="store_true",
        help="Scan all accessible projects in the GCP Organization.",
    )
    parser.add_argument(
        "-o", "--output_dir",
        default="output",
        help="Directory to store results and logs (default: output).",
    )
    parser.add_argument(
        "-l", "--log_level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum API call retries with backoff (default: 3).",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=2,
        help="Base retry delay in seconds; actual delay = retry_delay^attempt (default: 2).",
    )
    parser.add_argument(
        "--concurrent-projects",
        type=int,
        default=None,
        help="Number of projects to scan concurrently (default: all at once).",
    )
    parser.add_argument(
        "--concurrent-services",
        type=int,
        default=None,
        help="Number of services to scan concurrently per project (default: all at once).",
    )
    parser.add_argument(
        "--org-id",
        default=None,
        help="GCP Organization ID for --all-projects scan (e.g. 123456789012).",
    )

    args = parser.parse_args()

    if args.all_projects:
        from organization_scanner import scan_organization

        scan_organization(
            org_id=args.org_id,
            scan=args.scan,
            output_dir=args.output_dir,
            log_level=args.log_level,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
            concurrent_projects=args.concurrent_projects,
            concurrent_services=args.concurrent_services,
        )
    else:
        main(
            scan=args.scan,
            projects=args.projects,
            output_dir=args.output_dir,
            log_level=args.log_level,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
            concurrent_projects=args.concurrent_projects,
            concurrent_services=args.concurrent_services,
        )
