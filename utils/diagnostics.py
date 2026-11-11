"""Runtime environment diagnostics (5 startup checks) and sanitized Markdown export."""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from core.models import DiagnosticCheck, DiagnosticReport, OperationalLimits

OMNIFETCH_VERSION = "1.1.0"
_FREE_SPACE_RE = re.compile(r"\((\d+\s*MB\s*free)\)")


def check_python_version(min_version: tuple[int, int] = (3, 10)) -> DiagnosticCheck:
    """Verify that the running Python interpreter satisfies the minimum version."""
    current = (sys.version_info.major, sys.version_info.minor)
    version_str = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if current >= min_version:
        return DiagnosticCheck(
            name="Python Runtime",
            passed=True,
            detail=f"Python {version_str} detected.",
        )
    return DiagnosticCheck(
        name="Python Runtime",
        passed=False,
        detail=f"Python {version_str} is below the required minimum ({min_version[0]}.{min_version[1]}).",
        remediation=f"Install Python {min_version[0]}.{min_version[1]}+ and recreate the virtual environment.",
    )


def check_yt_dlp_package() -> DiagnosticCheck:
    """Verify that the yt-dlp package is installed and discoverable."""
    try:
        version = importlib.metadata.version("yt-dlp")
        return DiagnosticCheck(
            name="yt-dlp Engine",
            passed=True,
            detail=f"yt-dlp {version} installed.",
        )
    except importlib.metadata.PackageNotFoundError:
        return DiagnosticCheck(
            name="yt-dlp Engine",
            passed=False,
            detail="yt-dlp package was not found in the active Python environment.",
            remediation="Run: pip install -U yt-dlp",
        )


def check_binary(binary_name: str, display_name: str) -> DiagnosticCheck:
    """Verify that an external binary (FFmpeg / FFprobe) is available on PATH."""
    resolved = shutil.which(binary_name)
    if resolved:
        return DiagnosticCheck(
            name=display_name,
            passed=True,
            detail=f"Binary found at: {resolved}",
        )
    return DiagnosticCheck(
        name=display_name,
        passed=False,
        detail=f"Executable '{binary_name}' was not found on system PATH.",
        remediation=(
            f"Install FFmpeg (including {binary_name}) and add its 'bin' directory "
            "to your system PATH (e.g. via apt, winget, or brew)."
        ),
    )


def check_temp_directory(
    temp_root: Path | None = None,
    min_free_bytes: int = 512 * 1024 * 1024,
) -> DiagnosticCheck:
    """Verify write/read/delete permissions and free disk space in temporary storage."""
    env_dir = os.environ.get("OMNIFETCH_TEMP_DIR")
    default_dir = Path(env_dir) if env_dir else (Path(tempfile.gettempdir()) / "omnifetch_temp")
    base_dir = temp_root or default_dir
    probe_file = base_dir / f".diag_probe_{uuid.uuid4().hex}.tmp"
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        probe_file.write_bytes(b"omnifetch-ok")
        content = probe_file.read_bytes()
        probe_file.unlink(missing_ok=True)
        if content != b"omnifetch-ok":
            return DiagnosticCheck(
                name="Temporary Storage",
                passed=False,
                detail=f"Write integrity check failed at: {base_dir}",
                remediation="Verify read/write permissions on the temporary workspace directory.",
            )

        usage = shutil.disk_usage(base_dir)
        free_mib = usage.free / (1024 * 1024)
        if usage.free < min_free_bytes:
            req_mib = min_free_bytes / (1024 * 1024)
            return DiagnosticCheck(
                name="Temporary Storage",
                passed=False,
                detail=f"Insufficient free disk space ({free_mib:.0f} MB free; minimum {req_mib:.0f} MB).",
                remediation="Free up disk space before starting new downloads.",
            )

        return DiagnosticCheck(
            name="Temporary Storage",
            passed=True,
            detail=f"Write check passed at {base_dir} ({free_mib:.0f} MB free).",
        )
    except OSError as exc:
        return DiagnosticCheck(
            name="Temporary Storage",
            passed=False,
            detail=f"No write permission at {base_dir}: {exc}",
            remediation="Grant write permissions to the configured temporary directory.",
        )


def run_startup_diagnostics(
    temp_root: Path | None = None,
    limits: OperationalLimits | None = None,
) -> DiagnosticReport:
    """Execute the 5 mandatory OmniFetch startup environment checks."""
    active_limits = limits or OperationalLimits()
    checks = (
        check_python_version(),
        check_yt_dlp_package(),
        check_binary("ffmpeg", "FFmpeg"),
        check_binary("ffprobe", "FFprobe"),
        check_temp_directory(
            temp_root=temp_root,
            min_free_bytes=active_limits.min_free_disk_bytes,
        ),
    )
    return DiagnosticReport(checks=checks)


def export_diagnostic_summary(
    report: DiagnosticReport,
    *,
    last_operation_status: str = "None",
    app_version: str = OMNIFETCH_VERSION,
) -> dict[str, str]:
    """Generate a path-free diagnostic dictionary safe for display or issue reporting."""
    check_map = {c.name: c for c in report.checks}

    py_chk = check_map.get("Python Runtime")
    py_ver = (
        py_chk.detail.replace("Python ", "").replace(" detected.", "")
        if py_chk and py_chk.passed
        else f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )

    yt_chk = check_map.get("yt-dlp Engine")
    yt_ver = (
        yt_chk.detail.replace("yt-dlp ", "").replace(" installed.", "")
        if yt_chk and yt_chk.passed
        else "Unavailable"
    )

    ff_chk = check_map.get("FFmpeg")
    fp_chk = check_map.get("FFprobe")
    storage_chk = check_map.get("Temporary Storage")

    free_space = "Unknown"
    if storage_chk:
        match = _FREE_SPACE_RE.search(storage_chk.detail)
        if match:
            free_space = match.group(1)

    return {
        "OmniFetch Version": app_version,
        "Python": py_ver,
        "yt-dlp": yt_ver,
        "FFmpeg": "Available" if (ff_chk and ff_chk.passed) else "Unavailable",
        "FFprobe": "Available" if (fp_chk and fp_chk.passed) else "Unavailable",
        "Temporary Directory": "Writable" if (storage_chk and storage_chk.passed) else "Unwritable",
        "Available Space": free_space,
        "Last Operation": last_operation_status,
    }


def format_diagnostic_markdown(
    report: DiagnosticReport,
    *,
    last_operation_status: str = "None",
    app_version: str = OMNIFETCH_VERSION,
) -> str:
    """Format a sanitized Markdown diagnostic report with zero local path leakage."""
    summary = export_diagnostic_summary(
        report,
        last_operation_status=last_operation_status,
        app_version=app_version,
    )
    lines = [
        "# Diagnostic Report — OmniFetch",
        "",
        "| Field | Status |",
        "| :--- | :--- |",
    ]
    for key, val in summary.items():
        lines.append(f"| {key} | {val} |")
    return "\n".join(lines) + "\n"
