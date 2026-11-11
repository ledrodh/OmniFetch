"""Custom exception hierarchy, error taxonomy mapping, and log sanitization."""

from __future__ import annotations

import logging
import re

from core.models import ErrorCategory, StructuredError

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_URL_CREDENTIALS_RE = re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@")
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|auth|sig|signature|key|api_key|session|hdnea|policy|key-pair-id)=)([^&\s\"']+)",
    re.IGNORECASE,
)


def sanitize_log_message(message: str) -> str:
    """Strip ANSI escape sequences, embedded URL credentials, and sensitive query tokens."""
    if not message:
        return ""
    cleaned = _ANSI_ESCAPE_RE.sub("", message)
    cleaned = _URL_CREDENTIALS_RE.sub(r"\1[REDACTED]:[REDACTED]@", cleaned)
    cleaned = _SENSITIVE_QUERY_RE.sub(r"\1[REDACTED]", cleaned)
    return cleaned


class SanitizedLogFormatter(logging.Formatter):
    """Logging formatter that sanitizes messages and tracebacks before emission."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return sanitize_log_message(rendered)


def get_sanitized_logger(name: str = "omnifetch") -> logging.Logger:
    """Return a configured logger with automatic sensitive-data sanitization."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = SanitizedLogFormatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class OmniFetchError(Exception):
    """Base domain exception for all OmniFetch operational errors."""

    category: ErrorCategory = ErrorCategory.EXTRACTION_FAILED
    default_user_message: str = "An unexpected error occurred while processing the media."

    def __init__(
        self,
        message: str | None = None,
        *,
        technical_detail: str = "",
    ) -> None:
        self.user_message = message or self.default_user_message
        self.technical_detail = sanitize_log_message(technical_detail or self.user_message)
        super().__init__(self.user_message)

    def to_structured(self) -> StructuredError:
        """Convert the exception into an immutable StructuredError for the UI layer."""
        return StructuredError(
            category=self.category,
            user_message=self.user_message,
            technical_detail=self.technical_detail,
        )


class InvalidURLError(OmniFetchError):
    """Invalid URL syntax, disallowed scheme, invalid timestamp, or Anti-SSRF block."""

    category = ErrorCategory.INVALID_URL
    default_user_message = "The provided address is not a valid public URL."


class UnsupportedPlatformError(OmniFetchError):
    """Extractor unavailable or unsupported by yt-dlp."""

    category = ErrorCategory.UNSUPPORTED_PLATFORM
    default_user_message = "This platform or URL format is not supported."


class MediaUnavailableError(OmniFetchError):
    """Private, removed, geo-restricted, or authentication-gated media."""

    category = ErrorCategory.MEDIA_UNAVAILABLE
    default_user_message = "Content is unavailable, private, or access-restricted."


class ResourceLimitExceededError(OmniFetchError):
    """Media exceeds configured duration, filesize, workspace, batch, or concurrency limits."""

    category = ErrorCategory.RESOURCE_LIMIT
    default_user_message = "The operation exceeds configured resource limits."


class NetworkTimeoutError(OmniFetchError):
    """Network connection drop or timeout exceeded while contacting the origin."""

    category = ErrorCategory.NETWORK_TIMEOUT
    default_user_message = "Connection timed out while communicating with the media host."


class ExtractionFailedError(OmniFetchError):
    """Upstream layout change or metadata extractor parsing failure."""

    category = ErrorCategory.EXTRACTION_FAILED
    default_user_message = "Failed to extract media metadata. Check yt-dlp updates."


class PostProcessingError(OmniFetchError):
    """FFmpeg or FFprobe failure during merge, remux, clipping, or inspection."""

    category = ErrorCategory.POST_PROCESSING
    default_user_message = "Download finished, but FFmpeg post-processing failed."


class StorageError(OmniFetchError):
    """Insufficient disk space, permission error, or path confinement violation."""

    category = ErrorCategory.STORAGE_ERROR
    default_user_message = "Failed to write temporary file to local storage."


def classify_yt_dlp_error(exc: Exception) -> OmniFetchError:
    """Map raw yt-dlp, network, or OS exceptions into OmniFetch's error taxonomy."""
    if isinstance(exc, OmniFetchError):
        return exc

    raw_msg = sanitize_log_message(str(exc))
    lower_msg = raw_msg.lower()

    if isinstance(exc, TimeoutError) or any(
        token in lower_msg
        for token in (
            "timed out",
            "timeout",
            "read operation timed out",
            "connection reset",
            "temporary failure in name resolution",
        )
    ):
        return NetworkTimeoutError(technical_detail=raw_msg)

    if isinstance(exc, PermissionError | FileNotFoundError | OSError) and any(
        token in lower_msg
        for token in ("no space left", "permission denied", "read-only file system", "errno 28")
    ):
        return StorageError(technical_detail=raw_msg)

    if any(
        token in lower_msg
        for token in (
            "unsupported url",
            "no suitable extractor",
            "is not a valid url",
        )
    ):
        return UnsupportedPlatformError(technical_detail=raw_msg)

    if any(
        token in lower_msg
        for token in (
            "private video",
            "video unavailable",
            "this video is unavailable",
            "has been removed",
            "sign in to confirm your age",
            "login required",
            "members-only",
            "http error 403",
            "http error 404",
            "http error 401",
            "geo-restricted",
            "not available in your country",
        )
    ):
        return MediaUnavailableError(technical_detail=raw_msg)

    if any(
        token in lower_msg
        for token in (
            "file is larger than max-filesize",
            "max-filesize",
            "exceeds maximum",
        )
    ):
        return ResourceLimitExceededError(technical_detail=raw_msg)

    if any(
        token in lower_msg
        for token in (
            "postprocessing:",
            "ffmpeg",
            "ffprobe",
            "conversion failed",
            "error opening output file",
        )
    ):
        return PostProcessingError(technical_detail=raw_msg)

    return ExtractionFailedError(technical_detail=raw_msg)
