"""Media post-processing (remuxing, re-encoding, post-download trimming, and FFprobe inspection)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.models import DownloadMode, FormatOption, OutputProfile, TimeRange
from utils.error_handler import PostProcessingError, get_sanitized_logger

logger = get_sanitized_logger(__name__)
_FASTSTART_CONTAINERS = frozenset({"mp4", "m4a", "mov"})


def build_postprocessor_config(format_option: FormatOption) -> dict[str, Any]:
    """Build yt-dlp postprocessor configuration respecting native containers and Original vs Compatibility profiles."""
    config: dict[str, Any] = {}
    postprocessors: list[dict[str, Any]] = []
    pp_args: dict[str, list[str]] = {}
    container_lower = format_option.container.lower().lstrip(".")

    if format_option.mode == DownloadMode.VIDEO:
        if format_option.profile == OutputProfile.COMPATIBILITY or format_option.requires_reencode:
            config["merge_output_format"] = "mp4"
            postprocessors.append(
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }
            )
            pp_args["videoconvertor"] = [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
            ]
        else:
            # Original Profile: fast lossless remux/merge (-c copy) preserving native codecs
            config["merge_output_format"] = container_lower
            if container_lower in _FASTSTART_CONTAINERS:
                pp_args["merger"] = ["-c", "copy", "-movflags", "+faststart"]
            else:
                pp_args["merger"] = ["-c", "copy"]
    else:
        # Audio-Only Mode
        if container_lower == "mp3" or format_option.requires_reencode:
            target_kbps = int(format_option.abr_kbps or 192)
            clamped_kbps = max(64, min(target_kbps, 320))
            postprocessors.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(clamped_kbps),
                }
            )
        elif container_lower == "m4a":
            postprocessors.append(
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": "m4a",
                }
            )
            pp_args["videoremuxer"] = ["-c", "copy", "-movflags", "+faststart"]

    if postprocessors:
        config["postprocessors"] = postprocessors
    if pp_args:
        config["postprocessor_args"] = pp_args

    return config


def probe_media_file(file_path: Path, *, timeout_seconds: int = 15) -> dict[str, Any]:
    """Inspect container and stream metadata of a local media file via FFprobe."""
    if not file_path.exists() or not file_path.is_file():
        raise PostProcessingError(
            technical_detail=f"Media file not found for FFprobe inspection: {file_path}"
        )

    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(file_path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PostProcessingError(
            technical_detail=f"Failed to execute FFprobe on {file_path.name}: {exc}"
        ) from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise PostProcessingError(
            technical_detail=f"FFprobe returned non-zero exit code {completed.returncode}: {stderr}"
        )

    try:
        data = json.loads(completed.stdout or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as exc:
        raise PostProcessingError(
            technical_detail=f"Invalid JSON output from FFprobe: {exc}"
        ) from exc


def remux_media(
    input_path: Path,
    output_path: Path,
    *,
    faststart: bool = True,
    timeout_seconds: int = 300,
) -> Path:
    """Perform lossless stream-copy remuxing (-c copy) via FFmpeg."""
    if not input_path.exists():
        raise PostProcessingError(
            technical_detail=f"Input file does not exist for remuxing: {input_path}"
        )

    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-i",
        str(input_path),
        "-c",
        "copy",
    ]
    ext_clean = output_path.suffix.lower().lstrip(".")
    if faststart and ext_clean in _FASTSTART_CONTAINERS:
        cmd.extend(["-movflags", "+faststart"])
    cmd.append(str(output_path))

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PostProcessingError(
            technical_detail=f"Failed to execute FFmpeg remux: {exc}"
        ) from exc

    if completed.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        stderr = (completed.stderr or "").strip()
        raise PostProcessingError(
            technical_detail=f"FFmpeg remux failed (exit={completed.returncode}): {stderr}"
        )

    return output_path


def trim_media_segment(
    input_path: Path,
    time_range: TimeRange,
    *,
    output_path: Path | None = None,
    faststart: bool = True,
    timeout_seconds: int = 300,
) -> Path:
    """
    Post-download trim using fast FFmpeg input seeking (`-ss` / `-to` before `-i`) and `-c copy`.

    Because stream copy does not re-encode frames, start/end boundaries snap to the nearest
    keyframes rather than providing frame-accurate re-encoded cuts (planned for a future mode).
    """
    if not input_path.exists():
        raise PostProcessingError(
            technical_detail=f"Input file does not exist for temporal trimming: {input_path}"
        )

    if output_path is None or output_path.resolve() == input_path.resolve():
        in_place = True
        target_out = input_path.with_name(f"{input_path.stem}.clip_tmp{input_path.suffix}")
    else:
        in_place = False
        target_out = output_path

    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    # Placing both -ss and -to as input options before -i uses demuxer seeking in source timeline
    cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{time_range.start_seconds:.3f}",
        "-to",
        f"{time_range.end_seconds:.3f}",
        "-i",
        str(input_path),
        "-c",
        "copy",
    ]
    ext_clean = target_out.suffix.lower().lstrip(".")
    if faststart and ext_clean in _FASTSTART_CONTAINERS:
        cmd.extend(["-movflags", "+faststart"])
    cmd.append(str(target_out))

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        target_out.unlink(missing_ok=True)
        raise PostProcessingError(technical_detail=f"Failed to execute FFmpeg trim: {exc}") from exc

    if completed.returncode != 0 or not target_out.exists() or target_out.stat().st_size == 0:
        stderr = (completed.stderr or "").strip()
        target_out.unlink(missing_ok=True)
        raise PostProcessingError(
            technical_detail=f"FFmpeg temporal trim failed (exit={completed.returncode}): {stderr}"
        )

    if in_place:
        target_out.replace(input_path)
        return input_path

    return target_out
