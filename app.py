"""Streamlit UI, 4-Stage Pipeline State Machine, and v1.1 Features (Trimming, History, Sequential Batch)."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import streamlit as st

from core.downloader import download_media, execute_batch_download
from core.extractor import get_info
from core.format_selector import filter_formats
from core.models import (
    BatchSummary,
    DiagnosticReport,
    DownloadMode,
    FormatOption,
    MediaMetadata,
    OperationalLimits,
    OperationStatus,
    ProgressState,
    SessionHistoryEntry,
    StructuredError,
)
from utils.diagnostics import (
    export_diagnostic_summary,
    format_diagnostic_markdown,
    run_startup_diagnostics,
)
from utils.error_handler import OmniFetchError, classify_yt_dlp_error, sanitize_log_message
from utils.file_manager import TempFileManager
from utils.validators import validate_time_range

_MAX_HISTORY_TITLE_LEN = 120

STATE_DEFAULTS: dict[str, Any] = {
    "source_url": "",
    "last_inspected_url": "",
    "media_info": None,
    "download_mode": DownloadMode.VIDEO,
    "selected_format": None,
    "clip_start": "",
    "clip_end": "",
    "operation_status": OperationStatus.IDLE,
    "progress_data": {
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed_bps": None,
        "eta_seconds": None,
        "percent": None,
        "postprocessor_name": None,
        "message": "",
    },
    "output_file": None,
    "error_message": None,
    "active_operation_id": None,
    "download_elapsed": None,
    "session_history": [],
    "batch_summary": None,
    "batch_urls_text": "",
    "batch_mode": DownloadMode.VIDEO,
    "batch_cancel_requested": False,
}

_MIME_TYPES: dict[str, str] = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
}


def format_duration(seconds: float | None, *, is_live: bool = False) -> str:
    """Format duration in seconds into HH:MM:SS or MM:SS."""
    if is_live and (seconds is None or seconds <= 0):
        return "LIVE (Continuous Stream)"
    if seconds is None or seconds <= 0:
        return "Unknown"
    total_sec = int(round(seconds))
    hours, rem = divmod(total_sec, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_speed(speed_bps: float | None) -> str:
    """Format transfer speed in bytes/sec for display."""
    if speed_bps is None or speed_bps <= 0:
        return "-- MB/s"
    mib_s = speed_bps / (1024 * 1024)
    if mib_s >= 1.0:
        return f"{mib_s:.2f} MB/s"
    kib_s = speed_bps / 1024
    return f"{kib_s:.0f} KB/s"


def get_mime_type(filepath: Path) -> str:
    """Return the MIME type matching a file extension."""
    ext = filepath.suffix.lower().lstrip(".")
    return _MIME_TYPES.get(ext, "application/octet-stream")


def _sanitize_history_title(raw_title: str) -> str:
    """Strip sensitive tokens/ANSI codes and cap media title length for in-memory session history."""
    cleaned = sanitize_log_message(raw_title).strip() or "Untitled Media"
    if len(cleaned) > _MAX_HISTORY_TITLE_LEN:
        return f"{cleaned[: _MAX_HISTORY_TITLE_LEN - 3].rstrip()}..."
    return cleaned


def init_session_state(state: MutableMapping[str, Any] | None = None) -> MutableMapping[str, Any]:
    """Idempotently initialize all required state-machine keys in Streamlit session_state."""
    target: MutableMapping[str, Any] = (
        cast(MutableMapping[str, Any], st.session_state) if state is None else state
    )
    for key, default_val in STATE_DEFAULTS.items():
        if key not in target:
            if isinstance(default_val, dict):
                target[key] = dict(default_val)
            elif isinstance(default_val, list):
                target[key] = list(default_val)
            else:
                target[key] = default_val
    return target


def record_session_history(
    entry: SessionHistoryEntry,
    *,
    state: MutableMapping[str, Any] | None = None,
    limits: OperationalLimits | None = None,
) -> None:
    """Append a sanitized metadata entry to the bounded in-memory session FIFO."""
    target = init_session_state(state)
    active_limits = limits or OperationalLimits()
    sanitized_entry = SessionHistoryEntry(
        operation_id=entry.operation_id[:8],
        title=_sanitize_history_title(entry.title),
        platform=sanitize_log_message(entry.platform).strip() or "Generic",
        format_label=sanitize_log_message(entry.format_label).strip(),
        filesize_bytes=max(0, entry.filesize_bytes),
        elapsed_seconds=max(0.0, entry.elapsed_seconds),
        status=entry.status,
        timestamp_iso=entry.timestamp_iso,
        time_range_label=entry.time_range_label,
        error_code=sanitize_log_message(entry.error_code).strip(),
    )
    history_list: list[SessionHistoryEntry] = list(target.get("session_history") or [])
    history_list.insert(0, sanitized_entry)
    target["session_history"] = history_list[: active_limits.max_history_entries]


def clear_session_history(state: MutableMapping[str, Any] | None = None) -> None:
    """Clear all in-memory operation history entries for the current session."""
    target = init_session_state(state)
    target["session_history"] = []


def cleanup_batch_state(
    state: MutableMapping[str, Any] | None = None,
    file_manager: TempFileManager | None = None,
) -> int:
    """Clean up temporary workspaces created by a completed batch and clear `batch_summary`."""
    target = init_session_state(state)
    manager = file_manager or TempFileManager()
    summary: BatchSummary | None = target.get("batch_summary")
    removed = 0
    if summary is not None:
        for item in summary.items:
            if item.result is not None:
                if manager.cleanup_operation(item.result.operation_id, force=True):
                    removed += 1
    target["batch_summary"] = None
    target["batch_cancel_requested"] = False
    return removed


def reset_media_state(
    state: MutableMapping[str, Any] | None = None,
    file_manager: TempFileManager | None = None,
    *,
    clear_url: bool = False,
) -> None:
    """Invalidate metadata/formats/files and immediately purge prior temporary artifacts."""
    target = init_session_state(state)
    manager = file_manager or TempFileManager()

    prev_op_id = target.get("active_operation_id")
    if isinstance(prev_op_id, str) and prev_op_id:
        manager.cleanup_operation(prev_op_id, force=True)

    manager.cleanup_expired_files()

    if clear_url:
        target["source_url"] = ""
    target["last_inspected_url"] = ""
    target["media_info"] = None
    target["selected_format"] = None
    target["clip_start"] = ""
    target["clip_end"] = ""
    target["operation_status"] = OperationStatus.IDLE
    target["progress_data"] = dict(STATE_DEFAULTS["progress_data"])
    target["output_file"] = None
    target["error_message"] = None
    target["active_operation_id"] = None
    target["download_elapsed"] = None


def sync_url_invalidation(
    new_url: str,
    state: MutableMapping[str, Any] | None = None,
    file_manager: TempFileManager | None = None,
) -> bool:
    """Detect changes to `source_url` and reset derived inspection/download state."""
    target = init_session_state(state)
    normalized_new = new_url.strip()
    last_url = str(target.get("last_inspected_url") or "").strip()

    target["source_url"] = normalized_new
    if last_url and normalized_new != last_url:
        reset_media_state(target, file_manager=file_manager, clear_url=False)
        target["source_url"] = normalized_new
        return True
    return False


def inspect_media_url(
    url: str,
    *,
    state: MutableMapping[str, Any] | None = None,
    file_manager: TempFileManager | None = None,
    limits: OperationalLimits | None = None,
    resolve_dns: bool = True,
) -> MediaMetadata | None:
    """Execute Stage 1 (Identifying media) and update the session state machine."""
    target = init_session_state(state)
    reset_media_state(target, file_manager=file_manager, clear_url=False)
    cleaned_url = url.strip()
    target["source_url"] = cleaned_url
    target["operation_status"] = OperationStatus.IDENTIFYING

    try:
        metadata = get_info(cleaned_url, limits=limits, resolve_dns=resolve_dns)
        target["media_info"] = metadata
        target["last_inspected_url"] = cleaned_url
        target["operation_status"] = OperationStatus.READY

        mode = target.get("download_mode") or DownloadMode.VIDEO
        mode_formats = filter_formats(metadata.available_formats, mode=mode)
        if mode_formats:
            target["selected_format"] = mode_formats[0].format_id
        elif metadata.available_formats:
            target["selected_format"] = metadata.available_formats[0].format_id
        return metadata
    except Exception as exc:
        mapped = exc if isinstance(exc, OmniFetchError) else classify_yt_dlp_error(exc)
        target["operation_status"] = OperationStatus.ERROR
        target["error_message"] = mapped.to_structured()
        return None


def execute_media_download(
    format_option: FormatOption,
    *,
    state: MutableMapping[str, Any] | None = None,
    file_manager: TempFileManager | None = None,
    limits: OperationalLimits | None = None,
    on_progress: Any | None = None,
    resolve_dns: bool = True,
) -> Path | None:
    """Execute Stages 2–4 (Downloading -> Processing -> Completed) and record sanitized history."""
    target = init_session_state(state)
    manager = file_manager or TempFileManager()
    media_info: MediaMetadata | None = target.get("media_info")
    url = str(target.get("source_url") or "")

    prev_op_id = target.get("active_operation_id")
    if isinstance(prev_op_id, str) and prev_op_id:
        manager.cleanup_operation(prev_op_id, force=True)
        target["active_operation_id"] = None
        target["output_file"] = None

    target["error_message"] = None

    def _state_progress_bridge(pstate: ProgressState) -> None:
        target["operation_status"] = pstate.stage
        target["progress_data"] = {
            "downloaded_bytes": pstate.downloaded_bytes,
            "total_bytes": pstate.total_bytes,
            "speed_bps": pstate.speed_bps,
            "eta_seconds": pstate.eta_seconds,
            "percent": pstate.percent,
            "postprocessor_name": pstate.postprocessor_name,
            "message": pstate.message,
        }
        if on_progress is not None:
            on_progress(pstate)

    try:
        time_range = validate_time_range(
            str(target.get("clip_start") or ""),
            str(target.get("clip_end") or ""),
            media_duration=media_info.duration_seconds if media_info else None,
            is_live=media_info.is_live if media_info else False,
        )
        result = download_media(
            url=url,
            format_option=format_option,
            title=media_info.title if media_info else "omnifetch_media",
            time_range=time_range,
            file_manager=manager,
            limits=limits,
            progress_callback=_state_progress_bridge,
            resolve_dns=resolve_dns,
        )
        target["active_operation_id"] = result.operation_id
        target["output_file"] = result.output_path
        target["download_elapsed"] = result.elapsed_seconds
        target["operation_status"] = OperationStatus.COMPLETED

        record_session_history(
            SessionHistoryEntry(
                operation_id=result.operation_id[:8],
                title=media_info.title if media_info else result.filename,
                platform=media_info.platform if media_info else "Generic",
                format_label=format_option.label,
                filesize_bytes=result.filesize_bytes,
                elapsed_seconds=result.elapsed_seconds,
                status=OperationStatus.COMPLETED,
                timestamp_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                time_range_label=time_range.label if time_range else "Full",
            ),
            state=target,
            limits=limits,
        )
        return result.output_path
    except Exception as exc:
        mapped = exc if isinstance(exc, OmniFetchError) else classify_yt_dlp_error(exc)
        target["operation_status"] = OperationStatus.ERROR
        target["error_message"] = mapped.to_structured()
        target["output_file"] = None
        record_session_history(
            SessionHistoryEntry(
                operation_id="error",
                title=media_info.title if media_info else "Failed Operation",
                platform=media_info.platform if media_info else "Generic",
                format_label=format_option.label,
                filesize_bytes=0,
                elapsed_seconds=0.0,
                status=OperationStatus.ERROR,
                timestamp_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                time_range_label="Full",
                error_code=mapped.category.value,
            ),
            state=target,
            limits=limits,
        )
        return None


def execute_batch_from_state(
    urls: Sequence[str],
    *,
    mode: DownloadMode = DownloadMode.VIDEO,
    state: MutableMapping[str, Any] | None = None,
    file_manager: TempFileManager | None = None,
    limits: OperationalLimits | None = None,
    cancel_check: Callable[[], bool] | None = None,
    resolve_dns: bool = True,
) -> BatchSummary | None:
    """Execute a controlled sequential batch updating `batch_summary` and `session_history`."""
    target = init_session_state(state)
    manager = file_manager or TempFileManager()
    cleanup_batch_state(target, file_manager=manager)
    target["error_message"] = None

    try:
        summary = execute_batch_download(
            urls,
            mode=mode,
            file_manager=manager,
            limits=limits,
            cancel_check=cancel_check,
            resolve_dns=resolve_dns,
        )
        target["batch_summary"] = summary
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        for item in summary.items:
            if item.succeeded and item.result is not None:
                record_session_history(
                    SessionHistoryEntry(
                        operation_id=item.result.operation_id[:8],
                        title=item.title or item.result.filename,
                        platform="Batch",
                        format_label=item.result.format_used.label,
                        filesize_bytes=item.result.filesize_bytes,
                        elapsed_seconds=item.result.elapsed_seconds,
                        status=OperationStatus.COMPLETED,
                        timestamp_iso=now_iso,
                        time_range_label="Full",
                    ),
                    state=target,
                    limits=limits,
                )
            elif item.error is not None:
                record_session_history(
                    SessionHistoryEntry(
                        operation_id=f"batch-{item.index}",
                        title=item.title or f"Batch Item #{item.index}",
                        platform="Batch",
                        format_label=mode.value,
                        filesize_bytes=0,
                        elapsed_seconds=0.0,
                        status=OperationStatus.ERROR,
                        timestamp_iso=now_iso,
                        time_range_label="Full",
                        error_code=item.error.category.value,
                    ),
                    state=target,
                    limits=limits,
                )
        return summary
    except Exception as exc:
        mapped = exc if isinstance(exc, OmniFetchError) else classify_yt_dlp_error(exc)
        target["operation_status"] = OperationStatus.ERROR
        target["error_message"] = mapped.to_structured()
        target["batch_summary"] = None
        return None


def _render_diagnostics_panel(
    report: DiagnosticReport,
    *,
    last_status: str = "None",
) -> None:
    """Render Startup Check status, sanitized summary table, and Markdown report export."""
    if report.all_passed:
        with st.expander("Local Environment Diagnostics (5/5 Checks Passed)", expanded=False):
            summary = export_diagnostic_summary(report, last_operation_status=last_status)
            st.table([{"Check": k, "Status": v} for k, v in summary.items()])
            st.download_button(
                label="Export Diagnostic Report (.md)",
                data=format_diagnostic_markdown(report, last_operation_status=last_status),
                file_name="omnifetch-diagnostics.md",
                mime="text/markdown",
            )
        return

    st.error(
        "Mandatory runtime prerequisites failed. "
        "Media URL submission is blocked until the local environment is fixed."
    )
    for chk in report.checks:
        icon = "OK" if chk.passed else "FAIL"
        st.markdown(f"**[{icon}] {chk.name}** — {chk.detail}")
        if not chk.passed and chk.remediation:
            st.info(f"How to fix: `{chk.remediation}`")


def _format_audio_language_cell(fmt: FormatOption) -> str:
    """Format the audio language column for the stream selection table."""
    if fmt.audio_language and fmt.is_original_audio:
        return f"{fmt.audio_language.upper()} (Original)"
    if fmt.audio_language:
        return fmt.audio_language.upper()
    if fmt.is_original_audio:
        return "Original"
    return "Default"


def _render_stage_stepper(current_status: OperationStatus) -> None:
    """Render the 4-stage visual pipeline indicator."""
    stages = [
        (OperationStatus.IDENTIFYING, "1. Identifying Media"),
        (OperationStatus.DOWNLOADING, "2. Downloading"),
        (OperationStatus.PROCESSING, "3. Processing (FFmpeg)"),
        (OperationStatus.COMPLETED, "4. Completed"),
    ]
    stage_order = {
        OperationStatus.IDLE: 0,
        OperationStatus.IDENTIFYING: 1,
        OperationStatus.READY: 1,
        OperationStatus.DOWNLOADING: 2,
        OperationStatus.PROCESSING: 3,
        OperationStatus.COMPLETED: 4,
        OperationStatus.ERROR: -1,
    }
    current_rank = stage_order.get(current_status, 0)
    cols = st.columns(4)
    for idx, (_st_enum, label) in enumerate(stages, start=1):
        with cols[idx - 1]:
            if current_rank > idx or (current_status == OperationStatus.COMPLETED and idx == 4):
                st.success(f"✓ {label}")
            elif current_rank == idx:
                st.info(f"● {label}")
            else:
                st.caption(f"○ {label}")


def _render_session_history_panel(state: MutableMapping[str, Any]) -> None:
    """Render the temporary in-memory session history expander (cleared on app restart)."""
    history: list[SessionHistoryEntry] = list(state.get("session_history") or [])
    if not history:
        return
    with st.expander(
        f"Session History — Temporary In-Memory ({len(history)} operations)",
        expanded=False,
    ):
        rows = [
            {
                "ID": h.operation_id,
                "Title": h.title,
                "Platform": h.platform,
                "Format": h.format_label,
                "Segment": h.time_range_label,
                "Status": h.status.value,
                "Error Code": h.error_code or "-",
                "Size (MB)": f"{h.filesize_bytes / (1024 * 1024):.2f}",
                "Duration (s)": f"{h.elapsed_seconds:.1f}",
                "Timestamp": h.timestamp_iso,
            }
            for h in history
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        if st.button("Clear Session History", key="btn_clear_history"):
            clear_session_history(state)
            st.rerun()


def _render_batch_panel(
    state: MutableMapping[str, Any],
    *,
    file_manager: TempFileManager,
    limits: OperationalLimits,
) -> None:
    """Render the sequential controlled batch workflow (up to 5 URLs) and per-item download buttons."""
    with st.expander(
        f"Sequential Batch Processing (Up to {limits.max_batch_urls} Public URLs)",
        expanded=bool(state.get("batch_summary") is not None),
    ):
        st.caption(
            "Batch execution runs sequentially to preserve global disk quotas, independent Download Leases, "
            "and failure isolation. Cancelling stops after the current item completes."
        )
        urls_text = st.text_area(
            "Public Media URLs (one per line)",
            value=str(state.get("batch_urls_text") or ""),
            placeholder="https://...\nhttps://...",
            height=110,
            key="batch_urls_textarea",
        )
        state["batch_urls_text"] = urls_text

        b_col1, b_col2, b_col3 = st.columns([2, 2, 2])
        with b_col1:
            selected_batch_mode = st.radio(
                "Batch Output Mode",
                options=[DownloadMode.VIDEO, DownloadMode.AUDIO_ONLY],
                format_func=lambda m: "Video + Audio" if m == DownloadMode.VIDEO else "Audio-Only",
                horizontal=True,
                key="batch_mode_radio",
            )
            batch_mode = (
                selected_batch_mode
                if isinstance(selected_batch_mode, DownloadMode)
                else DownloadMode.VIDEO
            )
            state["batch_mode"] = batch_mode
        with b_col2:
            state["batch_cancel_requested"] = st.checkbox(
                "Stop batch after current item finishes",
                value=bool(state.get("batch_cancel_requested")),
                key="batch_cancel_checkbox",
            )
        with b_col3:
            run_batch_clicked = st.button("Start Sequential Batch", key="btn_run_batch")
            clear_batch_clicked = st.button("Clear Batch & Temp Files", key="btn_clear_batch")

        if clear_batch_clicked:
            cleanup_batch_state(state, file_manager=file_manager)
            state["batch_urls_text"] = ""
            st.rerun()

        if run_batch_clicked:
            raw_lines = [line.strip() for line in urls_text.splitlines() if line.strip()]
            with st.spinner("Running sequential batch items..."):
                execute_batch_from_state(
                    raw_lines,
                    mode=batch_mode,
                    state=state,
                    file_manager=file_manager,
                    limits=limits,
                    cancel_check=lambda: bool(state.get("batch_cancel_requested")),
                )
            st.rerun()

        summary: BatchSummary | None = state.get("batch_summary")
        if summary is not None:
            total_mib = summary.total_bytes / (1024 * 1024)
            st.info(
                f"Batch Summary: {summary.succeeded_count} succeeded, "
                f"{summary.failed_count} failed, "
                f"cancelled={summary.cancelled}, total={total_mib:.2f} MB."
            )
            for item in summary.items:
                if item.succeeded and item.result is not None:
                    item_mib = item.result.filesize_bytes / (1024 * 1024)
                    st.success(f"#{item.index} Ready: {item.title} ({item_mib:.2f} MB)")
                    st.download_button(
                        label=f"Download #{item.index}: {item.result.filename}",
                        data=file_manager.build_transfer_loader(
                            item.result.operation_id, item.result.output_path
                        ),
                        file_name=item.result.filename,
                        mime=get_mime_type(item.result.output_path),
                        key=f"batch_dl_{item.result.operation_id}",
                    )
                else:
                    err_msg = (
                        item.error.user_message if item.error is not None else "Processing failed."
                    )
                    st.error(f"#{item.index} Failed ({item.url}): {err_msg}")


def _render_single_media_workspace(
    media_info: MediaMetadata,
    *,
    state: MutableMapping[str, Any],
    file_manager: TempFileManager,
    limits: OperationalLimits,
) -> None:
    """Render metadata details, stream table, post-download trim controls, and download button."""
    st.divider()
    col_thumb, col_meta = st.columns([1, 2])
    with col_thumb:
        if media_info.thumbnail_url:
            st.image(media_info.thumbnail_url, use_container_width=True)
        else:
            st.info("Thumbnail unavailable for this stream.")

    with col_meta:
        st.subheader(media_info.title)
        m_col1, m_col2, m_col3 = st.columns(3)
        m_col1.metric("Platform", media_info.platform)
        m_col2.metric(
            "Duration",
            format_duration(media_info.duration_seconds, is_live=media_info.is_live),
        )
        m_col3.metric("Channel / Author", media_info.uploader or "Unknown")

        mode_label = st.radio(
            "Extraction Mode",
            options=[DownloadMode.VIDEO, DownloadMode.AUDIO_ONLY],
            format_func=lambda m: "Video + Audio (Original Profile / Remux)"
            if m == DownloadMode.VIDEO
            else "Audio-Only (Original / MP3)",
            horizontal=True,
            index=0 if state.get("download_mode") == DownloadMode.VIDEO else 1,
        )
        if mode_label != state.get("download_mode"):
            state["download_mode"] = mode_label
            filtered = filter_formats(media_info.available_formats, mode=mode_label)
            state["selected_format"] = filtered[0].format_id if filtered else None

        filtered_formats = filter_formats(
            media_info.available_formats,
            mode=state["download_mode"],
        )

        if not filtered_formats:
            st.warning("No compatible streams remain within configured size/duration limits.")
            return

        table_rows = [
            {
                "Quality": fmt.resolution,
                "Container": fmt.container.upper(),
                "Video Codec": fmt.video_codec.upper(),
                "Audio Codec": fmt.audio_codec.upper(),
                "Audio Language": _format_audio_language_cell(fmt),
                "FFmpeg Operation": "Re-encode" if fmt.requires_reencode else "Remux / Copy",
                "Est. Size": fmt.filesize_human,
            }
            for fmt in filtered_formats
        ]
        st.dataframe(table_rows, use_container_width=True, hide_index=True)

        format_map = {fmt.format_id: fmt for fmt in filtered_formats}
        options_list = list(format_map.keys())
        selected_raw = state.get("selected_format")
        default_fmt_id: str = (
            selected_raw
            if isinstance(selected_raw, str) and selected_raw in format_map
            else filtered_formats[0].format_id
        )
        chosen_fmt_id = st.selectbox(
            "Select Output Stream",
            options=options_list,
            index=options_list.index(default_fmt_id),
            format_func=lambda fid: (
                f"{format_map[fid].label} — Size: {format_map[fid].filesize_human}"
            ),
        )
        active_fmt_id = chosen_fmt_id if isinstance(chosen_fmt_id, str) else default_fmt_id
        state["selected_format"] = active_fmt_id
        chosen_format = format_map[active_fmt_id]

        with st.expander("Optional Post-Download Trim (Start / End)", expanded=False):
            if media_info.is_live:
                st.caption("Temporal trimming is unavailable for live streams.")
            else:
                st.caption(
                    "Note: Full media is downloaded first and trimmed locally via fast FFmpeg stream copy "
                    "(-c copy); boundaries snap to nearest keyframes rather than frame-exact cuts."
                )
                c_start, c_end = st.columns(2)
                with c_start:
                    state["clip_start"] = st.text_input(
                        "Start Time (SS, MM:SS, or HH:MM:SS)",
                        value=str(state.get("clip_start") or ""),
                        placeholder="00:00",
                    )
                with c_end:
                    state["clip_end"] = st.text_input(
                        "End Time (SS, MM:SS, or HH:MM:SS)",
                        value=str(state.get("clip_end") or ""),
                        placeholder="01:30",
                    )

        start_dl = st.button("Download and Process Media", type="primary", use_container_width=True)

    if start_dl:
        progress_bar = st.progress(0.0, text="Starting operation...")
        status_box = st.empty()

        def _update_ui_progress(pstate: ProgressState) -> None:
            if pstate.stage == OperationStatus.DOWNLOADING:
                dl_mib = pstate.downloaded_bytes / (1024 * 1024)
                spd_str = format_speed(pstate.speed_bps)
                eta_str = f"{int(pstate.eta_seconds)}s" if pstate.eta_seconds is not None else "--"
                if pstate.percent is not None and pstate.total_bytes:
                    tot_mib = pstate.total_bytes / (1024 * 1024)
                    fraction = min(0.99, max(0.0, pstate.percent / 100.0))
                    progress_bar.progress(
                        fraction,
                        text=(
                            f"[Stage 2/4 • Downloading] {pstate.percent:.1f}% "
                            f"({dl_mib:.1f}/{tot_mib:.1f} MB) • {spd_str} • ETA: {eta_str}"
                        ),
                    )
                else:
                    progress_bar.progress(
                        0.5,
                        text=f"[Stage 2/4 • Downloading] {dl_mib:.1f} MB received • {spd_str}",
                    )
            elif pstate.stage == OperationStatus.PROCESSING:
                progress_bar.progress(
                    0.99,
                    text=f"[Stage 3/4 • Processing] {pstate.message}",
                )
                status_box.info("FFmpeg running (stream merge/remuxing/trimming). Please wait...")
            elif pstate.stage == OperationStatus.COMPLETED:
                progress_bar.progress(1.0, text=f"[Stage 4/4 • Completed] {pstate.message}")
                status_box.empty()

        execute_media_download(
            chosen_format,
            state=state,
            file_manager=file_manager,
            limits=limits,
            on_progress=_update_ui_progress,
        )
        st.rerun()

    # Stage 4: Display lease-protected transfer button
    output_file: Path | None = state.get("output_file")
    active_op_id: str | None = state.get("active_operation_id")
    if (
        state.get("operation_status") == OperationStatus.COMPLETED
        and isinstance(output_file, Path)
        and output_file.exists()
        and isinstance(active_op_id, str)
    ):
        final_mib = output_file.stat().st_size / (1024 * 1024)
        elapsed_sec = state.get("download_elapsed") or 0.0
        st.success(
            f"Completed in {elapsed_sec:.1f}s! "
            f"Ready for transfer: `{output_file.name}` ({final_mib:.2f} MB)."
        )
        transfer_loader = file_manager.build_transfer_loader(active_op_id, output_file)
        st.download_button(
            label=f"Save {output_file.name} ({final_mib:.2f} MB)",
            data=transfer_loader,
            file_name=output_file.name,
            mime=get_mime_type(output_file),
            type="primary",
            use_container_width=True,
        )


def main() -> None:
    """Main Streamlit entrypoint for OmniFetch."""
    st.set_page_config(
        page_title="OmniFetch — Local-First Media Extraction",
        page_icon="🎬",
        layout="wide",
    )

    limits = OperationalLimits()
    file_manager = TempFileManager(ttl_seconds=limits.temp_ttl_seconds)
    state = init_session_state()

    st.title("OmniFetch")
    st.caption(
        "Local-First public media extraction, trimming, and remuxing via yt-dlp, FFmpeg, and FFprobe "
        "(zero cookies/credentials stored)."
    )

    diag_report = run_startup_diagnostics(
        temp_root=file_manager.base_dir,
        limits=limits,
    )
    _render_diagnostics_panel(
        diag_report,
        last_status=str(state.get("operation_status", OperationStatus.IDLE).value),
    )

    if not diag_report.all_passed:
        st.stop()

    col_input, col_inspect, col_reset = st.columns([6, 2, 2])
    with col_input:
        url_input = st.text_input(
            "Public Media URL",
            value=str(state.get("source_url") or ""),
            placeholder="https://www.youtube.com/watch?v=... or any supported public media link",
            label_visibility="collapsed",
        )
    with col_inspect:
        inspect_clicked = st.button("Inspect Media", type="primary", use_container_width=True)
    with col_reset:
        reset_clicked = st.button("New Query", use_container_width=True)

    if reset_clicked:
        reset_media_state(state, file_manager=file_manager, clear_url=True)
        st.rerun()

    # Invalidate cached metadata whenever the URL input diverges from last_inspected_url
    sync_url_invalidation(url_input, state, file_manager=file_manager)

    if inspect_clicked:
        if not url_input.strip():
            state["operation_status"] = OperationStatus.ERROR
            state["error_message"] = StructuredError(
                category=OmniFetchError.category,
                user_message="Enter a valid public URL before inspecting.",
            )
        else:
            with st.spinner("1/4 Identifying media and enumerating streams..."):
                inspect_media_url(
                    url_input,
                    state=state,
                    file_manager=file_manager,
                    limits=limits,
                )

    current_status: OperationStatus = state["operation_status"]
    _render_stage_stepper(current_status)

    err_obj: StructuredError | None = state.get("error_message")
    if err_obj is not None:
        st.error(f"**{err_obj.user_message}**")
        if err_obj.technical_detail:
            with st.expander("Sanitized Technical Details"):
                st.code(err_obj.technical_detail, language="text")

    _render_session_history_panel(state)

    media_info: MediaMetadata | None = state.get("media_info")
    if media_info is not None:
        _render_single_media_workspace(
            media_info,
            state=state,
            file_manager=file_manager,
            limits=limits,
        )

    _render_batch_panel(state, file_manager=file_manager, limits=limits)


if __name__ == "__main__":
    main()
