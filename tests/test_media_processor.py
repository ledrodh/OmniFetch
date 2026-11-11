"""Unit and FFmpeg integration tests for `core/media_processor.py`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.media_processor import (
    build_postprocessor_config,
    probe_media_file,
    remux_media,
    trim_media_segment,
)
from core.models import DownloadMode, FormatOption, OutputProfile, TimeRange
from utils.error_handler import PostProcessingError


def _make_option(
    *,
    mode: DownloadMode = DownloadMode.VIDEO,
    profile: OutputProfile = OutputProfile.ORIGINAL,
    container: str = "mp4",
    requires_reencode: bool = False,
    abr_kbps: float | None = 160.0,
) -> FormatOption:
    return FormatOption(
        format_id="test_fmt",
        yt_dlp_format="137+140",
        label="Test",
        mode=mode,
        profile=profile,
        container=container,
        video_codec="h264" if mode == DownloadMode.VIDEO else "none",
        audio_codec="aac",
        resolution="1080p" if mode == DownloadMode.VIDEO else "Audio-Only",
        abr_kbps=abr_kbps,
        requires_merge=(mode == DownloadMode.VIDEO),
        requires_reencode=requires_reencode,
    )


class TestMediaProcessorConfigs:
    """Validate FFmpeg postprocessor argument generation across containers and profiles."""

    def test_video_original_mp4_includes_faststart(self) -> None:
        opt = _make_option(mode=DownloadMode.VIDEO, container="mp4")
        cfg = build_postprocessor_config(opt)
        assert cfg["merge_output_format"] == "mp4"
        assert cfg["postprocessor_args"]["merger"] == ["-c", "copy", "-movflags", "+faststart"]

    def test_video_original_webm_and_mkv_omit_movflags(self) -> None:
        for container in ("webm", "mkv"):
            opt = _make_option(mode=DownloadMode.VIDEO, container=container)
            cfg = build_postprocessor_config(opt)
            assert cfg["merge_output_format"] == container
            assert cfg["postprocessor_args"]["merger"] == ["-c", "copy"]
            assert "+faststart" not in cfg["postprocessor_args"]["merger"]

    def test_video_compatibility_profile_forces_h264_aac_mp4(self) -> None:
        opt = _make_option(
            mode=DownloadMode.VIDEO,
            profile=OutputProfile.COMPATIBILITY,
            container="webm",
            requires_reencode=True,
        )
        cfg = build_postprocessor_config(opt)
        assert cfg["merge_output_format"] == "mp4"
        assert cfg["postprocessors"][0]["key"] == "FFmpegVideoConvertor"
        assert "libx264" in cfg["postprocessor_args"]["videoconvertor"]

    def test_audio_mp3_and_m4a_configs(self) -> None:
        mp3_opt = _make_option(
            mode=DownloadMode.AUDIO_ONLY,
            container="mp3",
            requires_reencode=True,
            abr_kbps=160.0,
        )
        cfg_mp3 = build_postprocessor_config(mp3_opt)
        assert cfg_mp3["postprocessors"][0] == {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "160",
        }

        m4a_opt = _make_option(mode=DownloadMode.AUDIO_ONLY, container="m4a")
        cfg_m4a = build_postprocessor_config(m4a_opt)
        assert cfg_m4a["postprocessors"][0]["key"] == "FFmpegVideoRemuxer"
        assert "+faststart" in cfg_m4a["postprocessor_args"]["videoremuxer"]


class TestStandaloneFFmpegAndFFprobe:
    """Validate `probe_media_file`, `remux_media`, and keyframe/non-keyframe `trim_media_segment`."""

    def test_probe_media_file_success_and_error_cases(self, tmp_path: Path) -> None:
        with pytest.raises(PostProcessingError):
            probe_media_file(tmp_path / "does_not_exist.mp4")

        media_file = tmp_path / "sample.mp4"
        media_file.write_bytes(b"sample")

        ok_proc = subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout='{"streams": [{"codec_name": "h264"}], "format": {"format_name": "mp4"}}',
            stderr="",
        )
        with patch("subprocess.run", return_value=ok_proc):
            parsed = probe_media_file(media_file)
            assert parsed["streams"][0]["codec_name"] == "h264"

        bad_exit = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=1, stdout="", stderr="Invalid data"
        )
        with patch("subprocess.run", return_value=bad_exit):
            with pytest.raises(PostProcessingError):
                probe_media_file(media_file)

        bad_json = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=0, stdout="{invalid_json", stderr=""
        )
        with patch("subprocess.run", return_value=bad_json):
            with pytest.raises(PostProcessingError):
                probe_media_file(media_file)

        with patch("subprocess.run", side_effect=OSError("ffprobe not found")):
            with pytest.raises(PostProcessingError):
                probe_media_file(media_file)

    def test_remux_media_mp4_webm_and_failure_cases(self, tmp_path: Path) -> None:
        with pytest.raises(PostProcessingError):
            remux_media(tmp_path / "missing.mp4", tmp_path / "out.mp4")

        in_file = tmp_path / "in.webm"
        out_webm = tmp_path / "out.webm"
        in_file.write_bytes(b"webm_data")

        captured_cmds: list[list[str]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured_cmds.append(cmd)
            Path(cmd[-1]).write_bytes(b"remuxed_output")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=_fake_run):
            res = remux_media(in_file, out_webm, faststart=True)
            assert res == out_webm
            # Non-MP4 containers must never receive `-movflags +faststart`
            assert "+faststart" not in captured_cmds[-1]

        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="err"
            ),
        ):
            with pytest.raises(PostProcessingError):
                remux_media(in_file, tmp_path / "fail.webm")

        with patch("subprocess.run", side_effect=OSError("ffmpeg missing")):
            with pytest.raises(PostProcessingError):
                remux_media(in_file, tmp_path / "err.webm")

    def test_trim_media_aligned_and_unaligned_keyframes_and_failure_cleanup(
        self, tmp_path: Path
    ) -> None:
        src_mp4 = tmp_path / "source_keyframes.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=6:size=320x240:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=6",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "20",  # Keyframe every 2.0 seconds (t=0.0, 2.0, 4.0)
                "-sc_threshold",
                "0",
                "-c:a",
                "aac",
                str(src_mp4),
            ],
            check=True,
        )

        # 1. Keyframe-aligned interval (2.0s - 4.0s) into separate output
        aligned_out = tmp_path / "aligned.mp4"
        res_aligned = trim_media_segment(
            src_mp4,
            TimeRange(2.0, 4.0),
            output_path=aligned_out,
        )
        assert res_aligned == aligned_out
        aligned_dur = float(probe_media_file(aligned_out)["format"]["duration"])
        assert 1.5 <= aligned_dur <= 2.8

        # 2. Non-keyframe-aligned interval (1.3s - 3.7s) cleanly produces valid container
        unaligned_out = tmp_path / "unaligned.mp4"
        res_unaligned = trim_media_segment(
            src_mp4,
            TimeRange(1.3, 3.7),
            output_path=unaligned_out,
        )
        assert res_unaligned == unaligned_out
        unaligned_dur = float(probe_media_file(unaligned_out)["format"]["duration"])
        assert 1.0 <= unaligned_dur <= 4.2

        # 3. Verify FFmpeg `-ss` and `-to` precede `-i` and `.clip_tmp` is removed on OSError
        captured_cmds: list[list[str]] = []

        def _fake_fail(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured_cmds.append(cmd)
            Path(cmd[-1]).write_bytes(b"partial_garbage")
            raise OSError("Simulated FFmpeg execution interruption")

        with patch("subprocess.run", side_effect=_fake_fail):
            with pytest.raises(PostProcessingError):
                trim_media_segment(src_mp4, TimeRange(1.0, 3.0))

        last_cmd = captured_cmds[-1]
        assert last_cmd.index("-ss") < last_cmd.index("-i")
        assert last_cmd.index("-to") < last_cmd.index("-i")
        assert not src_mp4.with_name(f"{src_mp4.stem}.clip_tmp.mp4").exists()
