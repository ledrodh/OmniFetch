"""Unit tests for `utils/error_handler.py`."""

from __future__ import annotations

import logging

import pytest

from core.models import ErrorCategory
from utils.error_handler import (
    ExtractionFailedError,
    InvalidURLError,
    MediaUnavailableError,
    NetworkTimeoutError,
    OmniFetchError,
    PostProcessingError,
    ResourceLimitExceededError,
    SanitizedLogFormatter,
    StorageError,
    UnsupportedPlatformError,
    classify_yt_dlp_error,
    get_sanitized_logger,
    sanitize_log_message,
)


class TestErrorHandler:
    """Validate error taxonomy mapping, ANSI/credential stripping, and log formatting."""

    def test_sanitize_empty_and_sensitive_messages(self) -> None:
        assert sanitize_log_message("") == ""
        raw = (
            "\x1b[31mERROR:\x1b[0m Failure at https://admin:secret123@cdn.example.com/v.mp4"
            "?token=my_secret_jwt&sig=998877&hdnea=abc&Policy=xyz&Key-Pair-Id=111&id=42"
        )
        sanitized = sanitize_log_message(raw)
        assert "secret123" not in sanitized
        assert "my_secret_jwt" not in sanitized
        assert "998877" not in sanitized
        assert "abc" not in sanitized
        assert "xyz" not in sanitized
        assert "111" not in sanitized
        assert "[REDACTED]" in sanitized
        assert "id=42" in sanitized

    def test_sanitized_log_formatter_and_logger(self) -> None:
        formatter = SanitizedLogFormatter("%(levelname)s: %(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=10,
            msg="URL https://u:p@example.com/a?api_key=TOPSECRET",
            args=(),
            exc_info=None,
        )
        formatted = formatter.format(record)
        assert "TOPSECRET" not in formatted
        assert "u:p" not in formatted

        logger = get_sanitized_logger("omnifetch.dedicated_test")
        assert len(logger.handlers) >= 1

    def test_passthrough_when_already_omnifetch_error(self) -> None:
        orig = InvalidURLError(technical_detail="detail")
        assert classify_yt_dlp_error(orig) is orig
        structured = orig.to_structured()
        assert structured.category == ErrorCategory.INVALID_URL

    @pytest.mark.parametrize(
        ("raw_exc", "expected_cls", "expected_cat"),
        [
            (TimeoutError("Socket timed out"), NetworkTimeoutError, ErrorCategory.NETWORK_TIMEOUT),
            (
                Exception("ERROR: Unsupported URL: https://foo.bar"),
                UnsupportedPlatformError,
                ErrorCategory.UNSUPPORTED_PLATFORM,
            ),
            (
                Exception("ERROR: Private video. Sign in if you have access"),
                MediaUnavailableError,
                ErrorCategory.MEDIA_UNAVAILABLE,
            ),
            (
                Exception("ERROR: File is larger than max-filesize"),
                ResourceLimitExceededError,
                ErrorCategory.RESOURCE_LIMIT,
            ),
            (
                Exception("ERROR: Postprocessing: ffmpeg exited with code 1"),
                PostProcessingError,
                ErrorCategory.POST_PROCESSING,
            ),
            (
                OSError("Errno 28: No space left on device"),
                StorageError,
                ErrorCategory.STORAGE_ERROR,
            ),
            (
                Exception("Some unknown parser breakdown"),
                ExtractionFailedError,
                ErrorCategory.EXTRACTION_FAILED,
            ),
        ],
    )
    def test_classify_yt_dlp_error_categories(
        self,
        raw_exc: Exception,
        expected_cls: type[OmniFetchError],
        expected_cat: ErrorCategory,
    ) -> None:
        mapped = classify_yt_dlp_error(raw_exc)
        assert isinstance(mapped, expected_cls)
        assert mapped.category == expected_cat
