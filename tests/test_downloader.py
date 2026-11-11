"""Unit and real E2E tests for `core/downloader.py` (real DASH `.mpd`, post-download trimming, sequential batches)."""

from __future__ import annotations

import functools
import http.server
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from core.downloader import download_media, execute_batch_download
from core.extractor import get_info
from core.format_selector import filter_formats
from core.media_processor import probe_media_file, remux_media, trim_media_segment
from core.models import (
    DownloadMode,
    ErrorCategory,
    FormatOption,
    MediaMetadata,
    OperationalLimits,
    OperationStatus,
    OutputProfile,
    ProgressState,
    TimeRange,
)
from utils.error_handler import (
    ExtractionFailedError,
    PostProcessingError,
    ResourceLimitExceededError,
)
from utils.file_manager import TempFileManager


def _sample_format_option(*, filesize_bytes: int | None = 1024 * 1024) -> FormatOption:
    return FormatOption(
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
        filesize_bytes=filesize_bytes,
        requires_merge=True,
    )


class _QuietHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


class TestDownloaderPipelineAndAggregateLimits:
    """Validate 4-stage pipeline, Download Lease, real DASH .mpd E2E, trimming, and sequential batches."""

    def test_emits_four_stages_and_activates_download_lease(self, tmp_path: Path) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "dl_store")
        fmt = _sample_format_option()
        recorded_states: list[ProgressState] = []

        class FakeSafeYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                self.opts = opts

            def __enter__(self) -> FakeSafeYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                for hook in self.opts.get("progress_hooks", []):
                    hook(
                        {
                            "status": "downloading",
                            "info_dict": {"format_id": "137"},
                            "downloaded_bytes": 512 * 1024,
                            "total_bytes": 800 * 1024,
                            "speed": 256 * 1024,
                            "eta": 1,
                        }
                    )
                    hook(
                        {
                            "status": "finished",
                            "info_dict": {"format_id": "137"},
                            "downloaded_bytes": 800 * 1024,
                            "total_bytes": 800 * 1024,
                        }
                    )
                for pp_hook in self.opts.get("postprocessor_hooks", []):
                    pp_hook({"status": "started", "postprocessor": "FFmpegMerger"})

                outtmpl_str = self.opts["outtmpl"]["default"]
                target_file = Path(outtmpl_str.replace("%(ext)s", "mp4"))
                target_file.write_bytes(b"0" * (1024 * 1024))
                return 0

        with patch("core.downloader.SafeYoutubeDL", FakeSafeYDL):
            result = download_media(
                "https://93.184.216.34/video",
                fmt,
                title="Meu Vídeo: Teste / Completo?",
                file_manager=mgr,
                progress_callback=recorded_states.append,
                resolve_dns=False,
            )

        stages = [s.stage for s in recorded_states]
        assert stages[0] == OperationStatus.IDENTIFYING
        assert OperationStatus.DOWNLOADING in stages
        assert OperationStatus.PROCESSING in stages
        assert stages[-1] == OperationStatus.COMPLETED
        assert mgr.has_active_lease(result.operation_id) is True
        assert result.output_path.exists()

    def test_aborts_when_dash_video_plus_audio_cumulative_exceeds_limit(
        self, tmp_path: Path
    ) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "dl_dash_aggregate")
        fmt = _sample_format_option(filesize_bytes=None)
        limits = OperationalLimits(max_filesize_bytes=1_600_000)

        class DashMultiStreamYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                self.opts = opts

            def __enter__(self) -> DashMultiStreamYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                hooks = self.opts.get("progress_hooks", [])
                for h in hooks:
                    h(
                        {
                            "status": "finished",
                            "tmpfilename": "video.f137.mp4",
                            "downloaded_bytes": 1_500_000,
                            "total_bytes": 1_500_000,
                        }
                    )
                for h in hooks:
                    h(
                        {
                            "status": "downloading",
                            "tmpfilename": "audio.f140.m4a",
                            "downloaded_bytes": 300_000,
                            "total_bytes": 300_000,
                        }
                    )
                return 0

        with patch("core.downloader.SafeYoutubeDL", DashMultiStreamYDL):
            with pytest.raises(ResourceLimitExceededError):
                download_media(
                    "https://93.184.216.34/dash",
                    fmt,
                    file_manager=mgr,
                    limits=limits,
                    resolve_dns=False,
                )

        assert [p for p in mgr.base_dir.iterdir() if p.is_dir()] == []

    def test_handles_nonzero_exit_and_missing_output_file(self, tmp_path: Path) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "dl_errors")
        fmt = _sample_format_option()

        class NonZeroYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                pass

            def __enter__(self) -> NonZeroYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                return 1

        with patch("core.downloader.SafeYoutubeDL", NonZeroYDL):
            with pytest.raises(ExtractionFailedError):
                download_media(
                    "https://93.184.216.34/fail",
                    fmt,
                    file_manager=mgr,
                    resolve_dns=False,
                )

        class NoOutputFileYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                pass

            def __enter__(self) -> NoOutputFileYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                return 0

        with patch("core.downloader.SafeYoutubeDL", NoOutputFileYDL):
            with pytest.raises(PostProcessingError):
                download_media(
                    "https://93.184.216.34/nofile",
                    fmt,
                    file_manager=mgr,
                    resolve_dns=False,
                )

    def test_empty_final_file_and_oversized_final_file_and_unexpected_exception(
        self, tmp_path: Path
    ) -> None:
        from core.downloader import _calculate_directory_bytes
        from utils.error_handler import MediaUnavailableError

        assert _calculate_directory_bytes(tmp_path / "nonexistent_dir") == 0

        mgr = TempFileManager(base_dir=tmp_path / "dl_edge")
        fmt = _sample_format_option(filesize_bytes=1024)

        class EmptyFileYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                self.opts = opts

            def __enter__(self) -> EmptyFileYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                for h in self.opts.get("progress_hooks", []):
                    h({"status": "downloading", "downloaded_bytes": 100})
                outtmpl_str = self.opts["outtmpl"]["default"]
                Path(outtmpl_str.replace("%(ext)s", "mp4")).write_bytes(b"")
                return 0

        with patch("core.downloader.SafeYoutubeDL", EmptyFileYDL):
            with pytest.raises(PostProcessingError):
                download_media(
                    "https://93.184.216.34/empty",
                    fmt,
                    file_manager=mgr,
                    resolve_dns=False,
                )

        class OversizedFinalFileYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                self.opts = opts

            def __enter__(self) -> OversizedFinalFileYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                outtmpl_str = self.opts["outtmpl"]["default"]
                Path(outtmpl_str.replace("%(ext)s", "mp4")).write_bytes(b"0" * 5000)
                return 0

        with patch("core.downloader.SafeYoutubeDL", OversizedFinalFileYDL):
            with pytest.raises(ResourceLimitExceededError):
                download_media(
                    "https://93.184.216.34/bigfinal",
                    fmt,
                    file_manager=mgr,
                    limits=OperationalLimits(max_filesize_bytes=2000),
                    resolve_dns=False,
                )

        class RawExceptionYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                pass

            def __enter__(self) -> RawExceptionYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                raise RuntimeError("ERROR: Private video")

        with patch("core.downloader.SafeYoutubeDL", RawExceptionYDL):
            with pytest.raises(MediaUnavailableError):
                download_media(
                    "https://93.184.216.34/priv",
                    fmt,
                    file_manager=mgr,
                    resolve_dns=False,
                )

    def test_real_dash_mpd_manifest_extraction_selection_download_and_ffmpeg_merge(
        self, tmp_path: Path
    ) -> None:
        """
        Real MPEG-DASH (.mpd) E2E Test:
        1. Generate a real MPEG-DASH manifest (`manifest.mpd`) + `.m4s` segments via FFmpeg:
           - 1280x720 H.264 video representation
           - 640x360 H.264 video representation
           - AAC audio representation
        2. Serve manifest and segments via a local ThreadingHTTPServer;
        3. Extract real metadata via `get_info()`;
        4. Select 720p (`requires_merge=True`);
        5. Download and merge real segments via `download_media()` + FFmpeg;
        6. Inspect output MP4 via `probe_media_file()` (FFprobe) verifying 720p H.264 + AAC.
        """
        dash_dir = tmp_path / "dash_server_root"
        dash_dir.mkdir(parents=True, exist_ok=True)
        mpd_file = dash_dir / "manifest.mpd"

        # Generate real .mpd DASH manifest with 720p/360p H.264 video and AAC audio streams
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=2:size=1280x720:rate=15",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=2:size=640x360:rate=15",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-map",
                "0:v:0",
                "-map",
                "1:v:0",
                "-map",
                "2:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-f",
                "dash",
                "-seg_duration",
                "1",
                str(mpd_file),
            ],
            check=True,
            cwd=str(dash_dir),
        )
        assert mpd_file.exists()

        handler = functools.partial(_QuietHTTPHandler, directory=str(dash_dir))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            mpd_url = f"http://127.0.0.1:{port}/manifest.mpd"
            mgr = TempFileManager(base_dir=tmp_path / "dash_mpd_e2e_store")

            # Strictly scope local loopback test server exception to this pytest fixture block;
            # production always runs resolve_dns=True with full _assert_public_hostname enforcement.
            with patch("utils.validators._assert_public_hostname", return_value=True):
                meta = get_info(mpd_url, resolve_dns=False)
                video_opts = filter_formats(meta.available_formats, mode=DownloadMode.VIDEO)
                heights = {o.height for o in video_opts}
                assert 720 in heights
                assert 360 in heights

                chosen_720p = [o for o in video_opts if o.height == 720][0]
                assert chosen_720p.requires_merge is True

                dl_res = download_media(
                    mpd_url,
                    chosen_720p,
                    title="DASH MPD Real 720p",
                    file_manager=mgr,
                    resolve_dns=False,
                )

            probe = probe_media_file(dl_res.output_path)
            streams = probe.get("streams", [])
            v_streams = [s for s in streams if s.get("codec_type") == "video"]
            a_streams = [s for s in streams if s.get("codec_type") == "audio"]
            assert len(v_streams) == 1
            assert v_streams[0]["codec_name"] == "h264"
            assert v_streams[0]["height"] == 720
            assert len(a_streams) == 1
            assert a_streams[0]["codec_name"] == "aac"
            assert mgr.cleanup_operation(dl_res.operation_id, force=True) is True
        finally:
            server.shutdown()
            server.server_close()

    def test_temporal_clipping_and_corrupted_postprocessing_failure(self, tmp_path: Path) -> None:
        """
        Validate post-download temporal trimming + corrupted post-processing cleanup:
        - Generate a real 4-second MP4 via FFmpeg;
        - Execute `download_media()` with `TimeRange(1.0, 3.0)` (~2s segment);
        - Verify clipped duration via FFprobe;
        - Verify clean PostProcessingError handling on invalid/missing media inputs.
        """
        mgr = TempFileManager(base_dir=tmp_path / "clip_store")
        fmt = _sample_format_option(filesize_bytes=1024 * 1024)

        class RealFourSecondVideoYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                self.opts = opts

            def __enter__(self) -> RealFourSecondVideoYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                outtmpl_str = self.opts["outtmpl"]["default"]
                target_mp4 = Path(outtmpl_str.replace("%(ext)s", "mp4"))
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc=duration=4:size=640x360:rate=15",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:duration=4",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-g",
                        "15",
                        "-c:a",
                        "aac",
                        str(target_mp4),
                    ],
                    check=True,
                )
                return 0

        clip_range = TimeRange(start_seconds=1.0, end_seconds=3.0)
        with patch("core.downloader.SafeYoutubeDL", RealFourSecondVideoYDL):
            res = download_media(
                "https://93.184.216.34/clip_video",
                fmt,
                title="Video Recortado",
                time_range=clip_range,
                file_manager=mgr,
                resolve_dns=False,
            )

        assert res.time_range == clip_range
        probe = probe_media_file(res.output_path)
        duration = float(probe["format"]["duration"])
        assert 1.3 <= duration <= 2.7
        assert mgr.cleanup_operation(res.operation_id, force=True) is True

        # Test trim_media_segment with separate output path and missing/corrupted files
        corrupted_in = tmp_path / "corrupted.mp4"
        corrupted_out = tmp_path / "corrupted_remux.mp4"
        corrupted_in.write_bytes(b"NOT_A_VALID_MP4_CONTAINER_HEADER")
        with pytest.raises(PostProcessingError):
            remux_media(corrupted_in, corrupted_out)
        with pytest.raises(PostProcessingError):
            trim_media_segment(corrupted_in, clip_range)
        with pytest.raises(PostProcessingError):
            trim_media_segment(tmp_path / "missing_input.mp4", clip_range)

    def test_controlled_batch_download_partial_success_cancellation_and_limits(
        self, tmp_path: Path
    ) -> None:
        """
        Validate controlled sequential batch execution:
        - Empty URL list returns empty BatchSummary;
        - Batch sizes above `max_batch_urls` raise ResourceLimitExceededError;
        - Partial success: valid URLs finish even if an SSRF-blocked URL fails mid-batch;
        - Cooperative cancellation stops before starting the next item while preserving completed items.
        """
        mgr = TempFileManager(base_dir=tmp_path / "batch_store")
        limits = OperationalLimits(max_batch_urls=3)

        assert execute_batch_download([], file_manager=mgr, limits=limits).items == ()

        with pytest.raises(ResourceLimitExceededError):
            execute_batch_download(
                [
                    "https://93.184.216.34/1",
                    "https://93.184.216.34/2",
                    "https://93.184.216.34/3",
                    "https://93.184.216.34/4",
                ],
                file_manager=mgr,
                limits=limits,
                resolve_dns=False,
            )

        fmt = _sample_format_option(filesize_bytes=1024)
        sample_meta = MediaMetadata(
            media_id="m1",
            title="Item Lote",
            webpage_url="https://93.184.216.34/ok",
            platform="Generic",
            extractor_key="Generic",
            duration_seconds=10.0,
            available_formats=(fmt,),
        )

        class BatchFakeYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                self.opts = opts

            def __enter__(self) -> BatchFakeYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                for h in self.opts.get("progress_hooks", []):
                    h({"status": "downloading", "downloaded_bytes": 512, "total_bytes": 1024})
                outtmpl_str = self.opts["outtmpl"]["default"]
                Path(outtmpl_str.replace("%(ext)s", "mp4")).write_bytes(b"0" * 1024)
                return 0

        progress_events: list[tuple[int, str, OperationStatus]] = []

        with (
            patch("core.downloader.get_info", return_value=sample_meta),
            patch("core.downloader.SafeYoutubeDL", BatchFakeYDL),
        ):
            # Batch of 3 URLs where item 2 targets loopback and is rejected by SSRF guard
            summary = execute_batch_download(
                [
                    "https://93.184.216.34/item1",
                    "http://127.0.0.1/blocked-item2",
                    "https://93.184.216.34/item3",
                ],
                file_manager=mgr,
                limits=limits,
                on_item_progress=lambda idx, u, st: progress_events.append((idx, u, st.stage)),
                resolve_dns=False,
            )

        assert summary.cancelled is False
        assert len(summary.items) == 3
        assert summary.succeeded_count == 2
        assert summary.failed_count == 1
        assert summary.total_bytes == 2048
        assert summary.items[0].succeeded is True
        assert summary.items[1].succeeded is False
        assert summary.items[1].error is not None
        assert summary.items[2].succeeded is True
        assert len(progress_events) >= 2

        # Teste de cancelamento cooperativo apos o 1o item
        processed_count = 0

        def _cancel_after_first() -> bool:
            nonlocal processed_count
            processed_count += 1
            return processed_count > 1

        with (
            patch("core.downloader.get_info", return_value=sample_meta),
            patch("core.downloader.SafeYoutubeDL", BatchFakeYDL),
        ):
            cancelled_summary = execute_batch_download(
                ["https://93.184.216.34/item1", "https://93.184.216.34/item2"],
                file_manager=mgr,
                limits=limits,
                cancel_check=_cancel_after_first,
                resolve_dns=False,
            )

        assert cancelled_summary.cancelled is True
        assert len(cancelled_summary.items) == 1
        assert cancelled_summary.succeeded_count == 1

    def test_batch_five_urls_accepted_ffmpeg_failure_global_quota_and_cleanup(
        self, tmp_path: Path
    ) -> None:
        """
        Cover all additional Batch & Downloader checks required by Audit Item 7:
        - Explicitly enforces `concurrent_fragment_downloads == 1` in ydl_opts;
        - 5 valid URLs succeed; 6 URLs rejected cleanly;
        - FFmpeg postprocessing failure on one item isolates failure and cleans temp folder;
        - Global workspace quota exceeded halts offending item with structured error;
        - Completed items remain retrievable through `build_transfer_loader()`.
        """
        mgr = TempFileManager(base_dir=tmp_path / "batch_audit_store")
        limits_five = OperationalLimits(
            max_batch_urls=5,
            max_filesize_bytes=10_000,
            max_workspace_bytes=10_000,
            max_global_workspace_bytes=4096,
        )
        fmt = _sample_format_option(filesize_bytes=512)
        meta_ok = MediaMetadata(
            media_id="b1",
            title="Batch Valid Item",
            webpage_url="https://93.184.216.34/ok",
            platform="Generic",
            extractor_key="Generic",
            duration_seconds=5.0,
            available_formats=(fmt,),
        )
        captured_fragment_concurrency: list[Any] = []
        call_seq = 0

        class MixedBatchYDL:
            def __init__(self, opts: dict[str, Any], **kwargs: object) -> None:
                self.opts = opts
                captured_fragment_concurrency.append(opts.get("concurrent_fragment_downloads"))

            def __enter__(self) -> MixedBatchYDL:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def download(self, urls: list[str]) -> int:
                nonlocal call_seq
                call_seq += 1
                if call_seq == 2:
                    # Simulate mid-item FFmpeg failure leaving behind no orphan artifact
                    raise RuntimeError("ERROR: Postprocessing: ffmpeg failed on stream 2")
                outtmpl_str = self.opts["outtmpl"]["default"]
                Path(outtmpl_str.replace("%(ext)s", "mp4")).write_bytes(b"B" * 512)
                return 0

        five_urls = [f"https://93.184.216.34/item_{i}" for i in range(5)]
        with (
            patch("core.downloader.get_info", return_value=meta_ok),
            patch("core.downloader.SafeYoutubeDL", MixedBatchYDL),
        ):
            summary = execute_batch_download(
                five_urls,
                file_manager=mgr,
                limits=limits_five,
                resolve_dns=False,
            )

        assert all(c == 1 for c in captured_fragment_concurrency)
        assert len(summary.items) == 5
        assert summary.succeeded_count == 4
        assert summary.failed_count == 1
        assert summary.items[1].error is not None
        assert summary.items[1].error.category == ErrorCategory.POST_PROCESSING

        # Verify each completed item is retrievable via LeasedFileReader and then clean up
        for item in summary.items:
            if item.succeeded and item.result is not None:
                with mgr.build_transfer_loader(
                    item.result.operation_id, item.result.output_path
                )() as r:
                    assert r.read() == b"B" * 512
                assert mgr.cleanup_operation(item.result.operation_id, force=True) is True

        # 6 URLs rejected when max_batch_urls=5
        with pytest.raises(ResourceLimitExceededError):
            execute_batch_download(
                [f"https://93.184.216.34/x_{i}" for i in range(6)],
                file_manager=mgr,
                limits=limits_five,
                resolve_dns=False,
            )

        # Global workspace quota exceeded inside batch produces isolated ResourceLimit failure
        saturated_op, saturated_dir = mgr.create_operation_dir()
        (saturated_dir / "fill.bin").write_bytes(b"S" * 4000)
        with patch("core.downloader.get_info", return_value=meta_ok):
            quota_summary = execute_batch_download(
                ["https://93.184.216.34/over_quota"],
                file_manager=mgr,
                limits=limits_five,
                resolve_dns=False,
            )
        assert quota_summary.failed_count == 1
        assert quota_summary.items[0].error is not None
        assert quota_summary.items[0].error.category == ErrorCategory.RESOURCE_LIMIT
        mgr.cleanup_operation(saturated_op, force=True)
