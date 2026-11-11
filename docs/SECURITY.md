# Security Architecture — OmniFetch

This document covers OmniFetch's threat boundaries, layered Anti-SSRF protections, context-isolated DNS guard, filesystem confinement, and test fixture isolation rules.

---

## 1. Scope & Threat Boundary

* **No Authentication or Cookies:** OmniFetch operates strictly on public HTTP/HTTPS media links. Cookie files, login forms, and session tokens are excluded by design to eliminate session leakage vectors.
* **Log & UI Redaction (`utils/error_handler.py`):** All logged technical details pass through `sanitize_log_message()`, stripping ANSI terminal escapes, URL credentials (`https://user:pass@...`), and sensitive query parameters (`token`, `access_token`, `auth`, `sig`, `signature`, `key`, `api_key`, `session`, `hdnea`, `policy`, `key-pair-id`).

---

## 2. Layered Anti-SSRF Defense

1. **Syntactic & Scheme Validation (`validate_url`):**
   * Rejects non-string values, lengths > 2048 bytes, control characters (`\r`, `\n`, `\x00`, `\t`), embedded `user:pass@` URL credentials, invalid ports, and any non-HTTP(S) scheme (`file://`, `ftp://`, `javascript:`, `ytsearch:`).
   * Rejects internal hostnames (`localhost`, `metadata.google.internal`, `*.local`, `*.internal`, `*.lan`, `*.home`, `*.arpa`, `*.corp`, `*.localdomain`) and unqualified single-label hosts.
2. **IPv4 & IPv6 Filtering Across Python 3.10–3.13+ (`_assert_public_ip`):**
   * Rejects loopback, private (RFC 1918), link-local / cloud metadata (`169.254.0.0/16`), Carrier-Grade NAT (`100.64.0.0/10`), multicast, reserved, unspecified, TEST-NET ranges, NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`), 6to4 (`2002::/16`), Teredo (`2001::/23`), and ULA (`fc00::/7`).
   * `extract_embedded_ipv4()` handles both Python `ipaddress` properties and packed binary fallbacks for IPv4-mapped IPv6 (`::ffff:0:0/96`), NAT64, and 6to4 addresses consistently across Python 3.10+ (addressing cross-version differences in `IPv6Address.is_global`).
3. **Post-Extraction Manifest & Fragment Inspection (`_validate_extracted_stream_urls`):**
   * Inspects `webpage_url`, `original_url`, `url`, `manifest_url`, every entry in `formats[*].url` / `manifest_url` / `fragment_base_url`, fragment lists (`fragments[*].url`), and `thumbnail` before presenting metadata or invoking downloads.

---

## 3. Concurrency-Safe, Context-Isolated DNS Guard (`guarded_dns_resolution`)

Validating DNS only before calling `yt-dlp` leaves a **TOCTOU / DNS rebinding** window when `yt-dlp` opens sockets later or follows `301`/`302` redirects. Intercepting `socket.getaddrinfo` naively per thread would create race conditions and corrupt global state on out-of-order exits across concurrent Streamlit sessions.

OmniFetch resolves both issues using four coordinated mechanisms in [`utils/validators.py`](../utils/validators.py):

1. **Single Lock-Protected Dispatcher (`_DNS_GUARD_LOCK` + `_ACTIVE_DNS_GUARDS`):**
   * `guarded_dns_resolution()` maintains a reference count (`_ACTIVE_DNS_GUARDS`) and captures `_BASE_GETADDRINFO` only on the transition `0 -> 1`.
   * Even if Session A enters before Session B and exits *before* Session B, Session B remains protected and the original `socket.getaddrinfo` pointer is restored only when `_ACTIVE_DNS_GUARDS` reaches `0` (including on exceptions).
2. **`ContextVar` Per-Context Depth Isolation (`_DNS_GUARD_DEPTH`):**
   * `_dispatch_guarded_getaddrinfo()` reads `_DNS_GUARD_DEPTH.get()`. Unrelated threads (e.g., Streamlit's internal Tornado HTTP server resolving `localhost`) observe depth `0` and bypass validation without interference.
3. **Transport Layer & Fragment Worker Threads (`SafeYoutubeDL`):**
   * Covered `yt-dlp` backends: standard `urllib` and `requests` handlers over Python's `socket` module.
   * `SafeYoutubeDL.__init__()` and `core/downloader.py` pin `concurrent_fragment_downloads = 1` by default so fragment fetches run deterministically on the calling thread.
   * Each `SafeYoutubeDL.urlopen(req)` call validates pre-request URL, runs `super().urlopen(req)` inside `with guarded_dns_resolution()`, and revalidates the final resolved response URL (`response.url` / `response.geturl()`). Because `yt-dlp`'s `FragmentFD` calls `self.ydl.urlopen()`, even worker threads in a fragment `ThreadPoolExecutor` enter `guarded_dns_resolution()` individually.
   * `run_in_guarded_context()` explicitly copies/propagates `ContextVar` state (`contextvars.copy_context()`) for arbitrary worker threads spawned inside a guarded operation.

---

## 4. Filesystem Confinement & Resource Limits

* **UUID4 Workspace Confinement (`TempFileManager`):**
  * Operation directories require strict UUID4 names (`is_valid_operation_id`), reject symlinks (`is_symlink()`), verify `resolved_candidate.is_relative_to(base_dir)`, sanitize filenames against path traversal/Unicode directional overrides/Windows device names (`CON`, `NUL`, `COM1`, `LPT1`), and cap path lengths for Windows `MAX_PATH`.
* **In-Process Atomic Space Reservation (`reserve_space`):**
  * `OperationalLimits` enforces `2 GiB` per logical media output, `4 GiB` per single operation workspace, and `8 GiB` globally across active sessions **within the OmniFetch process**.
  * Before downloading starts (and as unknown-size streams progress), `TempFileManager.reserve_space()` acquires `_GLOBAL_STORAGE_LOCK`, sums physical bytes and logical reservations across all active operations sharing `base_dir`, and rejects overcommits immediately to prevent TOCTOU races between concurrent sessions.

---

## 5. Local Test Fixture Isolation vs. Production SSRF

Production code (`app.py`, `core/extractor.py`, `core/downloader.py`) enforces `resolve_dns=True` with zero bypass flags or environment toggles. In [`tests/test_downloader.py`](../tests/test_downloader.py), the local MPEG-DASH `.mpd` end-to-end test serves generated fixture segments on `127.0.0.1` strictly inside a test-scoped `with patch("utils.validators._assert_public_hostname", return_value=True):` context alongside `resolve_dns=False`, ensuring loopback access remains confined exclusively to test execution.
