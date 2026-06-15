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
from typing import Any

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


def get_json_from_url(url: str) -> list | None:
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


def _substitute_project(params: Any, project_id: str) -> Any:
    """Recursively replace '{project}' placeholders with the actual project ID.

    Args:
        params: Any parameter value (dict, list, str, or other types).
        project_id: The actual GCP project ID.

    Returns:
        The parameter value with '{project}' substituted.
    """
    if isinstance(params, dict):
        return {k: _substitute_project(v, project_id) for k, v in params.items()}
    if isinstance(params, list):
        return [_substitute_project(v, project_id) for v in params]
    if isinstance(params, str):
        return params.replace("{project}", project_id)
    return params


def _has_project_placeholder(params: Any) -> bool:
    """Check if any parameter value contains the '{project}' placeholder.

    Args:
        params: Any parameter value.

    Returns:
        True if the placeholder is found, False otherwise.
    """
    if isinstance(params, dict):
        return any(_has_project_placeholder(v) for v in params.values())
    if isinstance(params, list):
        return any(_has_project_placeholder(v) for v in params)
    if isinstance(params, str):
        return "{project}" in params
    return False


def _is_service_disabled_error(exc: HttpError) -> bool:
    """Check if an HttpError is due to a service being disabled.

    Args:
        exc: The HttpError exception raised by the API call.

    Returns:
        True if the error indicates a disabled service/API, False otherwise.
    """
    try:
        content = json.loads(exc.content.decode("utf-8"))
        error = content.get("error", {})
        message = error.get("message", "")
        if "has not been used in project" in message or "is disabled" in message:
            return True
        for detail in error.get("details", []):
            if detail.get("reason") == "SERVICE_DISABLED":
                return True
    except Exception:
        pass
    return False


def _merge_responses(responses: list[dict], result_key: str | None) -> Any:
    """Merge a list of paginated response dicts.

    Args:
        responses: List of response dicts.
        result_key: Top-level key to extract/merge (e.g., 'items').

    Returns:
        The merged response.
    """
    if not responses:
        return None
    if len(responses) == 1:
        resp = responses[0]
        return resp.get(result_key, resp) if result_key else resp

    if result_key:
        first_val = responses[0].get(result_key)
        if isinstance(first_val, list):
            merged_list = []
            for r in responses:
                merged_list.extend(r.get(result_key) or [])
            return merged_list
        if isinstance(first_val, dict):
            # Dynamic nested merge (e.g. compute aggregatedList)
            merged_dict: dict[str, Any] = {}
            for r in responses:
                val = r.get(result_key) or {}
                for k, v in val.items():
                    if k not in merged_dict:
                        merged_dict[k] = v
                    else:
                        if isinstance(merged_dict[k], dict) and isinstance(v, dict):
                            for sub_k, sub_v in v.items():
                                if sub_k not in merged_dict[k]:
                                    merged_dict[k][sub_k] = sub_v
                                else:
                                    if isinstance(merged_dict[k][sub_k], list) and isinstance(sub_v, list):
                                        merged_dict[k][sub_k].extend(sub_v)
                        else:
                            merged_dict[k] = v
            return merged_dict
        # Fallback if result_key value is not list/dict
        return responses[-1].get(result_key)

    # No result_key: merge top-level keys
    merged: dict[str, Any] = {}
    for r in responses:
        for k, v in r.items():
            if k == "nextPageToken":
                continue
            if k not in merged:
                merged[k] = v
            else:
                if isinstance(merged[k], list) and isinstance(v, list):
                    merged[k].extend(v)
                elif isinstance(merged[k], dict) and isinstance(v, dict):
                    merged[k].update(v)
    return merged


def dry_run_scan(scan_path: str, projects: list | None) -> None:
    """Validate the scan file and print the planned API calls.

    Args:
        scan_path: Path or URL to the JSON scan file.
        projects: List of GCP project IDs to scan.
    """
    print("=== GCP Auto Inventory Dry-Run / Validation Mode ===")

    # 1. Load scan file
    try:
        if scan_path.startswith("http://") or scan_path.startswith("https://"):
            services = get_json_from_url(scan_path)
            if services is None:
                print(f"Error: Failed to load scan file from URL '{scan_path}'")
                return
        else:
            if not os.path.exists(scan_path):
                print(f"Error: Scan file '{scan_path}' does not exist.")
                return
            with open(scan_path) as f:
                services = json.load(f)
    except Exception as exc:
        print(f"Error: Failed to parse scan file: {exc}")
        return

    # 2. Validate scan file structure
    if not isinstance(services, list):
        print("Error: Scan file must contain a JSON array/list of service definitions.")
        return

    errors = []
    validated_services = []
    for idx, service in enumerate(services):
        if not isinstance(service, dict):
            errors.append(f"Entry {idx} is not a JSON object.")
            continue

        missing = [field for field in ["api", "version", "resource", "method"] if field not in service]
        if missing:
            errors.append(f"Entry {idx} is missing required fields: {', '.join(missing)}")
            continue

        validated_services.append(service)

    if errors:
        print(f"Validation failed with {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
        return

    print(f"Scan file is VALID. Found {len(validated_services)} service call definition(s).")

    # 3. Resolve projects
    resolved_projects = projects or ["<adc-default-project>"]
    print(f"Planned to scan against {len(resolved_projects)} project(s): {', '.join(resolved_projects)}")
    print("\nPlanned API calls:")

    for project in resolved_projects:
        print(f"\nProject: {project}")
        for idx, service in enumerate(validated_services):
            api = service["api"]
            version = service["version"]
            res = service["resource"]
            method = service["method"]
            desc = service.get("description", "No description")

            # Show parameter resolution
            orig_params = service.get("parameters", {})
            resolved_params = _substitute_project(orig_params, project)

            if _has_project_placeholder(orig_params):
                param_str = json.dumps(resolved_params)
                print(f"  [{idx + 1:02d}] {api}:{version} -> {res}.{method}()")
                print(f"       Description: {desc}")
                print(f"       Parameters (Substituted): {param_str}")
            else:
                # Flat injection
                inj_key = "projectId" if api == "bigquery" else "project"
                final_params = dict(resolved_params)
                if inj_key not in final_params:
                    final_params[inj_key] = project
                param_str = json.dumps(final_params)
                print(f"  [{idx + 1:02d}] {api}:{version} -> {res}.{method}()")
                print(f"       Description: {desc}")
                print(f"       Parameters (Injected): {param_str}")

    print("\n=== Dry-Run Completed Successfully ===")


def build_gcp_client(service_config: dict, credentials: Any) -> Any | None:
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
    except Exception:
        return None


def _resolve_resource(client: Any, resource_path: str) -> Any | None:
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
) -> Any | None:
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
        except Exception:
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
) -> dict | None:
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
    result_key = service.get("result_key")
    parameters = dict(service.get("parameters", {}))

    # Resolve project parameter name and placeholders
    if _has_project_placeholder(parameters):
        parameters = _substitute_project(parameters, project_id)
    else:
        # No placeholders, inject flat project parameter based on API type
        if api == "bigquery":
            if "projectId" not in parameters:
                parameters["projectId"] = project_id
        else:
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

        # Execute call with pagination
        responses = []
        next_page_token = None

        while True:
            current_parameters = dict(parameters)
            if next_page_token:
                current_parameters["pageToken"] = next_page_token

            response = _call_with_retry(
                resource, method, current_parameters, max_retries, retry_delay
            )

            if response is None:
                break

            responses.append(response)
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break

        if not responses:
            log.info("No data returned for api=%s resource=%s method=%s", api, resource_path, method)
            return None

        merged_result = _merge_responses(responses, result_key)
        if merged_result is None or (isinstance(merged_result, (list, dict)) and not merged_result):
            log.info("No data returned for api=%s resource=%s method=%s (result empty)", api, resource_path, method)
            return None

    except HttpError as exc:
        if _is_service_disabled_error(exc):
            log.warning(
                "API '%s' is disabled in project '%s'. Skipping resource '%s'.",
                api, project_id, resource_path,
            )
        else:
            log.error(
                "HttpError for api=%s resource=%s method=%s project=%s: %s",
                api, resource_path, method, project_id, exc,
            )
            log.debug(traceback.format_exc())
        return None
    except Exception as exc:
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
        "result": merged_result,
    }


def process_project(
    project_id: str,
    services: list,
    credentials: Any,
    log: logging.Logger,
    max_retries: int,
    retry_delay: int,
    concurrent_services: int | None,
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
            except Exception as exc:
                log.error(
                    "Exception for service=%s: %s",
                    service.get("api", "?"), exc,
                )
                log.error(traceback.format_exc())

    log.info("Finished scanning project: %s", project_id)
    return project_results


def check_gcp_credentials() -> Any | None:
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
    except Exception as exc:
        print(f"Unexpected auth error: {exc}")
        return None


def main(
    scan: str,
    projects: list | None,
    output_dir: str,
    log_level: str,
    max_retries: int,
    retry_delay: int,
    concurrent_projects: int | None,
    concurrent_services: int | None,
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
        with open(scan) as f:
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
            except Exception as exc:
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the scan file and print the planned API calls without executing them.",
    )

    args = parser.parse_args()

    if args.dry_run:
        projects_to_show = args.projects
        if args.all_projects:
            projects_to_show = ["<all-active-org-projects>"]
        dry_run_scan(args.scan, projects_to_show)
    elif args.all_projects:
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
