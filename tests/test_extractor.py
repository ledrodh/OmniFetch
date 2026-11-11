"""Unit tests for `core/extractor.py` covering SSRF stream inspection, playlists, and error mapping."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.extractor import get_info
from core.models import OperationalLimits
from utils.error_handler import (
    InvalidURLError,
    MediaUnavailableError,
    ResourceLimitExceededError,
    UnsupportedPlatformError,
)


def _make_mock_ydl(
    return_info: dict[str, Any] | None = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    mock_instance = MagicMock()
    if side_effect is not None:
        mock_instance.extract_info.side_effect = side_effect
    else:
        mock_instance.extract_info.return_value = return_info
        mock_instance.sanitize_info.side_effect = lambda d: d
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_instance
    mock_cm.__exit__.return_value = False
    return mock_cm


class TestExtractor:
    """Validate metadata extraction, SSRF stream/manifest inspection, and playlists."""

    def test_extracts_normalized_metadata_with_missing_fields_and_playlist_wrapper(self) -> None:
        playlist_wrapper: dict[str, Any] = {
            "_type": "playlist",
            "entries": [
                {
                    "id": "hls_stream_01",
                    "title": "   Live HLS Event   ",
                    "webpage_url": "https://93.184.216.34/live",
                    "extractor_key": "Generic",
                    "duration": None,
                    "thumbnail": "http://127.0.0.1/ssrf-thumb.jpg",  # SSRF thumbnail must be discarded
                    "uploader": "Official Channel",
                    "is_live": True,
                    "url": "https://93.184.216.34/master.m3u8",
                    "formats": [],
                }
            ],
        }
        with patch("core.extractor.SafeYoutubeDL", return_value=_make_mock_ydl(playlist_wrapper)):
            meta = get_info(
                "https://93.184.216.34/live",
                resolve_dns=False,
                ytdl_extra_opts={"no_color": True},
            )

        assert meta.media_id == "hls_stream_01"
        assert meta.title == "Live HLS Event"
        assert meta.duration_seconds is None
        assert meta.thumbnail_url is None  # Discarded because it points to 127.0.0.1
        assert meta.uploader == "Official Channel"
        assert meta.is_live is True
        assert len(meta.available_formats) >= 1

    def test_blocks_ssrf_inside_format_stream_url_or_fragment(self) -> None:
        poisoned_format_info: dict[str, Any] = {
            "id": "ssrf_fmt",
            "title": "Poisoned Manifest",
            "webpage_url": "https://93.184.216.34/watch",
            "formats": [
                {
                    "format_id": "1",
                    "ext": "mp4",
                    "vcodec": "h264",
                    "acodec": "aac",
                    "url": "https://93.184.216.34/ok.mp4",
                    "fragments": [{"url": "http://169.254.169.254/latest/meta-data/"}],
                }
            ],
        }
        with patch(
            "core.extractor.SafeYoutubeDL", return_value=_make_mock_ydl(poisoned_format_info)
        ):
            with pytest.raises(InvalidURLError):
                get_info("https://93.184.216.34/watch", resolve_dns=False)

    def test_rejects_empty_info_or_empty_playlist(self) -> None:
        with patch("core.extractor.SafeYoutubeDL", return_value=_make_mock_ydl(None)):
            with pytest.raises(MediaUnavailableError):
                get_info("https://93.184.216.34/empty", resolve_dns=False)

        with patch(
            "core.extractor.SafeYoutubeDL",
            return_value=_make_mock_ydl({"_type": "playlist", "entries": []}),
        ):
            with pytest.raises(MediaUnavailableError):
                get_info("https://93.184.216.34/empty-pl", resolve_dns=False)

    def test_rejects_media_exceeding_duration_or_all_known_filesizes(self) -> None:
        oversized_info: dict[str, Any] = {
            "id": "big",
            "title": "Big File",
            "webpage_url": "https://93.184.216.34/watch",
            "duration": 100,
            "formats": [
                {
                    "format_id": "18",
                    "ext": "mp4",
                    "vcodec": "h264",
                    "acodec": "aac",
                    "filesize": 500 * 1024 * 1024,
                }
            ],
        }
        limits = OperationalLimits(max_filesize_bytes=10 * 1024 * 1024)
        with patch("core.extractor.SafeYoutubeDL", return_value=_make_mock_ydl(oversized_info)):
            with pytest.raises(ResourceLimitExceededError):
                get_info("https://93.184.216.34/watch", limits=limits, resolve_dns=False)

    def test_maps_unsupported_and_unavailable_errors(self) -> None:
        with patch(
            "core.extractor.SafeYoutubeDL",
            return_value=_make_mock_ydl(
                side_effect=Exception("ERROR: Unsupported URL: https://93.184.216.34/x")
            ),
        ):
            with pytest.raises(UnsupportedPlatformError):
                get_info("https://93.184.216.34/x", resolve_dns=False)

    def test_get_info_sets_lang_format_sort_and_original_language(self) -> None:
        captured_opts: dict[str, Any] = {}
        yt_multi_audio: dict[str, Any] = {
            "id": "yt_pt_orig",
            "title": "Video em Portugues com Dublagem EN",
            "webpage_url": "https://93.184.216.34/watch?v=yt_pt_orig",
            "extractor_key": "Youtube",
            "duration": 120,
            "formats": [
                {
                    "format_id": "137",
                    "ext": "mp4",
                    "vcodec": "h264",
                    "acodec": "none",
                    "height": 1080,
                },
                {
                    "format_id": "140-0",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "aac",
                    "abr": 128,
                    "language": "en",
                    "language_preference": 5,
                    "format_note": "English (default)",
                },
                {
                    "format_id": "140-1",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "aac",
                    "abr": 128,
                    "language": "pt",
                    "language_preference": 10,
                    "format_note": "Portuguese (original)",
                },
            ],
        }

        def _fake_safe_ytdl(ydl_opts: dict[str, Any], resolve_dns: bool = True) -> MagicMock:
            captured_opts.update(ydl_opts)
            return _make_mock_ydl(yt_multi_audio)

        with patch("core.extractor.SafeYoutubeDL", side_effect=_fake_safe_ytdl):
            meta = get_info("https://93.184.216.34/watch?v=yt_pt_orig", resolve_dns=False)

        assert captured_opts["format_sort"] == ["lang"]
        assert meta.original_language == "pt"
        assert meta.available_formats[0].yt_dlp_format.startswith("137+140-1/")
