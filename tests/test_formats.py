"""Dedicated unit tests for `core/format_selector.py` and `core/models.py`."""

from __future__ import annotations

from core.format_selector import (
    build_format_options,
    estimate_stream_filesize,
    filter_formats,
    normalize_codec_name,
)
from core.models import DownloadMode, OperationalLimits, OutputProfile


class TestFormatSelector:
    """Validate codec normalization, size estimation, DASH pairing, original audio, and filtering."""

    def test_normalize_all_supported_codecs(self) -> None:
        assert normalize_codec_name("avc1.640028") == "h264"
        assert normalize_codec_name("hev1.1.6.L93.B0") == "h265"
        assert normalize_codec_name("vp09.00.50.08") == "vp9"
        assert normalize_codec_name("vp08.00") == "vp8"
        assert normalize_codec_name("av01.0.08M.08") == "av1"
        assert normalize_codec_name("mp4a.40.2") == "aac"
        assert normalize_codec_name("mp4a.69") == "mp3"
        assert normalize_codec_name("opus") == "opus"
        assert normalize_codec_name("vorbis") == "vorbis"
        assert normalize_codec_name("flac") == "flac"
        assert normalize_codec_name("theora.1") == "theora"
        assert normalize_codec_name("unknown") == "none"
        assert normalize_codec_name("   ") == "none"
        assert normalize_codec_name(None) == "none"

    def test_estimate_stream_filesize_exact_approx_bitrate_and_unknown(self) -> None:
        assert estimate_stream_filesize({"filesize": 5_000_000}, 100.0) == (5_000_000, False)
        assert estimate_stream_filesize({"filesize_approx": 4_500_000}, 100.0) == (4_500_000, True)
        size, is_est = estimate_stream_filesize({"tbr": 800}, 10.0)
        assert size == 1_000_000
        assert is_est is True
        assert estimate_stream_filesize({"tbr": 0}, 10.0) == (None, False)
        assert estimate_stream_filesize({}, 10.0) == (None, False)
        assert estimate_stream_filesize({}, None) == (None, False)

    def test_build_format_options_pairs_dash_and_formats_gib(self) -> None:
        raw_formats = [
            {
                "format_id": "137",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "none",
                "height": 1080,
                "fps": 60,
                "tbr": 4000,
                "filesize": 1200 * 1024 * 1024,
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "filesize": 100 * 1024 * 1024,
            },
        ]
        options = build_format_options(raw_formats, duration_seconds=180.0)
        videos = filter_formats(
            options,
            mode=DownloadMode.VIDEO,
            profile=OutputProfile.ORIGINAL,
            container="mp4",
            max_height=1080,
        )
        assert len(videos) == 1
        assert "GB" in videos[0].filesize_human
        assert filter_formats(options, profile=OutputProfile.COMPATIBILITY) == ()
        assert filter_formats(options, container="webm") == ()
        assert filter_formats(options, max_height=720) == tuple(
            o for o in options if o.height is None or o.height <= 720
        )

    def test_edge_cases_opus_in_mp4_deduplication_no_audio_and_limits(self) -> None:
        # 1. MP4 video-only paired with Opus audio -> switches container to MKV
        # 2. Same-resolution deduplication keeping higher tbr
        # 3. Streams exceeding max_filesize_bytes are filtered out
        raw_formats = [
            {
                "format_id": "v_low_tbr",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "none",
                "height": 720,
                "tbr": 1000,
                "filesize": 5_000_000,
            },
            {
                "format_id": "v_high_tbr",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "none",
                "height": 720,
                "tbr": 2500,
                "filesize": 8_000_000,
            },
            {
                "format_id": "v_ignore_lower_tbr",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "none",
                "height": 720,
                "tbr": 900,
                "filesize": 4_000_000,
            },
            {
                "format_id": "v_webm",
                "ext": "webm",
                "vcodec": "vp9",
                "acodec": "opus",
                "height": 480,
                "tbr": 800,
                "filesize": 3_000_000,
            },
            {
                "format_id": "v_oversized",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "aac",
                "height": 2160,
                "filesize": 50_000_000,
            },
            {
                "format_id": "a_opus",
                "ext": "opus",
                "vcodec": "none",
                "acodec": "opus",
                "abr": 160,
                "filesize": 1_000_000,
            },
            {
                "format_id": "a_oversized",
                "ext": "flac",
                "vcodec": "none",
                "acodec": "flac",
                "abr": 900,
                "filesize": 40_000_000,
            },
            {
                "format_id": "a_unknown_ext",
                "ext": "aac_raw",
                "vcodec": "none",
                "acodec": "vorbis",
                "abr": 96,
                "filesize": 500_000,
            },
        ]
        limits = OperationalLimits(max_filesize_bytes=20_000_000)
        opts = build_format_options(raw_formats, duration_seconds=60.0, limits=limits)
        videos = filter_formats(opts, mode=DownloadMode.VIDEO)
        v_720 = [v for v in videos if v.height == 720][0]
        # H264 + Opus switched to MKV container and kept higher tbr (2500)
        assert v_720.container == "mkv"
        assert v_720.tbr_kbps == 2500.0
        assert all(v.height != 2160 for v in videos)

        # Video-only with no available audio stream falls back to bestaudio
        video_only_no_audio = [
            {
                "format_id": "v_silent",
                "ext": "flv",
                "vcodec": "h264",
                "acodec": "none",
            }
        ]
        opts_no_audio = build_format_options(video_only_no_audio)
        v_silent = filter_formats(opts_no_audio, mode=DownloadMode.VIDEO)[0]
        assert v_silent.yt_dlp_format == "v_silent+bestaudio/v_silent/best"
        assert v_silent.container == "mkv"
        assert v_silent.audio_codec == "auto"

    def test_fallback_when_no_individual_video_streams_exist(self) -> None:
        options = build_format_options([])
        videos = filter_formats(options, mode=DownloadMode.VIDEO)
        assert len(videos) == 1
        assert videos[0].format_id == "video_best_auto_original"
        assert videos[0].filesize_human == "Unknown"

    def test_prefers_original_audio_language_over_dubbed_default_tracks(self) -> None:
        # Simulate YouTube multi-language tracks: dubbed EN appears first with (default)/higher bitrate,
        # and 360p progressive stream uses dubbed EN audio.
        raw_formats = [
            {
                "format_id": "18",
                "ext": "mp4",
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
                "height": 360,
                "tbr": 550,
                "language": "en",
                "language_preference": 5,
                "format_note": "English (default), 360p",
                "filesize": 12_000_000,
            },
            {
                "format_id": "134",
                "ext": "mp4",
                "vcodec": "avc1.4d401e",
                "acodec": "none",
                "height": 360,
                "tbr": 320,
                "filesize": 8_000_000,
            },
            {
                "format_id": "137",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "none",
                "height": 1080,
                "tbr": 4200,
                "filesize": 80_000_000,
            },
            {
                "format_id": "140-0",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 130,
                "language": "en",
                "language_preference": 5,
                "format_note": "English (default), medium",
                "filesize": 3_200_000,
            },
            {
                "format_id": "140-desc",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 140,
                "language": "pt-desc",
                "language_preference": -10,
                "format_note": "Portuguese descriptive, medium",
                "filesize": 3_400_000,
            },
            {
                "format_id": "140-1",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "language": "pt",
                "language_preference": 10,
                "format_note": "Portuguese (original), medium",
                "filesize": 3_000_000,
            },
            {
                "format_id": "251-1",
                "ext": "webm",
                "vcodec": "none",
                "acodec": "opus",
                "abr": 160,
                "language": "pt",
                "language_preference": 10,
                "format_note": "Portuguese (original), medium",
                "filesize": 3_500_000,
            },
        ]

        options = build_format_options(raw_formats, duration_seconds=180.0)
        videos = filter_formats(options, mode=DownloadMode.VIDEO)
        audios = filter_formats(options, mode=DownloadMode.AUDIO_ONLY)

        # 1080p MP4 video must pair with 140-1 (PT original), never 140-0 (EN)
        v_1080 = next(v for v in videos if v.height == 1080)
        assert v_1080.yt_dlp_format.startswith("137+140-1/")
        assert v_1080.audio_language == "pt"
        assert v_1080.is_original_audio is True

        # DASH 360p + 140-1 (PT original) must beat progressive 18 (EN dubbed)
        v_360 = next(v for v in videos if v.height == 360)
        assert v_360.yt_dlp_format.startswith("134+140-1/")
        assert v_360.audio_language == "pt"
        assert v_360.is_original_audio is True

        # Audio-only: PT original tracks are ordered first by default before EN tracks
        assert audios[0].audio_language == "pt"
        assert audios[0].is_original_audio is True
        assert audios[1].audio_language == "pt"
        assert audios[1].is_original_audio is True

        # Universal MP3 option targets best original audio stream (251-1)
        mp3_opt = next(a for a in audios if a.container == "mp3")
        assert mp3_opt.yt_dlp_format == "251-1/bestaudio/best"
        assert mp3_opt.audio_language == "pt"
        assert mp3_opt.is_original_audio is True

    def test_infers_original_language_from_metadata_without_original_note(self) -> None:
        raw_formats = [
            {
                "format_id": "v1",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "none",
                "height": 720,
            },
            {
                "format_id": "a_en_dubbed",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "abr": 160,
                "language": "en",
                "format_note": "English auto-dubbed",
            },
            {
                "format_id": "a_pt_native",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "abr": 128,
                "language": "pt-BR",
                "format_note": "Portuguese",
            },
        ]
        options = build_format_options(raw_formats, original_language="pt")
        v_opt = filter_formats(options, mode=DownloadMode.VIDEO)[0]
        assert v_opt.yt_dlp_format.startswith("v1+a_pt_native/")
        assert v_opt.audio_language == "pt-br"
        assert v_opt.is_original_audio is True

    def test_language_normalization_and_same_bitrate_drc_deduplication(self) -> None:
        raw_formats = [
            "malformed_entry",  # type: ignore[list-item]
            {
                "format_id": "v_und",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "aac",
                "height": 720,
                "tbr": 1500,
                "language": "und",
            },
            {
                "format_id": "140-drc",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "abr": 128,
                "tbr": 128,
                "language": None,
                "format_note": "Original, DRC",
            },
            {
                "format_id": "140-orig",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "abr": 128,
                "tbr": 128,
                "language": None,
                "format_note": "Original",
            },
        ]
        options = build_format_options(raw_formats)
        audios = filter_formats(options, mode=DownloadMode.AUDIO_ONLY)
        m4a_opt = next(a for a in audios if a.container == "m4a")
        assert m4a_opt.yt_dlp_format == "140-orig"
        assert m4a_opt.is_original_audio is True
        assert "Original" in m4a_opt.label
