"""Tests for `app.py` state machine, sanitized session history, sequential batches, and Streamlit UI."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import app
from core.models import (
    BatchItemResult,
    BatchSummary,
    DownloadMode,
    DownloadResult,
    ErrorCategory,
    FormatOption,
    MediaMetadata,
    OperationalLimits,
    OperationStatus,
    OutputProfile,
    ProgressState,
    SessionHistoryEntry,
)
from utils.error_handler import InvalidURLError, ResourceLimitExceededError
from utils.file_manager import TempFileManager


def _sample_metadata() -> MediaMetadata:
    v_opt = FormatOption(
        format_id="video_137_original",
        yt_dlp_format="137+140",
        label="1080p (MP4 • H264/AAC)",
        mode=DownloadMode.VIDEO,
        profile=OutputProfile.ORIGINAL,
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        resolution="1080p",
        height=1080,
        filesize_bytes=10 * 1024 * 1024,
        requires_merge=True,
    )
    a_opt = FormatOption(
        format_id="audio_140_original",
        yt_dlp_format="140",
        label="Audio 128 kbps (M4A • AAC)",
        mode=DownloadMode.AUDIO_ONLY,
        profile=OutputProfile.ORIGINAL,
        container="m4a",
        video_codec="none",
        audio_codec="aac",
        resolution="Audio-Only",
        abr_kbps=128.0,
        filesize_bytes=2 * 1024 * 1024,
    )
    return MediaMetadata(
        media_id="vid_123",
        title="Demo Video",
        webpage_url="https://93.184.216.34/watch?v=123",
        platform="Youtube",
        extractor_key="Youtube",
        duration_seconds=125.0,
        thumbnail_url="https://93.184.216.34/thumb.jpg",
        uploader="Test Author",
        is_live=False,
        available_formats=(v_opt, a_opt),
    )


class TestAppStateMachine:
    """Validate state transitions, URL invalidation, FIFO session history, and sequential batches."""

    def test_format_helpers(self) -> None:
        assert app.format_duration(None, is_live=True) == "LIVE (Continuous Stream)"
        assert app.format_duration(None, is_live=False) == "Unknown"
        assert app.format_duration(125.0) == "02:05"
        assert app.format_duration(3661.0) == "01:01:01"
        assert app.format_speed(None) == "-- MB/s"
        assert app.format_speed(500 * 1024) == "500 KB/s"
        assert app.format_speed(2 * 1024 * 1024) == "2.00 MB/s"
        assert app.get_mime_type(Path("video.mp4")) == "video/mp4"
        assert app.get_mime_type(Path("audio.mp3")) == "audio/mpeg"
        assert (
            app._format_audio_language_cell(
                FormatOption(
                    format_id="v1",
                    yt_dlp_format="137+140-1",
                    label="1080p",
                    mode=DownloadMode.VIDEO,
                    profile=OutputProfile.ORIGINAL,
                    container="mp4",
                    video_codec="h264",
                    audio_codec="aac",
                    resolution="1080p",
                    audio_language="pt",
                    is_original_audio=True,
                )
            )
            == "PT (Original)"
        )
        assert (
            app._format_audio_language_cell(
                FormatOption(
                    format_id="a2",
                    yt_dlp_format="140-0",
                    label="Audio EN",
                    mode=DownloadMode.AUDIO_ONLY,
                    profile=OutputProfile.ORIGINAL,
                    container="m4a",
                    video_codec="none",
                    audio_codec="aac",
                    resolution="Audio-Only",
                    audio_language="en",
                    is_original_audio=False,
                )
            )
            == "EN"
        )
        assert (
            app._format_audio_language_cell(
                FormatOption(
                    format_id="v2",
                    yt_dlp_format="1",
                    label="Auto",
                    mode=DownloadMode.VIDEO,
                    profile=OutputProfile.ORIGINAL,
                    container="mp4",
                    video_codec="h264",
                    audio_codec="aac",
                    resolution="720p",
                    audio_language=None,
                    is_original_audio=True,
                )
            )
            == "Original"
        )
        assert (
            app._format_audio_language_cell(
                FormatOption(
                    format_id="v3",
                    yt_dlp_format="2",
                    label="Auto",
                    mode=DownloadMode.VIDEO,
                    profile=OutputProfile.ORIGINAL,
                    container="mp4",
                    video_codec="h264",
                    audio_codec="aac",
                    resolution="720p",
                )
            )
            == "Default"
        )

    def test_inspect_download_history_and_url_change_invalidation(self, tmp_path: Path) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "ui_store")
        state: dict[str, Any] = {}
        app.init_session_state(state)
        assert state["operation_status"] == OperationStatus.IDLE

        meta = _sample_metadata()
        with patch("app.get_info", return_value=meta):
            res_meta = app.inspect_media_url(
                "https://93.184.216.34/watch?v=123",
                state=state,
                file_manager=mgr,
                resolve_dns=False,
            )
        assert res_meta is meta
        assert state["operation_status"] == OperationStatus.READY
        assert state["selected_format"] == "video_137_original"

        op_id, op_dir = mgr.create_operation_dir(str(uuid.uuid4()))
        out_path = op_dir / "video_final.mp4"
        out_path.write_bytes(b"mp4_content")
        mgr.acquire_lease(op_id)

        def _fake_download_media(**kwargs: Any) -> DownloadResult:
            cb = kwargs.get("progress_callback")
            if cb:
                cb(
                    ProgressState(
                        stage=OperationStatus.DOWNLOADING,
                        downloaded_bytes=512,
                        total_bytes=1024,
                        percent=50.0,
                    )
                )
                cb(
                    ProgressState(
                        stage=OperationStatus.PROCESSING,
                        downloaded_bytes=1024,
                        total_bytes=1024,
                        percent=100.0,
                        message="FFmpeg...",
                    )
                )
            return DownloadResult(
                operation_id=op_id,
                output_path=out_path,
                filename=out_path.name,
                filesize_bytes=out_path.stat().st_size,
                elapsed_seconds=1.2,
                format_used=meta.available_formats[0],
                time_range=kwargs.get("time_range"),
            )

        state["clip_start"] = "00:05"
        state["clip_end"] = "00:25"
        recorded_ui_progress: list[ProgressState] = []
        with patch("app.download_media", side_effect=_fake_download_media):
            dl_path = app.execute_media_download(
                meta.available_formats[0],
                state=state,
                file_manager=mgr,
                on_progress=recorded_ui_progress.append,
                resolve_dns=False,
            )

        assert dl_path == out_path
        assert state["operation_status"] == OperationStatus.COMPLETED
        assert state["active_operation_id"] == op_id
        assert len(recorded_ui_progress) == 2
        assert out_path.exists()

        # Session history records sanitized entry with temporal range
        assert len(state["session_history"]) == 1
        assert state["session_history"][0].title == "Demo Video"
        assert state["session_history"][0].time_range_label == "00:05 - 00:25"

        # Changing URL invalidates temporary artifacts but preserves in-memory session history
        invalidated = app.sync_url_invalidation(
            "https://93.184.216.34/watch?v=NEW_VIDEO",
            state=state,
            file_manager=mgr,
        )
        assert invalidated is True
        assert state["media_info"] is None
        assert state["output_file"] is None
        assert state["active_operation_id"] is None
        assert state["operation_status"] == OperationStatus.IDLE
        assert not out_path.exists()
        assert not op_dir.exists()
        assert len(state["session_history"]) == 1

        app.clear_session_history(state)
        assert state["session_history"] == []

    def test_session_history_fifo_eviction_sanitization_failure_and_isolation(
        self, tmp_path: Path
    ) -> None:
        """
        Cover all SessionHistory audit guarantees:
        - FIFO cap of 20 entries evicts oldest entry on the 21st insertion;
        - Session isolation keeps state A and state B strictly separate;
        - Failed download logs sanitized error_code without credentials or tokens;
        - Overlong / dirty titles are sanitized and capped cleanly.
        """
        limits = OperationalLimits(max_history_entries=20)
        state_a: dict[str, Any] = {}
        state_b: dict[str, Any] = {}
        app.init_session_state(state_a)
        app.init_session_state(state_b)

        for idx in range(25):
            app.record_session_history(
                SessionHistoryEntry(
                    operation_id=f"op-{idx:04d}-ignored-suffix",
                    title=f"Item {idx} https://user:pw@cdn.example.com/v?token=secret_{idx} "
                    + ("Z" * 160),
                    platform="Youtube",
                    format_label="1080p",
                    filesize_bytes=1024,
                    elapsed_seconds=1.0,
                    status=OperationStatus.COMPLETED,
                    timestamp_iso="2026-11-11 00:00:00 UTC",
                ),
                state=state_a,
                limits=limits,
            )

        assert len(state_a["session_history"]) == 20
        newest = state_a["session_history"][0]
        oldest_kept = state_a["session_history"][-1]
        assert newest.operation_id == "op-0024-"
        assert oldest_kept.operation_id == "op-0005-"
        assert "pw" not in newest.title
        assert "secret_24" not in newest.title
        assert len(newest.title) <= 120
        assert state_b["session_history"] == []

        # Failure inside execute_media_download records error_code safely
        mgr = TempFileManager(base_dir=tmp_path / "hist_err")
        state_a["media_info"] = _sample_metadata()
        with patch("app.download_media", side_effect=InvalidURLError("bad url")):
            res = app.execute_media_download(
                _sample_metadata().available_formats[0],
                state=state_a,
                file_manager=mgr,
                resolve_dns=False,
            )
        assert res is None
        err_entry = state_a["session_history"][0]
        assert err_entry.status == OperationStatus.ERROR
        assert err_entry.error_code == ErrorCategory.INVALID_URL.value

    def test_execute_batch_from_state_cleanup_and_error_handling(self, tmp_path: Path) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "batch_ui_store")
        state: dict[str, Any] = {}
        app.init_session_state(state)
        meta = _sample_metadata()
        op_id, op_dir = mgr.create_operation_dir()
        item1_file = op_dir / "item1.mp4"
        item1_file.write_bytes(b"X" * 4096)
        fake_res = DownloadResult(
            operation_id=op_id,
            output_path=item1_file,
            filename="item1.mp4",
            filesize_bytes=4096,
            elapsed_seconds=0.5,
            format_used=meta.available_formats[0],
        )
        fake_summary = BatchSummary(
            items=(
                BatchItemResult(
                    index=1,
                    url="https://93.184.216.34/1",
                    succeeded=True,
                    title="Batch Video 1",
                    result=fake_res,
                ),
                BatchItemResult(
                    index=2,
                    url="http://127.0.0.1/blocked",
                    succeeded=False,
                    error=InvalidURLError("Blocked").to_structured(),
                ),
            ),
            cancelled=False,
        )
        with patch("app.execute_batch_download", return_value=fake_summary):
            summary = app.execute_batch_from_state(
                ["https://93.184.216.34/1", "http://127.0.0.1/blocked"],
                state=state,
                file_manager=mgr,
                resolve_dns=False,
            )
        assert summary is fake_summary
        assert state["batch_summary"] is fake_summary
        assert len(state["session_history"]) == 2
        assert op_dir.exists()

        # Cleaning batch state deletes all batch directories
        removed = app.cleanup_batch_state(state, file_manager=mgr)
        assert removed == 1
        assert not op_dir.exists()
        assert state["batch_summary"] is None

        with patch(
            "app.execute_batch_download",
            side_effect=ResourceLimitExceededError("Batch limit exceeded"),
        ):
            failed_batch = app.execute_batch_from_state(
                ["https://93.184.216.34/1"] * 10,
                state=state,
                file_manager=mgr,
                resolve_dns=False,
            )
        assert failed_batch is None
        assert state["operation_status"] == OperationStatus.ERROR
        assert state["error_message"].category == ErrorCategory.RESOURCE_LIMIT

    def test_inspect_and_download_handle_errors_cleanly(self, tmp_path: Path) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "ui_err")
        state: dict[str, Any] = {}
        app.init_session_state(state)

        with patch("app.get_info", side_effect=InvalidURLError()):
            res = app.inspect_media_url("http://127.0.0.1/bad", state=state, file_manager=mgr)
        assert res is None
        assert state["operation_status"] == OperationStatus.ERROR
        assert state["error_message"].category == ErrorCategory.INVALID_URL

    def test_multi_session_isolation_and_concurrency_guard(self, tmp_path: Path) -> None:
        import pytest

        from utils.validators import ConcurrencyGuard, operation_slot

        shared_mgr = TempFileManager(base_dir=tmp_path / "multi_session_store")
        session_a: dict[str, Any] = {}
        session_b: dict[str, Any] = {}
        app.init_session_state(session_a)
        app.init_session_state(session_b)

        op_a, dir_a = shared_mgr.create_operation_dir()
        file_a = dir_a / "session_a_video.mp4"
        file_a.write_bytes(b"video_a")
        shared_mgr.acquire_lease(op_a)
        session_a["source_url"] = "https://93.184.216.34/a"
        session_a["last_inspected_url"] = "https://93.184.216.34/a"
        session_a["active_operation_id"] = op_a
        session_a["output_file"] = file_a
        session_a["media_info"] = _sample_metadata()

        op_b, dir_b = shared_mgr.create_operation_dir()
        file_b = dir_b / "session_b_video.mp4"
        file_b.write_bytes(b"video_b")
        shared_mgr.acquire_lease(op_b)
        session_b["source_url"] = "https://93.184.216.34/b"
        session_b["last_inspected_url"] = "https://93.184.216.34/b"
        session_b["active_operation_id"] = op_b
        session_b["output_file"] = file_b
        session_b["media_info"] = _sample_metadata()

        app.reset_media_state(session_a, file_manager=shared_mgr, clear_url=True)
        assert not dir_a.exists()
        assert session_a["media_info"] is None
        assert dir_b.exists()
        assert file_b.exists()
        assert session_b["media_info"] is not None
        assert shared_mgr.has_active_lease(op_b) is True

        shared_guard = ConcurrencyGuard(max_concurrent=2)
        with operation_slot(guard=shared_guard, timeout=0.01):
            with operation_slot(guard=shared_guard, timeout=0.01):
                with pytest.raises(ResourceLimitExceededError):
                    with operation_slot(guard=shared_guard, timeout=0.01):
                        pass


class TestStreamlitUIRendering:
    """Validate Streamlit UI rendering, download buttons, and sequential batch panel via AppTest."""

    def test_app_renders_and_executes_full_flow_and_batch_panel(self, tmp_path: Path) -> None:
        from streamlit.testing.v1 import AppTest

        meta = _sample_metadata()
        op_id = str(uuid.uuid4())
        out_file = tmp_path / "demo_out.mp4"
        out_file.write_bytes(b"0" * 2048)

        fake_dl_result = DownloadResult(
            operation_id=op_id,
            output_path=out_file,
            filename=out_file.name,
            filesize_bytes=2048,
            elapsed_seconds=0.8,
            format_used=meta.available_formats[0],
        )

        def _fake_dl_with_hooks(**kwargs: Any) -> DownloadResult:
            cb = kwargs.get("progress_callback")
            if cb:
                cb(
                    ProgressState(
                        stage=OperationStatus.DOWNLOADING,
                        downloaded_bytes=1024,
                        total_bytes=2048,
                        speed_bps=512 * 1024,
                        eta_seconds=1.0,
                        percent=50.0,
                    )
                )
                cb(
                    ProgressState(
                        stage=OperationStatus.DOWNLOADING,
                        downloaded_bytes=1500,
                        total_bytes=None,
                        speed_bps=None,
                        eta_seconds=None,
                        percent=None,
                    )
                )
                cb(
                    ProgressState(
                        stage=OperationStatus.PROCESSING,
                        downloaded_bytes=2048,
                        percent=100.0,
                        message="FFmpeg...",
                    )
                )
                cb(
                    ProgressState(
                        stage=OperationStatus.COMPLETED,
                        downloaded_bytes=2048,
                        total_bytes=2048,
                        percent=100.0,
                        message="Completed!",
                    )
                )
            return fake_dl_result

        batch_summary = BatchSummary(
            items=(
                BatchItemResult(
                    index=1,
                    url="https://93.184.216.34/b1",
                    succeeded=True,
                    title="Batch Item Ready",
                    result=fake_dl_result,
                ),
                BatchItemResult(
                    index=2,
                    url="http://127.0.0.1/blocked",
                    succeeded=False,
                    error=InvalidURLError("Blocked URL").to_structured(),
                ),
            ),
            cancelled=False,
        )

        app_script = Path(__file__).resolve().parent.parent / "app.py"
        with (
            patch("core.extractor.get_info", return_value=meta),
            patch("core.downloader.download_media", side_effect=_fake_dl_with_hooks),
            patch("core.downloader.execute_batch_download", return_value=batch_summary),
            patch.object(TempFileManager, "build_transfer_loader", return_value=lambda: b"123"),
        ):
            at = AppTest.from_file(app_script, default_timeout=15)
            at.run()
            assert not at.exception
            assert len(at.title) == 1
            assert at.title[0].value == "OmniFetch"

            at.button[0].click().run()
            assert len(at.error) == 1

            at.text_input[0].set_value("https://93.184.216.34/watch?v=123").run()
            at.button[0].click().run()
            assert not at.exception
            assert len(at.subheader) == 1
            assert at.subheader[0].value == "Demo Video"

            at.radio[0].set_value(DownloadMode.AUDIO_ONLY).run()
            assert not at.exception
            at.radio[0].set_value(DownloadMode.VIDEO).run()

            at.button[2].click().run()
            assert not at.exception

            at.button[1].click().run()
            assert not at.exception

            # Exercise batch panel rendering and clearing via AppTest
            at.text_area(key="batch_urls_textarea").set_value(
                "https://93.184.216.34/b1\nhttp://127.0.0.1/blocked"
            ).run()
            at.button(key="btn_run_batch").click().run()
            assert not at.exception
            assert any("Ready: Batch Item Ready" in s.value for s in at.success)
            assert any("Blocked URL" in e.value for e in at.error)

            at.button(key="btn_clear_batch").click().run()
            assert not at.exception
            assert at.session_state["batch_summary"] is None

    def test_app_blocks_form_when_startup_diagnostics_fail(self) -> None:
        from streamlit.testing.v1 import AppTest

        from core.models import DiagnosticCheck, DiagnosticReport

        failed_report = DiagnosticReport(
            checks=(
                DiagnosticCheck(
                    name="FFmpeg",
                    passed=False,
                    detail="Binary missing.",
                    remediation="Install FFmpeg.",
                ),
            )
        )
        app_script = Path(__file__).resolve().parent.parent / "app.py"
        with patch("utils.diagnostics.run_startup_diagnostics", return_value=failed_report):
            at = AppTest.from_file(app_script, default_timeout=15)
            at.run()
            assert not at.exception
            assert len(at.error) == 1
            assert len(at.text_input) == 0
