"""Unit tests for `utils/diagnostics.py` covering all 5 prerequisite checks and report exports."""

from __future__ import annotations

import importlib.metadata
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from core.models import DiagnosticCheck, DiagnosticReport, OperationalLimits
from utils.diagnostics import (
    check_binary,
    check_python_version,
    check_temp_directory,
    check_yt_dlp_package,
    export_diagnostic_summary,
    format_diagnostic_markdown,
    run_startup_diagnostics,
)

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


class TestDiagnosticsDedicated:
    """Validate pass/fail branches across all 5 startup checks and Markdown export."""

    def test_python_version_pass_and_fail(self) -> None:
        assert check_python_version((3, 10)).passed is True
        failed = check_python_version((99, 0))
        assert failed.passed is False
        assert "Install Python" in failed.remediation

    def test_yt_dlp_package_present_and_missing(self) -> None:
        assert check_yt_dlp_package().passed is True
        with patch(
            "importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError("yt-dlp"),
        ):
            missing = check_yt_dlp_package()
            assert missing.passed is False
            assert "pip install" in missing.remediation

    def test_binary_present_and_missing(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            assert check_binary("ffmpeg", "FFmpeg").passed is True
        with patch("shutil.which", return_value=None):
            missing = check_binary("ffprobe", "FFprobe")
            assert missing.passed is False
            assert "ffprobe" in missing.remediation

    def test_temp_directory_low_disk_space_corrupted_read_and_oserror(self, tmp_path: Path) -> None:
        with patch("shutil.disk_usage", return_value=DiskUsage(1000, 900, 100)):
            low_disk = check_temp_directory(temp_root=tmp_path, min_free_bytes=1024 * 1024)
            assert low_disk.passed is False
            assert "Insufficient" in low_disk.detail

        with patch.object(Path, "read_bytes", return_value=b"corrupted-bytes"):
            bad_integrity = check_temp_directory(temp_root=tmp_path)
            assert bad_integrity.passed is False
            assert "integrity" in bad_integrity.detail

        with patch.object(Path, "write_bytes", side_effect=OSError("Read-only filesystem")):
            err_chk = check_temp_directory(temp_root=tmp_path)
            assert err_chk.passed is False
            assert "No write permission" in err_chk.detail

    def test_startup_diagnostics_report_properties_and_export_all_branches(
        self, tmp_path: Path
    ) -> None:
        report = run_startup_diagnostics(
            temp_root=tmp_path,
            limits=OperationalLimits(min_free_disk_bytes=1024),
        )
        assert len(report.checks) == 5
        assert report.all_passed is True
        assert report.failed_checks == ()

        summary = export_diagnostic_summary(report, last_operation_status="Completed")
        assert summary["FFmpeg"] == "Available"
        assert summary["FFprobe"] == "Available"
        assert summary["Temporary Directory"] == "Writable"
        assert "MB free" in summary["Available Space"]
        assert summary["Last Operation"] == "Completed"
        assert str(tmp_path) not in str(summary)

        md = format_diagnostic_markdown(report, last_operation_status="Completed")
        assert "# Diagnostic Report — OmniFetch" in md

        failed_report = DiagnosticReport(
            checks=(
                DiagnosticCheck(name="Python Runtime", passed=False, detail="Failed"),
                DiagnosticCheck(name="yt-dlp Engine", passed=False, detail="Missing"),
                DiagnosticCheck(name="FFmpeg", passed=False, detail="Missing"),
                DiagnosticCheck(name="FFprobe", passed=False, detail="Missing"),
                DiagnosticCheck(
                    name="Temporary Storage",
                    passed=False,
                    detail="No write permission",
                ),
            )
        )
        failed_summary = export_diagnostic_summary(failed_report)
        assert failed_summary["yt-dlp"] == "Unavailable"
        assert failed_summary["FFmpeg"] == "Unavailable"
        assert failed_summary["FFprobe"] == "Unavailable"
        assert failed_summary["Temporary Directory"] == "Unwritable"
        assert failed_summary["Available Space"] == "Unknown"

        empty_summary = export_diagnostic_summary(DiagnosticReport(checks=()))
        assert empty_summary["Temporary Directory"] == "Unwritable"
        assert empty_summary["Available Space"] == "Unknown"
