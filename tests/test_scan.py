"""Tests for GCP Auto Inventory."""

import json
import os
from typing import Any

# We test without needing real GCP credentials by mocking the google libraries
import sys
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

# Mock the google modules before importing scan
google_mock = MagicMock()
google_auth_mock = MagicMock()
google_auth_mock.default.return_value = (MagicMock(), "test-project")

class MockHttpError(Exception):
    def __init__(self, resp, content, uri=None):
        self.resp = resp
        self.content = content
        self.uri = uri
        super().__init__(str(content))

errors_mock = MagicMock()
errors_mock.HttpError = MockHttpError

sys.modules["google"] = google_mock
sys.modules["google.auth"] = google_auth_mock
sys.modules["google.auth.exceptions"] = MagicMock()
sys.modules["google.auth.transport"] = MagicMock()
sys.modules["google.auth.transport.requests"] = MagicMock()
sys.modules["googleapiclient"] = MagicMock()
sys.modules["googleapiclient.discovery"] = MagicMock()
sys.modules["googleapiclient.errors"] = errors_mock

# Now we can import scan safely
import scan as scan_module  # noqa: E402


class TestDateTimeEncoder:
    """Tests for DateTimeEncoder."""

    def test_encodes_datetime(self) -> None:
        dt = datetime(2026, 6, 13, 12, 0, 0)
        result = json.dumps({"ts": dt}, cls=scan_module.DateTimeEncoder)
        assert "2026-06-13" in result

    def test_passes_through_non_datetime(self) -> None:
        result = json.dumps({"key": "value"}, cls=scan_module.DateTimeEncoder)
        assert result == '{"key": "value"}'


class TestDisplayTime:
    """Tests for display_time helper."""

    def test_seconds_only(self) -> None:
        assert scan_module.display_time(45) == "0h:0m:45s"

    def test_minutes_and_seconds(self) -> None:
        assert scan_module.display_time(125) == "0h:2m:5s"

    def test_hours_minutes_seconds(self) -> None:
        assert scan_module.display_time(3661) == "1h:1m:1s"


class TestGetJsonFromUrl:
    """Tests for get_json_from_url."""

    def test_returns_parsed_json_on_success(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = [{"api": "compute"}]
        with patch("scan.requests.get", return_value=mock_response):
            result = scan_module.get_json_from_url("https://example.com/scan.json")
        assert result == [{"api": "compute"}]

    def test_returns_none_on_request_error(self) -> None:
        import requests as req
        with patch("scan.requests.get", side_effect=req.exceptions.ConnectionError("fail")):
            result = scan_module.get_json_from_url("https://bad.example.com/scan.json")
        assert result is None


class TestSetupLogging:
    """Tests for setup_logging."""

    def test_creates_log_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "logs")
            scan_module.setup_logging(log_dir, "INFO")
            assert os.path.isdir(log_dir)

    def test_returns_logger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import logging
            logger = scan_module.setup_logging(tmp, "DEBUG")
            assert isinstance(logger, logging.Logger)


class TestProcessProject:
    """Tests for process_project."""

    def test_empty_services_returns_empty_list(self) -> None:
        import logging
        log = logging.getLogger("test")
        credentials = MagicMock()
        result = scan_module.process_project(
            "my-project", [], credentials, log, 3, 2, None
        )
        assert result == []

    def test_successful_service_appended_to_results(self) -> None:
        import logging
        log = logging.getLogger("test")
        credentials = MagicMock()

        service_entry = {
            "api": "storage",
            "version": "v1",
            "resource": "buckets",
            "method": "list",
        }
        fake_result = {
            "project": "my-project",
            "api": "storage",
            "resource": "buckets",
            "method": "list",
            "result": [{"name": "my-bucket"}],
        }

        with patch("scan._get_service_data", return_value=fake_result):
            result = scan_module.process_project(
                "my-project", [service_entry], credentials, log, 3, 2, 1
            )

        assert len(result) == 1
        assert result[0]["api"] == "storage"

    def test_none_result_excluded(self) -> None:
        import logging
        log = logging.getLogger("test")
        credentials = MagicMock()

        service_entry = {"api": "compute", "version": "v1", "resource": "instances", "method": "list"}

        with patch("scan._get_service_data", return_value=None):
            result = scan_module.process_project(
                "my-project", [service_entry], credentials, log, 3, 2, 1
            )

        assert result == []


class TestMainFunction:
    """Integration-style tests for main()."""

    def test_exits_cleanly_without_credentials(self) -> None:
        with patch("scan.check_gcp_credentials", return_value=None):
            # Should return without raising
            scan_module.main(
                scan="scan/sample/compute.json",
                projects=["test-proj"],
                output_dir="/tmp/test-output",
                log_level="WARNING",
                max_retries=1,
                retry_delay=1,
                concurrent_projects=1,
                concurrent_services=1,
            )

    def test_writes_json_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_file = os.path.join(tmp, "scan.json")
            with open(scan_file, "w") as f:
                json.dump(
                    [{"api": "storage", "version": "v1", "resource": "buckets", "method": "list"}],
                    f,
                )

            fake_result = {
                "project": "my-proj",
                "api": "storage",
                "resource": "buckets",
                "method": "list",
                "result": [{"name": "bucket-1"}],
            }
            credentials = MagicMock()

            with (
                patch("scan.check_gcp_credentials", return_value=credentials),
                patch("scan.process_project", return_value=[fake_result]),
                patch("scan.setup_logging", return_value=MagicMock()),
            ):
                scan_module.main(
                    scan=scan_file,
                    projects=["my-proj"],
                    output_dir=tmp,
                    log_level="WARNING",
                    max_retries=1,
                    retry_delay=1,
                    concurrent_projects=1,
                    concurrent_services=1,
                    credentials=credentials,
                )

            # Check that at least one JSON file was created
            json_files = []
            for root, _, files in os.walk(tmp):
                json_files.extend(f for f in files if f.endswith(".json"))
            assert len(json_files) >= 1


class TestNewFeatures:
    """Tests for placeholder replacement, custom project param names, pagination, and disabled API handling."""

    def test_substitute_project(self) -> None:
        params = {
            "parent": "projects/{project}/locations/-",
            "nested": {
                "name": "projects/{project}/topics/my-topic",
                "list_field": ["{project}", "no-change"]
            },
            "integer": 123
        }
        res = scan_module._substitute_project(params, "my-real-project")
        assert res["parent"] == "projects/my-real-project/locations/-"
        assert res["nested"]["name"] == "projects/my-real-project/topics/my-topic"
        assert res["nested"]["list_field"][0] == "my-real-project"
        assert res["nested"]["list_field"][1] == "no-change"
        assert res["integer"] == 123

    def test_has_project_placeholder(self) -> None:
        assert scan_module._has_project_placeholder({"parent": "projects/{project}"}) is True
        assert scan_module._has_project_placeholder({"nested": {"list": ["{project}"]}}) is True
        assert scan_module._has_project_placeholder({"flat": "project-id"}) is False

    def test_is_service_disabled_error(self) -> None:
        from googleapiclient.errors import HttpError

        # Mock exception 1: contains SERVICE_DISABLED in details
        exc_resp = MagicMock()
        exc_resp.status = 403
        exc_content = json.dumps({
            "error": {
                "message": "Some API error",
                "details": [{"reason": "SERVICE_DISABLED"}]
            }
        }).encode("utf-8")
        exc1 = HttpError(exc_resp, exc_content)
        assert scan_module._is_service_disabled_error(exc1) is True

        # Mock exception 2: contains "has not been used in project" in message
        exc_content2 = json.dumps({
            "error": {
                "message": "Cloud Functions API has not been used in project 123 before or it is disabled."
            }
        }).encode("utf-8")
        exc2 = HttpError(exc_resp, exc_content2)
        assert scan_module._is_service_disabled_error(exc2) is True

        # Mock exception 3: standard non-disabled 403
        exc_content3 = json.dumps({
            "error": {
                "message": "Permission denied"
            }
        }).encode("utf-8")
        exc3 = HttpError(exc_resp, exc_content3)
        assert scan_module._is_service_disabled_error(exc3) is False

    def test_merge_responses_list(self) -> None:
        responses = [
            {"clusters": [{"name": "c1"}, {"name": "c2"}], "nextPageToken": "token1"},
            {"clusters": [{"name": "c3"}], "nextPageToken": "token2"},
            {"clusters": []}
        ]
        res = scan_module._merge_responses(responses, "clusters")
        assert len(res) == 3
        assert res == [{"name": "c1"}, {"name": "c2"}, {"name": "c3"}]

    def test_merge_responses_dict(self) -> None:
        # e.g., Compute aggregatedList
        responses = [
            {
                "items": {
                    "zones/us-central1-a": {"instances": [{"name": "i1"}]},
                    "zones/us-central1-b": {"instances": [{"name": "i2"}]}
                }
            },
            {
                "items": {
                    "zones/us-central1-a": {"instances": [{"name": "i3"}]},
                    "zones/us-central1-c": {"instances": [{"name": "i4"}]}
                }
            }
        ]
        res = scan_module._merge_responses(responses, "items")
        assert "zones/us-central1-b" in res
        assert "zones/us-central1-c" in res
        # zones/us-central1-a should be merged
        assert len(res["zones/us-central1-a"]["instances"]) == 2
        assert res["zones/us-central1-a"]["instances"] == [{"name": "i1"}, {"name": "i3"}]

    def test_dry_run_scan(self, capsys: Any) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scan_file = os.path.join(tmp, "scan.json")
            with open(scan_file, "w") as f:
                json.dump([
                    {
                        "api": "container",
                        "version": "v1",
                        "resource": "projects.locations.clusters",
                        "method": "list",
                        "parameters": {"parent": "projects/{project}/locations/-"},
                        "description": "GKE clusters"
                    },
                    {
                        "api": "bigquery",
                        "version": "v2",
                        "resource": "datasets",
                        "method": "list",
                        "description": "BQ Datasets"
                    }
                ], f)

            scan_module.dry_run_scan(scan_file, ["real-project-123"])
            captured = capsys.readouterr()
            assert "real-project-123" in captured.out
            assert "parent" in captured.out
            assert "projects/real-project-123/locations/-" in captured.out
            assert "projectId" in captured.out  # BQ projectId injection

    def test_get_service_data_disabled_api_graceful(self) -> None:
        from googleapiclient.errors import HttpError
        import logging

        # Setup disabled error
        exc_resp = MagicMock()
        exc_resp.status = 403
        exc_content = json.dumps({
            "error": {
                "message": "API has not been used in project or is disabled."
            }
        }).encode("utf-8")
        exc = HttpError(exc_resp, exc_content)

        # Mock build client
        mock_client = MagicMock()
        mock_resource = MagicMock()
        # Mock resource method execution throwing HttpError
        mock_method = MagicMock()
        mock_method.execute.side_effect = exc
        getattr(mock_resource, "list").return_value = mock_method

        service = {
            "api": "container",
            "version": "v1",
            "resource": "projects.locations.clusters",
            "method": "list",
            "parameters": {"parent": "projects/{project}/locations/-"}
        }

        log = logging.getLogger("test_disabled")
        with (
            patch("scan.build_gcp_client", return_value=mock_client),
            patch("scan._resolve_resource", return_value=mock_resource),
            patch.object(log, "warning") as mock_warning,
            patch.object(log, "error") as mock_error
        ):
            res = scan_module._get_service_data(MagicMock(), "proj-123", service, log, 1, 1)

        assert res is None
        # Should have logged warning instead of error
        mock_warning.assert_called_once()
        mock_error.assert_not_called()

    def test_count_gcp_resources_list(self) -> None:
        data = [{"name": "item1"}, {"name": "item2"}]
        assert scan_module._count_gcp_resources(data) == 2

    def test_count_gcp_resources_aggregated_list(self) -> None:
        # Compute Engine aggregatedList representation
        data = {
            "zones/us-central1-a": {
                "instances": [{"name": "i1"}, {"name": "i2"}]
            },
            "zones/us-central1-b": {
                "instances": [{"name": "i3"}]
            },
            "zones/us-central1-c": {
                "warning": {"message": "No instances"}
            }
        }
        assert scan_module._count_gcp_resources(data) == 3

    def test_count_gcp_resources_other(self) -> None:
        assert scan_module._count_gcp_resources(None) == 0
        assert scan_module._count_gcp_resources("some-string") == 1
        assert scan_module._count_gcp_resources({}) == 0

    def test_generate_summary_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "2026-06-13T14-00")
            project_dir = os.path.join(run_dir, "proj-123")
            os.makedirs(project_dir, exist_ok=True)

            # Write fake json results
            gke_file = os.path.join(project_dir, "container-projects_locations_clusters-list.json")
            with open(gke_file, "w") as f:
                json.dump([{"name": "c1"}, {"name": "c2"}], f)

            bq_file = os.path.join(project_dir, "bigquery-datasets-list.json")
            with open(bq_file, "w") as f:
                json.dump([{"name": "ds1"}], f)

            scan_module.generate_summary(run_dir)

            # Verify CSV file is created and has correct rows
            csv_path = os.path.join(run_dir, "summary.csv")
            assert os.path.exists(csv_path)
            with open(csv_path, "r") as f:
                content = f.read()
                assert "proj-123" in content
                assert "container" in content
                assert "projects.locations.clusters" in content
                assert "bigquery" in content
                assert "datasets" in content

            # Verify Markdown file is created and has correct lines
            md_path = os.path.join(run_dir, "summary.md")
            assert os.path.exists(md_path)
            with open(md_path, "r") as f:
                md_content = f.read()
                assert "# GCP Resource Inventory Run Summary" in md_content
                assert "proj-123" in md_content
                assert "**2**" in md_content
                assert "**1**" in md_content

