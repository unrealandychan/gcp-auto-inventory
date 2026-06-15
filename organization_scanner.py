"""GCP Organization Scanner.

Lists all active GCP projects accessible to the authenticated principal
(optionally scoped to an Organization), then runs the inventory scan
against each project using the same logic as scan.py.
"""

import json
import os
from typing import Any

from googleapiclient import discovery
from googleapiclient.errors import HttpError
from scan import check_gcp_credentials, timestamp
from scan import main as scan_main


def list_projects(credentials: Any, org_id: str | None = None) -> list:
    """List all active GCP projects accessible to the caller.

    If org_id is provided, only projects belonging to that Organization
    are returned. Otherwise all accessible projects are listed.

    Args:
        credentials: Google OAuth2 credentials.
        org_id: GCP Organization ID (numeric string, e.g. "123456789012").
                Pass None to list all accessible projects.

    Returns:
        List of project ID strings.
    """
    client = discovery.build("cloudresourcemanager", "v1", credentials=credentials, cache_discovery=False)

    projects = []
    page_token = None

    filter_str = "lifecycleState:ACTIVE"
    if org_id:
        filter_str += f" parent.type:organization parent.id:{org_id}"

    while True:
        request_kwargs: dict = {"filter": filter_str, "pageSize": 500}
        if page_token:
            request_kwargs["pageToken"] = page_token

        try:
            response = client.projects().list(**request_kwargs).execute()
        except HttpError as exc:
            print(f"Error listing projects: {exc}")
            break

        for project in response.get("projects", []):
            if project.get("lifecycleState") == "ACTIVE":
                projects.append(project["projectId"])

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return projects


def scan_organization(
    org_id: str | None,
    scan: str,
    output_dir: str,
    log_level: str,
    max_retries: int,
    retry_delay: int,
    concurrent_projects: int | None,
    concurrent_services: int | None,
) -> None:
    """Scan all active projects in a GCP Organization.

    Discovers all accessible projects (filtered by org_id when provided),
    writes an `projects.json` summary to the output directory, then
    invokes the standard per-project scan logic against each one.

    Args:
        org_id: GCP Organization ID; None means scan all accessible projects.
        scan: Path or URL to the JSON scan file.
        output_dir: Directory to write results and logs.
        log_level: Logging level string.
        max_retries: Max retries per API call.
        retry_delay: Base retry delay in seconds.
        concurrent_projects: Max concurrent project threads.
        concurrent_services: Max concurrent service threads per project.
    """
    credentials = check_gcp_credentials()
    if credentials is None:
        print("Invalid GCP credentials. Aborting organization scan.")
        return

    scope = f"organization {org_id}" if org_id else "all accessible projects"
    print(f"Listing projects in {scope}...")
    projects = list_projects(credentials, org_id=org_id)

    if not projects:
        print("No active projects found. Check permissions and org ID.")
        return

    print(f"Found {len(projects)} active project(s).")

    # Write projects summary
    org_output_dir = os.path.join(output_dir, f"organization-{timestamp}")
    os.makedirs(org_output_dir, exist_ok=True)
    with open(os.path.join(org_output_dir, "projects.json"), "w") as f:
        json.dump({"projects": projects, "count": len(projects)}, f, indent=2)
    print(f"Project list saved to {org_output_dir}/projects.json")

    scan_main(
        scan=scan,
        projects=projects,
        output_dir=org_output_dir,
        log_level=log_level,
        max_retries=max_retries,
        retry_delay=retry_delay,
        concurrent_projects=concurrent_projects,
        concurrent_services=concurrent_services,
        credentials=credentials,
    )
