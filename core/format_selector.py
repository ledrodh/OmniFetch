"""Selection, filtering, original-audio ranking, and normalization of yt-dlp streams."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from core.models import DownloadMode, FormatOption, OperationalLimits, OutputProfile

_ORIGINAL_NOTE_RE = re.compile(r"\boriginal\b", re.IGNORECASE)
_DRC_NOTE_RE = re.compile(r"\bdrc\b", re.IGNORECASE)
_DUBBED_TOKENS = ("dubbed", "autodub", "auto-dub", "translated", "ai-dub")


def normalize_language_code(raw_lang: Any) -> str | None:
    """Normalize an ISO/yt-dlp language tag (e.g. 'pt-BR' -> 'pt-br') or return None."""
    if not raw_lang or not isinstance(raw_lang, str):
        return None
    val = raw_lang.strip().lower().replace("_", "-")
    if val in ("none", "und", "unknown", "zxx", ""):
        return None
    return val


def _languages_match(candidate: str | None, target: str | None) -> bool:
    """Return True if two normalized language tags match directly or via primary ISO subtag."""
    if not candidate or not target:
        return False
    if candidate.endswith("-desc") and not target.endswith("-desc"):
        return False
    if candidate == target:
        return True
    return candidate.split("-", 1)[0] == target.split("-", 1)[0]


def _classify_audio_markers(fmt: dict[str, Any]) -> tuple[bool, bool, bool, bool]:
    """Identify audio track markers: `(is_original, is_dubbed, is_descriptive, is_drc)`."""
    lang = normalize_language_code(fmt.get("language"))
    note = str(fmt.get("format_note") or "").lower()
    fmt_id = str(fmt.get("format_id") or "").lower()
    raw_pref = fmt.get("language_preference")
    lang_pref = float(raw_pref) if isinstance(raw_pref, int | float) else None

    is_descriptive = (
        "descriptive" in note
        or "audio description" in note
        or (lang is not None and lang.endswith("-desc"))
        or (lang_pref is not None and lang_pref <= -10)
    )
    is_original = not is_descriptive and (
        bool(_ORIGINAL_NOTE_RE.search(note))
        or fmt_id.endswith(("-orig", "-original", "_orig", "_original"))
        or (lang_pref is not None and lang_pref >= 10)
    )
    is_dubbed = (
        not is_descriptive
        and not is_original
        and any(tok in note or tok in fmt_id for tok in _DUBBED_TOKENS)
    )
    is_drc = bool(_DRC_NOTE_RE.search(note)) or fmt_id.endswith(("-drc", "_drc"))
    return is_original, is_dubbed, is_descriptive, is_drc


def infer_original_language(
    raw_formats: Sequence[dict[str, Any]],
    *,
    info_language: Any = None,
) -> str | None:
    """Infer the media's original production language from format markers or top-level metadata."""
    for fmt in raw_formats:
        if not isinstance(fmt, dict):
            continue
        if normalize_codec_name(fmt.get("acodec")) == "none":
            continue
        is_original, _, is_descriptive, _ = _classify_audio_markers(fmt)
        if is_original and not is_descriptive:
            lang = normalize_language_code(fmt.get("language"))
            if lang:
                return lang
    return normalize_language_code(info_language)


def _stream_audio_originality_rank(
    fmt: dict[str, Any] | None,
    *,
    original_language: str | None = None,
    has_known_original: bool = False,
) -> tuple[int, float, int]:
    """Compute originality priority rank: `(tier, language_preference, non_drc)`."""
    if fmt is None:
        return (2, 0.0, 1)

    is_original, is_dubbed, is_descriptive, is_drc = _classify_audio_markers(fmt)
    lang = normalize_language_code(fmt.get("language"))
    raw_pref = fmt.get("language_preference")
    lang_pref = float(raw_pref) if isinstance(raw_pref, int | float) else 0.0

    if is_descriptive:
        tier = -1
    elif is_dubbed:
        tier = 0
    elif is_original:
        tier = 4
    elif original_language is not None and _languages_match(lang, original_language):
        tier = 3
    elif (original_language is not None or has_known_original) and lang is not None:
        tier = 1
    else:
        tier = 2

    return (tier, lang_pref, 0 if is_drc else 1)


def _format_audio_lang_suffix(audio_lang: str | None, is_original: bool) -> str:
    """Format an optional language suffix only when track language or originality metadata exists."""
    if audio_lang and is_original:
        return f" • {audio_lang.upper()} Original"
    if audio_lang:
        return f" • {audio_lang.upper()}"
    if is_original:
        return " • Original"
    return ""


def normalize_codec_name(raw_codec: Any) -> str:
    """Normalize technical codec strings (RFC 6381) into friendly identifiers."""
    if not raw_codec or not isinstance(raw_codec, str):
        return "none"
    val = raw_codec.strip().lower()
    if val in ("none", "unknown", ""):
        return "none"
    if val.startswith(("avc1", "avc3", "h264", "x264")):
        return "h264"
    if val.startswith(("hev1", "hvc1", "hevc", "h265")):
        return "h265"
    if val.startswith(("vp09", "vp9")):
        return "vp9"
    if val.startswith(("vp08", "vp8")):
        return "vp8"
    if val.startswith(("av01", "av1")):
        return "av1"
    if val.startswith(("mp3", "mp4a.69", "mp4a.6b")):
        return "mp3"
    if val.startswith(("mp4a", "aac")):
        return "aac"
    if val.startswith("opus"):
        return "opus"
    if val.startswith("vorbis"):
        return "vorbis"
    if val.startswith("flac"):
        return "flac"
    return val.split(".")[0]


def estimate_stream_filesize(
    fmt: dict[str, Any],
    duration_seconds: float | None,
) -> tuple[int | None, bool]:
    """Return `(filesize_bytes, is_estimated)` for an individual stream."""
    exact = fmt.get("filesize")
    if isinstance(exact, int | float) and exact > 0:
        return int(exact), False

    approx = fmt.get("filesize_approx")
    if isinstance(approx, int | float) and approx > 0:
        return int(approx), True

    if duration_seconds is not None and duration_seconds > 0:
        bitrate_kbps = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
        if isinstance(bitrate_kbps, int | float) and bitrate_kbps > 0:
            estimated = int((float(bitrate_kbps) * 1000.0 / 8.0) * float(duration_seconds))
            if estimated > 0:
                return estimated, True

    return None, False


def _has_known_original_marker(raw_formats: Sequence[dict[str, Any]]) -> bool:
    """Return True if any audio-bearing stream explicitly carries an original-track marker."""
    for fmt in raw_formats:
        if not isinstance(fmt, dict) or normalize_codec_name(fmt.get("acodec")) == "none":
            continue
        is_original, _, is_descriptive, _ = _classify_audio_markers(fmt)
        if is_original and not is_descriptive:
            return True
    return False


def _select_best_audio_stream(
    raw_formats: list[dict[str, Any]],
    preferred_container: str = "mp4",
    *,
    duration_seconds: float | None = None,
    max_filesize_bytes: int | None = None,
    original_language: str | None = None,
    has_known_original: bool = False,
) -> dict[str, Any] | None:
    """Select the best audio-only stream for DASH merging, strictly prioritizing original language."""
    audio_only: list[dict[str, Any]] = []
    for f in raw_formats:
        if (
            normalize_codec_name(f.get("vcodec")) != "none"
            or normalize_codec_name(f.get("acodec")) == "none"
            or not f.get("format_id")
        ):
            continue
        if max_filesize_bytes is not None:
            a_size, _ = estimate_stream_filesize(f, duration_seconds)
            if a_size is not None and a_size > max_filesize_bytes:
                continue
        audio_only.append(f)
    if not audio_only:
        return None

    def _score(fmt: dict[str, Any]) -> tuple[tuple[int, float, int], int, float]:
        orig_rank = _stream_audio_originality_rank(
            fmt,
            original_language=original_language,
            has_known_original=has_known_original,
        )
        acodec = normalize_codec_name(fmt.get("acodec"))
        ext = str(fmt.get("ext") or "").lower()
        compat = 1 if (preferred_container == "mp4" and (acodec == "aac" or ext == "m4a")) else 0
        abr = float(fmt.get("abr") or fmt.get("tbr") or 0.0)
        return (orig_rank, compat, abr)

    return max(audio_only, key=_score)


def build_format_options(
    raw_formats: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    duration_seconds: float | None = None,
    original_language: str | None = None,
    limits: OperationalLimits | None = None,
) -> tuple[FormatOption, ...]:
    """Build normalized video and audio options (Original Profile), defaulting to original audio."""
    active_limits = limits or OperationalLimits()
    valid_raw = [
        f
        for f in raw_formats
        if isinstance(f, dict)
        and f.get("format_id")
        and str(f.get("ext") or "").lower() not in ("mhtml", "none")
        and "storyboard" not in str(f.get("format_note") or "").lower()
    ]

    resolved_orig_lang = infer_original_language(valid_raw, info_language=original_language)
    has_known_original = _has_known_original_marker(valid_raw)

    video_options: dict[tuple[int, str, str], tuple[tuple[int, float, int], FormatOption]] = {}
    audio_options: dict[tuple[str, str, int, str], tuple[tuple[int, float, int], FormatOption]] = {}

    for fmt in valid_raw:
        fmt_id = str(fmt["format_id"])
        ext = str(fmt.get("ext") or "mp4").lower()
        vcodec = normalize_codec_name(fmt.get("vcodec"))
        acodec = normalize_codec_name(fmt.get("acodec"))
        height_raw = fmt.get("height")
        height = int(height_raw) if isinstance(height_raw, int | float) and height_raw > 0 else None
        fps_raw = fmt.get("fps")
        fps = float(fps_raw) if isinstance(fps_raw, int | float) and fps_raw > 0 else None
        abr_raw = fmt.get("abr")
        abr = float(abr_raw) if isinstance(abr_raw, int | float) and abr_raw > 0 else None
        tbr_raw = fmt.get("tbr")
        tbr = float(tbr_raw) if isinstance(tbr_raw, int | float) and tbr_raw > 0 else None

        size_bytes, is_est = estimate_stream_filesize(fmt, duration_seconds)

        # Video Streams (Progressive or DASH/HLS Video-Only)
        if vcodec != "none":
            requires_merge = acodec == "none"
            target_container = (
                "mp4" if ext in ("mp4", "m4v", "mov") else ("webm" if ext == "webm" else "mkv")
            )
            effective_acodec = acodec
            yt_dlp_spec = fmt_id
            effective_audio_fmt: dict[str, Any] | None = fmt

            if requires_merge:
                best_audio = _select_best_audio_stream(
                    valid_raw,
                    preferred_container=target_container,
                    duration_seconds=duration_seconds,
                    max_filesize_bytes=active_limits.max_filesize_bytes,
                    original_language=resolved_orig_lang,
                    has_known_original=has_known_original,
                )
                effective_audio_fmt = best_audio
                if best_audio is not None:
                    audio_id = str(best_audio["format_id"])
                    effective_acodec = normalize_codec_name(best_audio.get("acodec"))
                    yt_dlp_spec = f"{fmt_id}+{audio_id}/{fmt_id}+bestaudio/best"
                    audio_size, audio_est = estimate_stream_filesize(best_audio, duration_seconds)
                    if size_bytes is not None and audio_size is not None:
                        size_bytes += audio_size
                        is_est = is_est or audio_est
                    if effective_acodec == "opus" and target_container == "mp4":
                        target_container = "mkv"
                else:
                    yt_dlp_spec = f"{fmt_id}+bestaudio/{fmt_id}/best"
                    effective_acodec = "auto"

            if size_bytes is not None and size_bytes > active_limits.max_filesize_bytes:
                continue

            audio_rank = _stream_audio_originality_rank(
                effective_audio_fmt,
                original_language=resolved_orig_lang,
                has_known_original=has_known_original,
            )
            audio_lang = (
                normalize_language_code(effective_audio_fmt.get("language"))
                if effective_audio_fmt is not None
                else None
            )
            is_orig = audio_rank[0] >= 3 if (resolved_orig_lang or has_known_original) else False
            lang_suffix = _format_audio_lang_suffix(audio_lang, is_orig)

            res_label = f"{height}p" if height else str(fmt.get("resolution") or "Auto")
            fps_suffix = f"{int(fps)}" if (fps and fps > 30) else ""
            display_res = f"{res_label}{fps_suffix}"
            option = FormatOption(
                format_id=f"video_{fmt_id}_{OutputProfile.ORIGINAL.value}",
                yt_dlp_format=yt_dlp_spec,
                label=f"{display_res} ({target_container.upper()} • {vcodec.upper()}/{effective_acodec.upper()}{lang_suffix})",
                mode=DownloadMode.VIDEO,
                profile=OutputProfile.ORIGINAL,
                container=target_container,
                video_codec=vcodec,
                audio_codec=effective_acodec,
                resolution=display_res,
                height=height,
                fps=fps,
                abr_kbps=abr,
                tbr_kbps=tbr,
                filesize_bytes=size_bytes,
                is_filesize_estimated=is_est,
                requires_merge=requires_merge,
                requires_reencode=False,
                audio_language=audio_lang,
                is_original_audio=is_orig
                if (resolved_orig_lang or has_known_original or audio_rank[0] <= 0)
                else None,
            )

            dedup_key = (height or 0, target_container, vcodec)
            existing_entry = video_options.get(dedup_key)
            if existing_entry is None:
                video_options[dedup_key] = (audio_rank, option)
            else:
                existing_rank, existing_opt = existing_entry
                candidate_key = (
                    audio_rank[0],
                    audio_rank[1],
                    option.tbr_kbps or 0.0,
                    audio_rank[2],
                )
                current_key = (
                    existing_rank[0],
                    existing_rank[1],
                    existing_opt.tbr_kbps or 0.0,
                    existing_rank[2],
                )
                if candidate_key >= current_key:
                    video_options[dedup_key] = (audio_rank, option)

        # Audio-Only Streams
        elif acodec != "none":
            if size_bytes is not None and size_bytes > active_limits.max_filesize_bytes:
                continue

            audio_rank = _stream_audio_originality_rank(
                fmt,
                original_language=resolved_orig_lang,
                has_known_original=has_known_original,
            )
            audio_lang = normalize_language_code(fmt.get("language"))
            is_orig = audio_rank[0] >= 3 if (resolved_orig_lang or has_known_original) else False
            lang_suffix = _format_audio_lang_suffix(audio_lang, is_orig)

            audio_container = (
                "m4a"
                if acodec == "aac" or ext == "m4a"
                else (ext if ext in ("opus", "mp3", "ogg", "webm", "flac") else "m4a")
            )
            rounded_abr = int(round(abr or tbr or 128.0))
            audio_opt = FormatOption(
                format_id=f"audio_{fmt_id}_{OutputProfile.ORIGINAL.value}",
                yt_dlp_format=fmt_id,
                label=f"Audio {rounded_abr} kbps ({audio_container.upper()} • {acodec.upper()}{lang_suffix})",
                mode=DownloadMode.AUDIO_ONLY,
                profile=OutputProfile.ORIGINAL,
                container=audio_container,
                video_codec="none",
                audio_codec=acodec,
                resolution="Audio-Only",
                height=None,
                fps=None,
                abr_kbps=float(rounded_abr),
                tbr_kbps=tbr,
                filesize_bytes=size_bytes,
                is_filesize_estimated=is_est,
                requires_merge=False,
                requires_reencode=False,
                audio_language=audio_lang,
                is_original_audio=is_orig
                if (resolved_orig_lang or has_known_original or audio_rank[0] <= 0)
                else None,
            )
            audio_key = (audio_container, acodec, rounded_abr, audio_lang or "")
            existing_audio = audio_options.get(audio_key)
            if existing_audio is None:
                audio_options[audio_key] = (audio_rank, audio_opt)
            else:
                existing_rank, existing_opt = existing_audio
                if (audio_rank, audio_opt.tbr_kbps or 0.0) >= (
                    existing_rank,
                    existing_opt.tbr_kbps or 0.0,
                ):
                    audio_options[audio_key] = (audio_rank, audio_opt)

    sorted_videos = [
        opt
        for _, opt in sorted(
            video_options.values(),
            key=lambda pair: (
                pair[1].height or 0,
                pair[0][0],
                pair[1].fps or 0.0,
                pair[1].tbr_kbps or 0.0,
            ),
            reverse=True,
        )
    ]
    sorted_audios = [
        opt
        for _, opt in sorted(
            audio_options.values(),
            key=lambda pair: (
                pair[0][0],
                pair[0][1],
                pair[1].abr_kbps or 0.0,
                pair[1].tbr_kbps or 0.0,
                pair[0][2],
            ),
            reverse=True,
        )
    ]

    # Add universal MP3 conversion option targeting the original-language source track
    best_mp3_audio = _select_best_audio_stream(
        valid_raw,
        preferred_container="mp3",
        duration_seconds=duration_seconds,
        max_filesize_bytes=active_limits.max_filesize_bytes,
        original_language=resolved_orig_lang,
        has_known_original=has_known_original,
    )
    best_abr = (
        int(sorted_audios[0].abr_kbps) if sorted_audios and sorted_audios[0].abr_kbps else 192
    )
    best_size = sorted_audios[0].filesize_bytes if sorted_audios else None
    mp3_yt_dlp = (
        f"{best_mp3_audio['format_id']}/bestaudio/best"
        if best_mp3_audio is not None
        else "bestaudio/best"
    )
    mp3_lang = (
        normalize_language_code(best_mp3_audio.get("language"))
        if best_mp3_audio is not None
        else resolved_orig_lang
    )
    mp3_is_orig = bool(
        best_mp3_audio is not None
        and _stream_audio_originality_rank(
            best_mp3_audio,
            original_language=resolved_orig_lang,
            has_known_original=has_known_original,
        )[0]
        >= 3
        and (resolved_orig_lang is not None or has_known_original)
    )
    mp3_lang_suffix = _format_audio_lang_suffix(mp3_lang, mp3_is_orig)
    mp3_option = FormatOption(
        format_id=f"audio_best_mp3_{OutputProfile.ORIGINAL.value}",
        yt_dlp_format=mp3_yt_dlp,
        label=f"Audio MP3 ~{best_abr} kbps (Direct conversion without bitrate inflation{mp3_lang_suffix})",
        mode=DownloadMode.AUDIO_ONLY,
        profile=OutputProfile.ORIGINAL,
        container="mp3",
        video_codec="none",
        audio_codec="mp3",
        resolution="Audio-Only",
        abr_kbps=float(best_abr),
        filesize_bytes=best_size,
        is_filesize_estimated=True,
        requires_merge=False,
        requires_reencode=True,
        audio_language=mp3_lang,
        is_original_audio=mp3_is_orig if (resolved_orig_lang or has_known_original) else None,
    )

    # Fallback when generic extractors (e.g. generic HLS) omit per-stream details
    if not sorted_videos:
        sorted_videos.append(
            FormatOption(
                format_id="video_best_auto_original",
                yt_dlp_format="bestvideo+bestaudio/best",
                label="Best Available Quality (MP4/MKV • Native)",
                mode=DownloadMode.VIDEO,
                profile=OutputProfile.ORIGINAL,
                container="mp4",
                video_codec="auto",
                audio_codec="auto",
                resolution="Auto",
                requires_merge=True,
                requires_reencode=False,
                audio_language=resolved_orig_lang,
                is_original_audio=True if resolved_orig_lang else None,
            )
        )

    return tuple([*sorted_videos, *sorted_audios, mp3_option])


def filter_formats(
    formats: tuple[FormatOption, ...] | list[FormatOption],
    *,
    mode: DownloadMode | None = None,
    profile: OutputProfile | None = None,
    container: str | None = None,
    max_height: int | None = None,
) -> tuple[FormatOption, ...]:
    """Filter a collection of FormatOption objects by the given criteria."""
    result: list[FormatOption] = []
    for opt in formats:
        if mode is not None and opt.mode != mode:
            continue
        if profile is not None and opt.profile != profile:
            continue
        if container is not None and opt.container.lower() != container.lower():
            continue
        if max_height is not None and opt.height is not None and opt.height > max_height:
            continue
        result.append(opt)
    return tuple(result)
