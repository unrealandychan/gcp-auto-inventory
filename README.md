# GCP Auto Inventory

A command-line tool that scans GCP resources across projects and writes the results to JSON files.

Inspired by [aws-auto-inventory](https://github.com/aws-samples/aws-auto-inventory), adapted for Google Cloud Platform using the GCP Discovery API.

---

## Overview

GCP Auto Inventory builds a resource inventory by calling GCP REST API operations that you define in a scan file, then saving each response as JSON. You control which services, API methods, parameters, and projects are scanned.

The tool runs projects and services **concurrently** and retries rate-limited or transient API calls with **exponential backoff**.

Use it to collect a point-in-time snapshot of GCP resources for auditing, reporting, or migration planning.

Authentication uses **Application Default Credentials (ADC)** — the same mechanism as `gcloud` and the GCP client libraries.

---

## Features

- 📦 **Declarative scan files** — define which GCP APIs to call in plain JSON
- ⚡ **Concurrent scanning** — projects and services run in parallel
- 🔄 **Retry with backoff** — handles rate limits (HTTP 429) and transient errors
- 🏢 **Organization-wide scan** — enumerate all projects in a GCP Organization
- 🔗 **URL scan files** — load scan definitions from a remote URL
- 💾 **Structured JSON output** — one file per project × service × method
- 🔐 **ADC auth** — works with `gcloud auth application-default login`, service account keys, or Workload Identity
- 🔍 **Dry-Run / Validation Mode** — validate scan files structurally and preview API parameter resolutions without GCP credentials
- 📄 **API Pagination Support** — recursively paginates using `nextPageToken` and merges items automatically across all pages
- 🛠️ **Smart Project Param Names** — automatically uses `projectId` for BigQuery or substitutes `{project}` templates inside path parameters, preventing `TypeError` errors on nested resources
- 🛡️ **Graceful Disabled API Handling** — logs warning/info messages instead of verbose tracebacks when scanning projects where some APIs are not enabled

---

## Prerequisites

- Python 3.8+
- `gcloud` CLI configured with ADC, **or** a service account key set via `GOOGLE_APPLICATION_CREDENTIALS`
- IAM roles needed (minimum):
  - `roles/viewer` on each project to scan
  - `roles/resourcemanager.organizationViewer` for `--all-projects` (org scan)

---

## Installation

```bash
git clone https://github.com/unrealandychan/gcp-auto-inventory.git
cd gcp-auto-inventory
pip install -r requirements.txt
```

---

## Authentication

```bash
# Option 1: Interactive login (recommended for local use)
gcloud auth application-default login

# Option 2: Service account key
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json

# Verify
gcloud config set project my-project-id
```

---

## Getting Started

Scan Cloud Storage buckets in a single project:

```bash
python scan.py -s scan/sample/services/storage.json -p my-project-id
```

The tool prints the authenticated identity and stores results:

```
Authenticated. Default project: my-project-id
Scanning 1 project(s): my-project-id
Total elapsed time: 0h:0m:2s
Results stored in: output/2026-06-13T14-00/
```

Output files are written to:

```
output/
└── 2026-06-13T14-00/
    └── my-project-id/
        └── storage-buckets-list.json
```

---

## Usage

```
python scan.py -s SCAN_FILE [-p PROJECT ...] [options]
```

### Command-Line Options

| Flag | Description | Default |
|---|---|---|
| `-s`, `--scan` | Path or URL to the JSON scan file. **Required.** | — |
| `-p`, `--projects` | Space-separated list of GCP project IDs to scan. | ADC default project |
| `--all-projects` | Scan every active project in the GCP Organization. | Off |
| `--org-id` | GCP Organization ID for `--all-projects` (e.g. `123456789`). | — |
| `-o`, `--output_dir` | Directory for results and logs. | `output` |
| `-l`, `--log_level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. | `INFO` |
| `--max-retries` | Maximum retries per API call. | `3` |
| `--retry-delay` | Base delay (seconds) for retry backoff. | `2` |
| `--concurrent-projects` | Number of projects to scan at once. | All at once |
| `--concurrent-services` | Number of services to scan at once per project. | All at once |
| `--dry-run` | Validate the scan file and preview planned API calls without executing. | Off |

---

## Scan File Format

A scan file is a JSON array. Each entry describes one GCP API call:

```json
[
  {
    "api":         "compute",
    "version":     "v1",
    "resource":    "instances",
    "method":      "aggregatedList",
    "parameters":  {},
    "result_key":  "items",
    "description": "List all Compute Engine VM instances"
  }
]
```

| Field | Required | Description |
|---|---|---|
| `api` | ✅ | GCP Discovery API name, e.g. `compute`, `storage`, `container` |
| `version` | ✅ | API version, e.g. `v1`, `v2`, `v1beta1` |
| `resource` | ✅ | Dot-separated resource path, e.g. `instances`, `projects.locations.clusters` |
| `method` | ✅ | Method name, e.g. `list`, `aggregatedList`, `get` |
| `parameters` | ❌ | Extra kwargs for the API call. `project` is auto-injected. |
| `result_key` | ❌ | Top-level key to extract from the response. |
| `description` | ❌ | Human-readable note (ignored at runtime). |

> **Note:** For nested resources (e.g. `projects.locations.clusters`), the tool walks the resource path by calling each segment in turn. `project` is automatically substituted into parameter templates like `"parent": "projects/{project}/locations/-"`.

---

## Examples

### Scan a single project

```bash
python scan.py -s scan/sample/compute.json -p my-project-id
```

### Scan multiple projects

```bash
python scan.py -s scan/sample/all_services.json -p proj-a proj-b proj-c
```

### Scan all projects in an Organization

```bash
python scan.py -s scan/sample/all_services.json \
  --all-projects \
  --org-id 123456789012
```

### Load scan file from URL

```bash
python scan.py -s https://example.com/my-scan.json -p my-project-id
```

### Limit concurrency

```bash
python scan.py -s scan/sample/all_services.json \
  --all-projects \
  --concurrent-projects 5 \
  --concurrent-services 10
```

---

## Sample Scan Files

| File | Description |
|---|---|
| `scan/sample/all_services.json` | 20+ services: Compute, GKE, Cloud Run, BigQuery, Cloud SQL, IAM, KMS, and more |
| `scan/sample/compute.json` | Compute Engine only (VMs, disks, networks, firewalls, LB) |
| `scan/sample/services/storage.json` | Cloud Storage buckets |
| `scan/sample/services/gke.json` | GKE clusters |
| `scan/sample/services/iam.json` | IAM service accounts |

---

## Output Structure

```
output/
└── <timestamp>/                        # ISO timestamp of the run
    └── <project-id>/
        ├── compute-instances-aggregatedList.json
        ├── storage-buckets-list.json
        ├── container-projects_locations_clusters-list.json
        └── ...
```

For organization scans:

```
output/
└── organization-<timestamp>/
    ├── projects.json                   # Summary of discovered projects
    └── <project-id>/
        └── ...
```

---

## GCP API Discovery

GCP Auto Inventory is built on the [Google API Discovery Service](https://developers.google.com/discovery). You can browse all available APIs at:

```
https://discovery.googleapis.com/discovery/v1/apis
```

To find the right `api`, `version`, `resource`, and `method` for a service, use the [APIs Explorer](https://developers.google.com/apis-explorer).

---

## License

MIT — see [LICENSE](LICENSE).
