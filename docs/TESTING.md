# Testing, Quality Gates & CI — OmniFetch

This document summarizes OmniFetch's automated verification layers, coverage breakdown (statements and branches), local execution commands, and CI/Docker pipeline status.

---

## 1. Automated Test Suite Structure

| Test Module | Scope & Evidence Covered |
| :--- | :--- |
| [`tests/test_validators.py`](../tests/test_validators.py) | IPv4/IPv6 Anti-SSRF (NAT64, 6to4, mapped, cloud metadata), DNS rebinding simulation, out-of-order concurrent context exits, unentered-thread isolation, `ThreadPoolExecutor` fragment worker guard propagation (`copy_context()`), `TimeRange` parsing/limits, and `ConcurrencyGuard`. |
| [`tests/test_formats.py`](../tests/test_formats.py) | RFC 6381 codec normalization, filesize estimation, Opus-in-MP4 -> MKV migration, oversized audio filtering, multi-language YouTube auto-dubbed prioritization (`PT Original` beating `EN default`/progressive `360p`/MP3 fallback), and `-desc`/`-drc` handling. |
| [`tests/test_extractor.py`](../tests/test_extractor.py) | Safe metadata extraction, playlist unwrapping, SSRF rejection on embedded format/fragment/thumbnail URLs, `"format_sort": ["lang"]` verification, and error taxonomy mapping. |
| [`tests/test_media_processor.py`](../tests/test_media_processor.py) | Container-specific FFmpeg arguments (`-movflags +faststart` only for MP4 family), real FFmpeg aligned & unaligned keyframe trimming (`-ss`/`-to` before `-i`), partial `.clip_tmp` cleanup on failure, and FFprobe validation. |
| [`tests/test_file_manager.py`](../tests/test_file_manager.py) | Filename sanitization (`MAX_PATH`, Unicode directional controls, Windows reserved device names), `Download Lease` persistence/TTL, full `io.RawIOBase` contract on `9 MiB` payload (`read(1024)`, `read(-1)`, sequential `readinto()`, EOF `b""`, concurrent SHA-256 integrity, early `close()`), `32 MiB` dual-reader `< 2 MiB` `tracemalloc` benchmark, and multi-thread TOCTOU test on `reserve_space()`. |
| [`tests/test_downloader.py`](../tests/test_downloader.py) | 4-stage callbacks, multi-stream DASH byte accounting, `concurrent_fragment_downloads == 1`, **real local HTTP MPEG-DASH `.mpd` + `.m4s` end-to-end download + FFmpeg merge + FFprobe stream verification**, real FFmpeg `TimeRange` clip E2E, and controlled batch tests (5 accepted / 6 rejected, SSRF isolation, FFmpeg item failure, global quota limit, cooperative cancel, per-item transfer retrieval). |
| [`tests/test_app.py`](../tests/test_app.py) | Session state machine transitions, URL-change artifact purge, FIFO > 20 eviction, error-code sanitization, title truncation, cross-session isolation, `cleanup_batch_state()`, and Streamlit `AppTest` UI + batch panel + failed startup diagnostics gate. |
| [`tests/test_diagnostics.py`](../tests/test_diagnostics.py) | All 5 prerequisite checks pass/fail branches and path-free Markdown export. |
| [`tests/test_error_handler.py`](../tests/test_error_handler.py) | Credential/query token/ANSI sanitization and all 8 `ErrorCategory` mappings. |

---

## 2. Audited Test & Coverage Metrics (`126 passed`)

Latest verified local execution via `pytest --cov=core --cov=utils --cov=app --cov-branch --cov-report=term-missing --cov-report=json:quality-reports/latest/coverage.json`:

| Module | Statements | Line Coverage | Branches | Branch Coverage | Combined Coverage |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `app.py` | `362 / 375` | 96.5% | `103 / 116` | 88.8% | **94.7%** |
| `core/downloader.py` | `154 / 161` | 95.7% | `40 / 44` | 90.9% | **94.6%** |
| `core/extractor.py` | `83 / 85` | 97.6% | `35 / 36` | 97.2% | **97.5%** |
| `core/format_selector.py` | `242 / 246` | 98.4% | `118 / 126` | 93.7% | **96.8%** |
| `core/media_processor.py` | `97 / 97` | 100.0% | `32 / 34` | 94.1% | **98.5%** |
| `core/models.py` | `176 / 176` | 100.0% | `6 / 6` | 100.0% | **100.0%** |
| `utils/diagnostics.py` | `74 / 74` | 100.0% | `14 / 14` | 100.0% | **100.0%** |
| `utils/error_handler.py` | `79 / 79` | 100.0% | `17 / 18` | 94.4% | **99.0%** |
| `utils/file_manager.py` | `370 / 407` | 90.9% | `114 / 124` | 91.9% | **91.1%** |
| `utils/validators.py` | `266 / 268` | 99.3% | `99 / 102` | 97.1% | **98.6%** |
| **TOTAL** | **`1903 / 1968`** | **96.7%** | **`578 / 620`** | **93.2%** | **95.9%** |

### Notable Uncovered Defensive Branches
* `core/media_processor.py`: `faststart=False` on an MP4 trim (`222->224`) and non-`mp3`/non-`m4a` audio passthrough branch (`65->74`).
* `utils/file_manager.py`: Defensive OS/symlink/permission failure branches during directory sweeps (`is_symlink`, `OSError` stat catchers) and fallback when `_raw_stream` lacks `readinto` (`102-105`).
* `app.py`: Defensive fallback when no formats match the selected mode (`259-260`) and UI edge branches not triggered in headless `AppTest` click paths.

---

## 3. Continuous Integration & Docker Workflow

Defined in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml):
* **Job 1 (`quality-and-tests`):** Runs on Ubuntu across Python `3.10`, `3.11`, `3.12`, and `3.13` with FFmpeg/FFprobe installed, executing `ruff check .`, `ruff format --check .`, `pyright`, and `pytest --cov-branch` with independent **`>= 90%` statement and `>= 90%` branch** pass criteria.
* **Job 2 (`docker-smoke-test`):** Builds `Dockerfile`, verifies unprivileged `UID 1000`, `ffmpeg`/`ffprobe` availability, `run_startup_diagnostics()`, and polls `http://127.0.0.1:8501/_stcore/health` until `ok`.
