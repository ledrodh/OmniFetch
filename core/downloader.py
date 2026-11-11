"""Download orchestration, atomic global quota reservation, post-download trimming, and sequential batches."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from core.extractor import get_info
from core.format_selector import filter_formats
from core.media_processor import build_postprocessor_config, trim_media_segment
from core.models import (
    BatchItemResult,
    BatchSummary,
    DownloadMode,
    DownloadResult,
    FormatOption,
    OperationalLimits,
    OperationStatus,
    ProgressState,
    TimeRange,
)
from utils.error_handler import (
    ExtractionFailedError,
    OmniFetchError,
    PostProcessingError,
    ResourceLimitExceededError,
    StorageError,
    classify_yt_dlp_error,
    get_sanitized_logger,
)
from utils.file_manager import TempFileManager, sanitize_filename
from utils.validators import (
    ConcurrencyGuard,
    SafeYoutubeDL,
    operation_slot,
    validate_media_limits,
    validate_url,
)

logger = get_sanitized_logger(__name__)

ProgressCallback = Callable[[ProgressState], None]


def _calculate_directory_bytes(directory: Path) -> int:
    """Compute actual bytes occupied inside a single operation folder (excluding .lease markers)."""
    total_size = 0
    if not directory.exists():
        return 0
    for item in directory.rglob("*"):
        if item.is_file() and item.name != ".lease":
            try:
                total_size += item.stat().st_size
            except OSError:
                continue
    return total_size


def _locate_final_file(operation_dir: Path, preferred_ext: str) -> Path:
    """Locate the final post-processed output artifact inside the isolated operation directory."""
    candidates = [
        p
        for p in operation_dir.iterdir()
        if p.is_file()
        and not p.name.endswith((".part", ".ytdl", ".temp", ".tmp", ".lease"))
        and not p.name.startswith(".")
    ]
    if not candidates:
        raise PostProcessingError(
            technical_detail=f"No output media artifact found in {operation_dir}"
        )

    ext_lower = f".{preferred_ext.lower().lstrip('.')}"
    matching_ext = [p for p in candidates if p.suffix.lower() == ext_lower]
    selected = max(matching_ext or candidates, key=lambda p: p.stat().st_mtime)

    if selected.stat().st_size <= 0:
        raise PostProcessingError(
            technical_detail=f"Generated output artifact is empty (0 bytes): {selected.name}"
        )
    return selected


def download_media(
    url: str,
    format_option: FormatOption,
    *,
    title: str = "media",
    time_range: TimeRange | None = None,
    operation_id: str | None = None,
    file_manager: TempFileManager | None = None,
    limits: OperationalLimits | None = None,
    progress_callback: ProgressCallback | None = None,
    resolve_dns: bool = True,
    guard: ConcurrencyGuard | None = None,
) -> DownloadResult:
    """Execute download, post-processing, optional post-download trim, atomic reservation, and lease."""
    active_limits = limits or OperationalLimits()
    manager = file_manager or TempFileManager(ttl_seconds=active_limits.temp_ttl_seconds)
    op_id = operation_id or str(uuid.uuid4())
    start_ts = time.monotonic()

    def _emit(state: ProgressState) -> None:
        if progress_callback is not None:
            progress_callback(state)

    # Stage 1: Identify target, validate limits, and atomically reserve global capacity
    _emit(
        ProgressState(
            stage=OperationStatus.IDENTIFYING,
            message="Validating address and reserving temporary workspace capacity...",
        )
    )
    validated_url = validate_url(url, resolve_dns=resolve_dns)
    validate_media_limits(
        filesize_bytes=format_option.filesize_bytes,
        limits=active_limits,
    )
    manager.reserve_space(
        op_id,
        format_option.filesize_bytes or 0,
        active_limits.max_global_workspace_bytes,
    )

    safe_stem = sanitize_filename(title, max_length=80, default="omnifetch_media")

    # Aggregated multi-stream accounting (e.g. separate DASH video + audio downloads)
    stream_downloaded: dict[str, int] = {}
    stream_totals: dict[str, int] = {}
    op_dir_ref: Path | None = None

    def _resolve_stream_key(d: dict[str, Any]) -> str:
        info_dict = d.get("info_dict")
        if isinstance(info_dict, dict) and info_dict.get("format_id"):
            return f"fmt_{info_dict['format_id']}"
        for key in ("tmpfilename", "filename"):
            val = d.get(key)
            if isinstance(val, str) and val:
                return val
        return "primary"

    def _on_progress_hook(d: dict[str, Any]) -> None:
        status = str(d.get("status") or "")
        stream_key = _resolve_stream_key(d)
        downloaded = max(0, int(d.get("downloaded_bytes") or 0))
        total_raw = d.get("total_bytes") or d.get("total_bytes_estimate")
        total = int(total_raw) if isinstance(total_raw, int | float) and total_raw > 0 else None

        stream_downloaded[stream_key] = downloaded
        if total is not None:
            stream_totals[stream_key] = max(total, downloaded)

        cumulative_downloaded = sum(stream_downloaded.values())
        cumulative_total = (
            sum(stream_totals.values())
            if stream_totals
            else (format_option.filesize_bytes if format_option.filesize_bytes else None)
        )
        disk_used = _calculate_directory_bytes(op_dir_ref) if op_dir_ref is not None else 0

        # Defense-in-depth limits: logical media cap, session workspace cap, and atomic global cap
        validate_media_limits(
            filesize_bytes=cumulative_downloaded,
            workspace_bytes=disk_used,
            limits=active_limits,
        )
        if cumulative_total is not None:
            validate_media_limits(filesize_bytes=cumulative_total, limits=active_limits)

        required_reservation = max(cumulative_downloaded, disk_used, cumulative_total or 0)
        manager.reserve_space(
            op_id,
            required_reservation,
            active_limits.max_global_workspace_bytes,
        )

        if status == "downloading":
            speed_raw = d.get("speed")
            speed = (
                float(speed_raw) if isinstance(speed_raw, int | float) and speed_raw > 0 else None
            )
            eta_raw = d.get("eta")
            eta = float(eta_raw) if isinstance(eta_raw, int | float) and eta_raw >= 0 else None
            percent = (
                min(99.9, round((cumulative_downloaded / cumulative_total) * 100.0, 1))
                if (cumulative_total is not None and cumulative_total > 0)
                else None
            )
            _emit(
                ProgressState(
                    stage=OperationStatus.DOWNLOADING,
                    downloaded_bytes=cumulative_downloaded,
                    total_bytes=cumulative_total,
                    speed_bps=speed,
                    eta_seconds=eta,
                    percent=percent,
                    message="Downloading media stream...",
                )
            )
        elif status == "finished":
            _emit(
                ProgressState(
                    stage=OperationStatus.PROCESSING,
                    downloaded_bytes=cumulative_downloaded,
                    total_bytes=cumulative_total or cumulative_downloaded,
                    percent=100.0,
                    message="Download finished. Running FFmpeg post-processing...",
                )
            )

    def _on_postprocessor_hook(d: dict[str, Any]) -> None:
        pp_status = str(d.get("status") or "")
        pp_name = str(d.get("postprocessor") or "FFmpeg")
        if pp_status in ("started", "processing"):
            _emit(
                ProgressState(
                    stage=OperationStatus.PROCESSING,
                    downloaded_bytes=sum(stream_downloaded.values()),
                    percent=100.0,
                    postprocessor_name=pp_name,
                    message=f"Processing media via FFmpeg ({pp_name})...",
                )
            )

    try:
        with (
            operation_slot(active_limits, guard=guard),
            manager.active_operation(op_id, retain_lease_on_success=True) as op_dir,
        ):
            op_dir_ref = op_dir
            outtmpl = str(op_dir / f"{safe_stem}.%(ext)s")
            ydl_opts: dict[str, Any] = {
                "format": format_option.yt_dlp_format,
                "outtmpl": {"default": outtmpl},
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "socket_timeout": active_limits.socket_timeout_seconds,
                "max_filesize": active_limits.max_filesize_bytes,
                "default_search": "error",
                "overwrites": True,
                # Explicit sequential fragments keep fragment fetches deterministic inside the DNS guard
                "concurrent_fragment_downloads": 1,
                "format_sort": ["lang"],
                "progress_hooks": [_on_progress_hook],
                "postprocessor_hooks": [_on_postprocessor_hook],
            }
            ydl_opts.update(build_postprocessor_config(format_option))

            try:
                with SafeYoutubeDL(ydl_opts, resolve_dns=resolve_dns) as ydl:
                    ret_code = ydl.download([validated_url])
                    if isinstance(ret_code, int) and ret_code != 0:
                        raise ExtractionFailedError(
                            technical_detail=f"yt-dlp exited with non-zero code {ret_code}."
                        )
            except OmniFetchError:
                manager.cleanup_operation(op_id, force=True)
                raise
            except Exception as exc:
                manager.cleanup_operation(op_id, force=True)
                mapped = classify_yt_dlp_error(exc)
                logger.error(
                    "Download/post-processing failure (%s): %s",
                    mapped.category,
                    mapped.technical_detail,
                )
                raise mapped from exc

            final_file = _locate_final_file(op_dir, format_option.container)
            resolved_final = final_file.resolve()
            if not resolved_final.is_relative_to(op_dir.resolve()):
                manager.cleanup_operation(op_id, force=True)
                raise StorageError(
                    technical_detail=f"Generated artifact escaped isolated workspace: {resolved_final}"
                )

            if time_range is not None:
                _emit(
                    ProgressState(
                        stage=OperationStatus.PROCESSING,
                        downloaded_bytes=resolved_final.stat().st_size,
                        percent=100.0,
                        postprocessor_name="FFmpegTrim",
                        message=f"Applying post-download trim ({time_range.label})...",
                    )
                )
                try:
                    resolved_final = trim_media_segment(resolved_final, time_range)
                except OmniFetchError:
                    manager.cleanup_operation(op_id, force=True)
                    raise

            final_size = resolved_final.stat().st_size
            try:
                validate_media_limits(
                    filesize_bytes=final_size,
                    global_workspace_bytes=manager.get_total_workspace_bytes(),
                    limits=active_limits,
                )
            except OmniFetchError:
                manager.cleanup_operation(op_id, force=True)
                raise
    finally:
        manager.release_reservation(op_id)

    elapsed = round(time.monotonic() - start_ts, 2)
    _emit(
        ProgressState(
            stage=OperationStatus.COMPLETED,
            downloaded_bytes=final_size,
            total_bytes=final_size,
            percent=100.0,
            message=f"Completed in {elapsed:.1f}s.",
        )
    )

    return DownloadResult(
        operation_id=op_id,
        output_path=resolved_final,
        filename=resolved_final.name,
        filesize_bytes=final_size,
        elapsed_seconds=elapsed,
        format_used=format_option,
        time_range=time_range,
    )


def execute_batch_download(
    urls: Sequence[str],
    *,
    mode: DownloadMode = DownloadMode.VIDEO,
    file_manager: TempFileManager | None = None,
    limits: OperationalLimits | None = None,
    cancel_check: Callable[[], bool] | None = None,
    on_item_progress: Callable[[int, str, ProgressState], None] | None = None,
    resolve_dns: bool = True,
    guard: ConcurrencyGuard | None = None,
) -> BatchSummary:
    """
    Execute sequential controlled batch processing (v1.1) with:
    - Strict cap of `max_batch_urls` (5 URLs);
    - Failure isolation (partial success if any single URL or FFmpeg step fails);
    - Atomic global temporary storage quota checks before each item;
    - Cooperative between-item cancellation via `cancel_check` (stops after the current item).
    """
    active_limits = limits or OperationalLimits()
    manager = file_manager or TempFileManager(ttl_seconds=active_limits.temp_ttl_seconds)

    cleaned_urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    if not cleaned_urls:
        return BatchSummary(items=(), cancelled=False)

    if len(cleaned_urls) > active_limits.max_batch_urls:
        raise ResourceLimitExceededError(
            f"Batch contains {len(cleaned_urls)} URLs, exceeding the maximum allowed ({active_limits.max_batch_urls}).",
            technical_detail=f"batch_size={len(cleaned_urls)} > max_batch_urls={active_limits.max_batch_urls}",
        )

    results: list[BatchItemResult] = []
    cancelled = False

    for idx, item_url in enumerate(cleaned_urls, start=1):
        if cancel_check is not None and cancel_check():
            cancelled = True
            break

        try:
            validate_media_limits(
                global_workspace_bytes=manager.get_committed_workspace_bytes(),
                limits=active_limits,
            )
            meta = get_info(
                item_url,
                limits=active_limits,
                resolve_dns=resolve_dns,
                guard=guard,
            )
            candidates = filter_formats(meta.available_formats, mode=mode)
            chosen_fmt = candidates[0] if candidates else meta.available_formats[0]

            def _item_cb(pstate: ProgressState, _idx: int = idx, _url: str = item_url) -> None:
                if on_item_progress is not None:
                    on_item_progress(_idx, _url, pstate)

            dl_res = download_media(
                item_url,
                chosen_fmt,
                title=meta.title,
                file_manager=manager,
                limits=active_limits,
                progress_callback=_item_cb,
                resolve_dns=resolve_dns,
                guard=guard,
            )
            results.append(
                BatchItemResult(
                    index=idx,
                    url=item_url,
                    succeeded=True,
                    title=meta.title,
                    result=dl_res,
                )
            )
        except Exception as exc:
            mapped = exc if isinstance(exc, OmniFetchError) else classify_yt_dlp_error(exc)
            results.append(
                BatchItemResult(
                    index=idx,
                    url=item_url,
                    succeeded=False,
                    error=mapped.to_structured(),
                )
            )

    return BatchSummary(items=tuple(results), cancelled=cancelled)
