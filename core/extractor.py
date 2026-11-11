"""Safe public media metadata extraction via yt-dlp without downloading content."""

from __future__ import annotations

from typing import Any

from core.format_selector import build_format_options, infer_original_language
from core.models import MediaMetadata, OperationalLimits
from utils.error_handler import (
    ExtractionFailedError,
    MediaUnavailableError,
    OmniFetchError,
    classify_yt_dlp_error,
    get_sanitized_logger,
)
from utils.validators import (
    ConcurrencyGuard,
    SafeYoutubeDL,
    operation_slot,
    validate_media_limits,
    validate_url,
)

logger = get_sanitized_logger(__name__)
_STREAM_URL_FIELDS = ("url", "manifest_url", "fragment_base_url")


def _normalize_duration(raw_duration: Any) -> float | None:
    """Convert raw duration field to a positive float or None when absent (e.g. HLS live streams)."""
    if isinstance(raw_duration, int | float) and raw_duration > 0:
        return float(raw_duration)
    return None


def _extract_primary_entry(raw_info: dict[str, Any] | None) -> dict[str, Any]:
    """Unwrap single-item playlist/multi_video containers or validate the primary metadata dict."""
    if not raw_info or not isinstance(raw_info, dict):
        raise MediaUnavailableError(technical_detail="yt-dlp returned empty metadata (None).")

    if raw_info.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in (raw_info.get("entries") or []) if isinstance(e, dict)]
        if not entries:
            raise MediaUnavailableError(
                technical_detail="Playlist or container returned zero valid entries."
            )
        return entries[0]

    return raw_info


def _validate_extracted_stream_urls(
    info: dict[str, Any],
    raw_formats: list[dict[str, Any]],
    *,
    resolve_dns: bool,
) -> None:
    """Inspect all extracted page, manifest, format, and fragment URLs against SSRF."""
    for top_field in ("webpage_url", "original_url", "url", "manifest_url"):
        val = info.get(top_field)
        if isinstance(val, str) and val.strip():
            validate_url(val, resolve_dns=resolve_dns)

    for fmt in raw_formats:
        for field_name in _STREAM_URL_FIELDS:
            stream_url = fmt.get(field_name)
            if isinstance(stream_url, str) and stream_url.strip():
                validate_url(stream_url, resolve_dns=resolve_dns)

        fragments = fmt.get("fragments")
        if isinstance(fragments, list):
            for frag in fragments:
                if isinstance(frag, dict) and isinstance(frag.get("url"), str):
                    validate_url(frag["url"], resolve_dns=resolve_dns)


def get_info(
    url: str,
    *,
    limits: OperationalLimits | None = None,
    resolve_dns: bool = True,
    guard: ConcurrencyGuard | None = None,
    ytdl_extra_opts: dict[str, Any] | None = None,
) -> MediaMetadata:
    """Extract and normalize metadata for a public URL enforcing timeout and Anti-SSRF checks."""
    active_limits = limits or OperationalLimits()
    validated_url = validate_url(url, resolve_dns=resolve_dns)

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "skip_download": True,
        "socket_timeout": active_limits.socket_timeout_seconds,
        "default_search": "error",
        "geo_bypass": False,
        # Prioritize language_preference (original audio track) before codec/bitrate in yt-dlp sort
        "format_sort": ["lang"],
    }
    if ytdl_extra_opts:
        ydl_opts.update(ytdl_extra_opts)

    with operation_slot(active_limits, guard=guard):
        try:
            with SafeYoutubeDL(ydl_opts, resolve_dns=resolve_dns) as ydl:
                raw_result = ydl.extract_info(validated_url, download=False)
                sanitized_dict = (
                    ydl.sanitize_info(raw_result)
                    if raw_result is not None and hasattr(ydl, "sanitize_info")
                    else raw_result
                )
        except OmniFetchError:
            raise
        except Exception as exc:
            mapped = classify_yt_dlp_error(exc)
            logger.warning(
                "Metadata extraction failed (%s): %s",
                mapped.category,
                mapped.technical_detail,
            )
            raise mapped from exc

    info = _extract_primary_entry(sanitized_dict if isinstance(sanitized_dict, dict) else None)
    raw_formats_list = [f for f in (info.get("formats") or []) if isinstance(f, dict)]

    # Validate all extracted page, manifest, stream, and fragment URLs against SSRF
    _validate_extracted_stream_urls(info, raw_formats_list, resolve_dns=resolve_dns)

    final_webpage_url = str(info.get("webpage_url") or info.get("original_url") or validated_url)
    direct_manifest_url = info.get("url")

    duration_seconds = _normalize_duration(info.get("duration"))
    validate_media_limits(duration_seconds=duration_seconds, limits=active_limits)

    if not raw_formats_list and isinstance(direct_manifest_url, str):
        raw_formats_list = [
            {
                "format_id": str(info.get("format_id") or "0"),
                "ext": str(info.get("ext") or "mp4"),
                "vcodec": str(info.get("vcodec") or "h264"),
                "acodec": str(info.get("acodec") or "aac"),
                "height": info.get("height"),
                "fps": info.get("fps"),
                "tbr": info.get("tbr"),
                "abr": info.get("abr"),
                "filesize": info.get("filesize"),
                "filesize_approx": info.get("filesize_approx"),
            }
        ]

    known_sizes = [
        int(f["filesize"])
        for f in raw_formats_list
        if isinstance(f.get("filesize"), int | float) and f["filesize"] > 0
    ]
    if known_sizes and min(known_sizes) > active_limits.max_filesize_bytes:
        validate_media_limits(filesize_bytes=min(known_sizes), limits=active_limits)

    original_language = infer_original_language(
        raw_formats_list,
        info_language=info.get("language"),
    )
    available_formats = build_format_options(
        raw_formats_list,
        duration_seconds=duration_seconds,
        original_language=original_language,
        limits=active_limits,
    )
    if not available_formats:
        raise ExtractionFailedError(technical_detail="No compatible formats found after filtering.")

    extractor_key = str(info.get("extractor_key") or info.get("extractor") or "Generic")
    platform = str(info.get("extractor_key") or info.get("webpage_url_domain") or extractor_key)
    title = str(info.get("title") or "Untitled Media").strip() or "Untitled Media"
    media_id = str(info.get("id") or "unknown")
    thumbnail_raw = info.get("thumbnail")
    thumbnail_url = None
    if isinstance(thumbnail_raw, str) and thumbnail_raw.startswith(("http://", "https://")):
        try:
            thumbnail_url = validate_url(thumbnail_raw, resolve_dns=resolve_dns)
        except OmniFetchError:
            thumbnail_url = None

    uploader = str(info["uploader"]).strip() if isinstance(info.get("uploader"), str) else None
    is_live = bool(info.get("is_live") or False)

    if is_live and duration_seconds is None:
        logger.info("Live stream detected for media_id=%s", media_id)

    return MediaMetadata(
        media_id=media_id,
        title=title,
        webpage_url=final_webpage_url,
        platform=platform,
        extractor_key=extractor_key,
        duration_seconds=duration_seconds,
        thumbnail_url=thumbnail_url,
        uploader=uploader,
        is_live=is_live,
        original_language=original_language,
        raw_formats=tuple(raw_formats_list),
        available_formats=available_formats,
    )
