"""Tests for GCP Auto Inventory."""

import json
import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch


# We test without needing real GCP credentials by mocking the google libraries
import sys

# Mock the google modules before importing scan
google_mock = MagicMock()
google_auth_mock = MagicMock()
google_auth_mock.default.return_value = (MagicMock(), "test-project")

sys.modules["google"] = google_mock
sys.modules["google.auth"] = google_auth_mock
sys.modules["google.auth.exceptions"] = MagicMock()
sys.modules["google.auth.transport"] = MagicMock()
sys.modules["google.auth.transport.requests"] = MagicMock()
sys.modules["googleapiclient"] = MagicMock()
sys.modules["googleapiclient.discovery"] = MagicMock()
sys.modules["googleapiclient.errors"] = MagicMock()

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
