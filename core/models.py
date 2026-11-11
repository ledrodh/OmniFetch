"""Immutable data models, operational state enums, and resource limits for OmniFetch."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class DownloadMode(str, Enum):
    """Extraction mode selected by the user."""

    VIDEO = "video"
    AUDIO_ONLY = "audio_only"


class OutputProfile(str, Enum):
    """Output profile controlling FFmpeg post-processing behavior."""

    ORIGINAL = "original"
    COMPATIBILITY = "compatibility"


class OperationStatus(str, Enum):
    """4-stage processing pipeline states plus idle/ready/error."""

    IDLE = "idle"
    IDENTIFYING = "identifying"
    READY = "ready"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    ERROR = "error"


class ErrorCategory(str, Enum):
    """Structured taxonomy of OmniFetch operational errors."""

    INVALID_URL = "invalid_url"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    MEDIA_UNAVAILABLE = "media_unavailable"
    RESOURCE_LIMIT = "resource_limit"
    NETWORK_TIMEOUT = "network_timeout"
    EXTRACTION_FAILED = "extraction_failed"
    POST_PROCESSING = "post_processing"
    STORAGE_ERROR = "storage_error"


@dataclass(frozen=True, slots=True)
class OperationalLimits:
    """Configurable resource governance limits for local and global execution."""

    max_duration_seconds: int = 3 * 3600  # 3 hours
    max_filesize_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB per logical media output
    max_workspace_bytes: int = 4 * 1024 * 1024 * 1024  # 4 GiB per session workspace
    max_global_workspace_bytes: int = 8 * 1024 * 1024 * 1024  # 8 GiB global process cap
    max_batch_urls: int = 5  # Maximum URLs per controlled sequential batch (v1.1)
    max_history_entries: int = 20  # Maximum sanitized entries retained in session FIFO (v1.1)
    socket_timeout_seconds: int = 15
    download_timeout_seconds: int = 1800
    max_concurrent_operations: int = 2
    temp_ttl_seconds: int = 1800  # 30 minutes
    min_free_disk_bytes: int = 512 * 1024 * 1024  # 512 MiB


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Validated post-download temporal clipping interval in seconds."""

    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        """Return effective clipped segment duration in seconds."""
        return round(max(0.0, self.end_seconds - self.start_seconds), 3)

    @staticmethod
    def _fmt_sec(sec: float) -> str:
        total = int(round(max(0.0, sec)))
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    @property
    def label(self) -> str:
        """Human-readable interval representation (e.g. 00:10 - 00:45)."""
        return f"{self._fmt_sec(self.start_seconds)} - {self._fmt_sec(self.end_seconds)}"


@dataclass(frozen=True, slots=True)
class FormatOption:
    """Normalized stream option ready for UI selection and download execution."""

    format_id: str
    yt_dlp_format: str
    label: str
    mode: DownloadMode
    profile: OutputProfile
    container: str
    video_codec: str
    audio_codec: str
    resolution: str
    height: int | None = None
    fps: float | None = None
    abr_kbps: float | None = None
    tbr_kbps: float | None = None
    filesize_bytes: int | None = None
    is_filesize_estimated: bool = False
    requires_merge: bool = False
    requires_reencode: bool = False
    audio_language: str | None = None
    is_original_audio: bool | None = None

    @property
    def filesize_human(self) -> str:
        """Return a human-readable filesize label."""
        if self.filesize_bytes is None or self.filesize_bytes <= 0:
            return "Unknown"
        prefix = "~" if self.is_filesize_estimated else ""
        mib = self.filesize_bytes / (1024 * 1024)
        if mib >= 1024:
            gib = mib / 1024
            return f"{prefix}{gib:.2f} GB"
        return f"{prefix}{mib:.1f} MB"


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    """Normalized metadata for public media extracted via yt-dlp."""

    media_id: str
    title: str
    webpage_url: str
    platform: str
    extractor_key: str
    duration_seconds: float | None = None
    thumbnail_url: str | None = None
    uploader: str | None = None
    is_live: bool = False
    original_language: str | None = None
    raw_formats: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    available_formats: tuple[FormatOption, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ProgressState:
    """Immutable snapshot of 4-stage pipeline progress."""

    stage: OperationStatus
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bps: float | None = None
    eta_seconds: float | None = None
    percent: float | None = None
    postprocessor_name: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Consolidated result of a download and post-processing operation."""

    operation_id: str
    output_path: Path
    filename: str
    filesize_bytes: int
    elapsed_seconds: float
    format_used: FormatOption
    time_range: TimeRange | None = None


@dataclass(frozen=True, slots=True)
class SessionHistoryEntry:
    """Sanitized in-memory operation record for the current session (no local paths or tokens)."""

    operation_id: str
    title: str
    platform: str
    format_label: str
    filesize_bytes: int
    elapsed_seconds: float
    status: OperationStatus
    timestamp_iso: str
    time_range_label: str = "Full"
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    """Isolated result of a single item inside a controlled sequential batch."""

    index: int
    url: str
    succeeded: bool
    title: str = ""
    result: DownloadResult | None = None
    error: StructuredError | None = None


@dataclass(frozen=True, slots=True)
class BatchSummary:
    """Consolidated summary of a sequential batch with partial-success and cancellation support."""

    items: tuple[BatchItemResult, ...]
    cancelled: bool = False

    @property
    def succeeded_count(self) -> int:
        """Number of batch items that completed successfully."""
        return sum(1 for item in self.items if item.succeeded)

    @property
    def failed_count(self) -> int:
        """Number of batch items that failed without aborting remaining items."""
        return sum(1 for item in self.items if not item.succeeded)

    @property
    def total_bytes(self) -> int:
        """Total output bytes produced by successful batch items."""
        return sum(
            item.result.filesize_bytes
            for item in self.items
            if item.succeeded and item.result is not None
        )


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """Individual result for a startup environment prerequisite check."""

    name: str
    passed: bool
    detail: str
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    """Consolidated report from Startup Check diagnostics."""

    checks: tuple[DiagnosticCheck, ...]

    @property
    def all_passed(self) -> bool:
        """Indicate whether every mandatory prerequisite check passed."""
        return all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> tuple[DiagnosticCheck, ...]:
        """Return only failed prerequisite checks."""
        return tuple(check for check in self.checks if not check.passed)


@dataclass(frozen=True, slots=True)
class StructuredError:
    """User-safe immutable error representation for the UI layer."""

    category: ErrorCategory
    user_message: str
    technical_detail: str = ""
