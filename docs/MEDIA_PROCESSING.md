# Media Selection, Processing & Streaming — OmniFetch

This document describes stream selection, original-audio ranking, FFmpeg post-processing, temporal trimming contracts, sequential batch processing, and `RawIOBase` leased file streaming.

---

## 1. Original-Language Audio Prioritization

Multi-language platforms (especially YouTube auto-dubbing) frequently return several audio tracks per video, where the creator's original track carries `language_preference = 10` or `(original)` in `format_note` while an auto-dubbed track may carry `language_preference = 5` (`(default)`) or higher bitrate.

OmniFetch enforces original-language priority in [`core/format_selector.py`](../core/format_selector.py), [`core/extractor.py`](../core/extractor.py), and [`core/downloader.py`](../core/downloader.py):

1. **`infer_original_language()`:** Scans audio-bearing streams for explicit `original` markers (`format_note`, `format_id` suffixes `-orig`/`-original`, or `language_preference >= 10`) and falls back to top-level extractor `language` metadata.
2. **`_stream_audio_originality_rank()`:** Computes `(tier, language_preference, non_drc)` ranking in strict preference order:
   * Tier `4`: Explicit `original` marker or `language_preference >= 10` (non-descriptive);
   * Tier `3`: Language tag matching inferred original language (exact or primary ISO subtag, e.g., `pt-br` matching `pt`);
   * Tier `2`: Unspecified language metadata (preserves neutral tie-breaking on single-language or generic streams);
   * Tier `1`: Foreign / non-original language track when an original track or original metadata is present;
   * Tier `0`: Explicitly dubbed/auto-dubbed/translated track;
   * Tier `-1`: Descriptive audio (`descriptive`, `-desc`, `language_preference <= -10`).
   * On tie, `language_preference` and non-DRC (`non_drc = 1` over `-drc`) break ties cleanly.
3. **Applied Uniformly:** Used in DASH video+audio pairing (`_select_best_audio_stream`), progressive vs. DASH deduplication (preventing dubbed progressive `360p` from displacing DASH video + original audio), audio-only stream sorting, the universal MP3 conversion format spec (`<orig_id>/bestaudio/best`), and `yt-dlp`'s `"format_sort": ["lang"]`.

---

## 2. Remuxing, Re-Encoding & Post-Download Trimming

* **Original Profile (`-c copy`):** Merges and remuxes native streams losslessly. `-movflags +faststart` is appended **only** for MP4-family containers (`mp4`, `m4a`, `mov`), avoiding corruption on `webm` and `mkv`.
* **Compatibility Profile:** Re-encodes video streams on demand into widely compatible `H.264` (`libx264`, `veryfast`, `crf=23`) + `AAC` in `.mp4`.
* **MP3 Extraction:** Re-encodes audio using the bitrate tier of the best original-language source track (clamped between `64` and `320 kbps`) rather than inflating low-bitrate sources.
* **Post-Download Temporal Trimming (`trim_media_segment`):**
  * Parses `SS`, `MM:SS`, and `HH:MM:SS` (`validate_time_range`) and rejects intervals `< 1s`, `end <= start`, durations past `media_duration + 0.5s`, and `is_live=True` streams.
  * **Contract Clarification:** In v1.1, OmniFetch downloads the full selected media first and runs FFmpeg locally (`ffmpeg -y -v error -ss START -to END -i INPUT -c copy ...`) before replacing the artifact atomically via `.clip_tmp`. Because `-c copy` seeks via keyframes, boundaries snap to the nearest keyframes rather than achieving frame-accurate cuts. Both behaviors are documented in the UI expander.

---

## 3. Controlled Sequential Batch Processing

`execute_batch_download()` processes up to `max_batch_urls = 5` URLs **sequentially**:
* **Why Sequential in v1.1:** Deterministic interaction with per-item Download Leases, process-scoped atomic global space reservations (`max_global_workspace_bytes`), per-item SSRF checks, and clear progress reporting.
* **Failure Isolation:** An invalid URL, SSRF block, quota limit, or FFmpeg error on item $k$ records a `StructuredError` on `BatchItemResult(succeeded=False)` and proceeds cleanly to item $k+1$.
* **Cooperative Between-Item Cancellation:** `cancel_check()` is evaluated between batch items; requesting cancellation stops the batch gracefully **after the currently executing item finishes**.
* **Delivery & Cleanup:** In [`app.py`](../app.py), each succeeded batch item exposes its own `st.download_button` backed by an independent Download Lease, and `cleanup_batch_state()` purges all batch workspaces on reset/clear.

---

## 4. `Download Lease` & `LeasedFileReader(io.RawIOBase)` Contract

* Upon successful download completion, `TempFileManager.active_operation(..., retain_lease_on_success=True)` writes an expiration marker (`.lease`) protecting the operation folder from background `cleanup_expired_files()` TTL sweeps while the download button is displayed.
* `TempFileManager.build_transfer_loader(op_id, file_path)` returns a thread-safe factory for Streamlit's `st.download_button`, opening a `LeasedFileReader(io.RawIOBase)`:
  * `read(size=-1)` preserves standard Python `io.RawIOBase` semantics (returns up to `size` bytes when `size >= 0`, or reads completely until EOF `b""` when `size < 0` or `None` without silent truncation).
  * `readinto(buf)` delegates directly into caller byte buffers to reduce intermediate heap allocations.
  * `iter_chunks()` yields fixed `64 KiB` (`DEFAULT_STREAM_CHUNK_BYTES`) chunks and releases the reader lease in `finally`.
  * Concurrent active transfers increment `_reader_counts[op_id]`; if `cleanup_operation()` is triggered mid-transfer, deletion is deferred via `_pending_deletion` until the final reader calls `close()`.
  * **Streamlit Buffering Note:** Although `LeasedFileReader` bounds Python heap allocations per reader stream, Streamlit's Tornado HTTP server may buffer HTTP download responses internally; local deployment with adequate RAM headroom is recommended for files near `2 GiB`.
