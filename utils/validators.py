"""URL syntax validation, thread-safe Anti-SSRF DNS guard, time-range parsing, limits, and concurrency."""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import Context, ContextVar, copy_context
from typing import Any, TypeVar
from urllib.parse import urljoin, urlsplit

from yt_dlp.YoutubeDL import YoutubeDL

from core.models import OperationalLimits, TimeRange
from utils.error_handler import InvalidURLError, ResourceLimitExceededError

_T = TypeVar("_T")

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
        "instance-data",
        "broadcasthost",
        "ip6-localhost",
        "ip6-loopback",
    }
)
_BLOCKED_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".lan",
    ".home",
    ".arpa",
    ".corp",
    ".localdomain",
)
_RESTRICTED_IPV4_NETWORKS = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),  # RFC 6598 Carrier-Grade NAT
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),  # Link-Local + Cloud Metadata 169.254.169.254
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.0.0.0/24"),  # IETF Protocol Assignments
    ipaddress.IPv4Network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("198.18.0.0/15"),  # Benchmarking
    ipaddress.IPv4Network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.IPv4Network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.IPv4Network("224.0.0.0/4"),  # Multicast
    ipaddress.IPv4Network("240.0.0.0/4"),  # Reserved / Broadcast
)
_RESTRICTED_IPV6_NETWORKS = (
    ipaddress.IPv6Network("::/128"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("::ffff:0:0/96"),  # IPv4-mapped
    ipaddress.IPv6Network("64:ff9b::/96"),  # NAT64 Well-Known Prefix
    ipaddress.IPv6Network("64:ff9b:1::/48"),  # NAT64 Local-Use Prefix
    ipaddress.IPv6Network("100::/64"),  # Discard-Only
    ipaddress.IPv6Network("2001::/23"),  # IETF Protocol Assignments + Teredo
    ipaddress.IPv6Network("2001:db8::/32"),  # Documentation
    ipaddress.IPv6Network("2002::/16"),  # 6to4
    ipaddress.IPv6Network("fc00::/7"),  # Unique Local (ULA)
    ipaddress.IPv6Network("fe80::/10"),  # Link-Local
    ipaddress.IPv6Network("ff00::/8"),  # Multicast
)
_IPV4_MAPPED_PREFIX = ipaddress.IPv6Network("::ffff:0:0/96")
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_SIX_TO_FOUR_PREFIX = ipaddress.IPv6Network("2002::/16")
_MAX_URL_LENGTH = 2048
_TIMESTAMP_RE = re.compile(r"^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$")

# Thread-safe and context-isolated DNS interception without global monkeypatch races
_STDLIB_GETADDRINFO: Callable[..., list[Any]] = socket.getaddrinfo
_DNS_GUARD_LOCK = threading.Lock()
_ACTIVE_DNS_GUARDS: int = 0
_BASE_GETADDRINFO: Callable[..., list[Any]] | None = None
_DNS_GUARD_DEPTH: ContextVar[int] = ContextVar("_DNS_GUARD_DEPTH", default=0)


def extract_embedded_ipv4(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """Extract embedded IPv4 from IPv6 addresses (IPv4-mapped, NAT64, 6to4) on Python 3.10+."""
    if isinstance(ip_obj, ipaddress.IPv4Address):
        return ip_obj

    mapped = getattr(ip_obj, "ipv4_mapped", None)
    if isinstance(mapped, ipaddress.IPv4Address):
        return mapped

    sixtofour = getattr(ip_obj, "sixtofour", None)
    if isinstance(sixtofour, ipaddress.IPv4Address):
        return sixtofour

    packed = ip_obj.packed
    if ip_obj in _IPV4_MAPPED_PREFIX or ip_obj in _NAT64_PREFIX:
        return ipaddress.IPv4Address(packed[12:16])
    if ip_obj in _SIX_TO_FOUR_PREFIX:
        return ipaddress.IPv4Address(packed[2:6])
    return None


def _assert_public_ip(raw_ip: str) -> None:
    """Ensure a resolved IP address is strictly public across Python 3.10 through 3.13+."""
    try:
        ip_obj = ipaddress.ip_address(raw_ip)
    except ValueError as exc:
        raise InvalidURLError(
            technical_detail=f"Invalid IP address in DNS resolution: {raw_ip}"
        ) from exc

    embedded_v4 = extract_embedded_ipv4(ip_obj)
    if embedded_v4 is not None:
        ip_obj = embedded_v4

    if isinstance(ip_obj, ipaddress.IPv4Address):
        if (
            ip_obj.is_loopback
            or ip_obj.is_private
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
            or not ip_obj.is_global
            or any(ip_obj in net for net in _RESTRICTED_IPV4_NETWORKS)
        ):
            raise InvalidURLError(
                technical_detail=f"Anti-SSRF block triggered for restricted IPv4: {ip_obj}"
            )
        return

    if (
        ip_obj.is_loopback
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or not ip_obj.is_global
        or any(ip_obj in net for net in _RESTRICTED_IPV6_NETWORKS)
    ):
        raise InvalidURLError(
            technical_detail=f"Anti-SSRF block triggered for restricted IPv6: {ip_obj}"
        )


def _assert_public_hostname(hostname: str) -> bool:
    """Validate hostname is not local/internal and return True if hostname is an IP literal."""
    clean_host = hostname.strip().lower().rstrip(".")
    if not clean_host:
        raise InvalidURLError(technical_detail="Missing hostname in URL.")

    if clean_host in _BLOCKED_HOSTNAMES or clean_host.endswith(_BLOCKED_SUFFIXES):
        raise InvalidURLError(
            technical_detail=f"Anti-SSRF block triggered for restricted hostname: {clean_host}"
        )

    is_ip_literal = False
    try:
        ipaddress.ip_address(clean_host)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False

    if is_ip_literal:
        _assert_public_ip(clean_host)
        return True

    if "." not in clean_host:
        raise InvalidURLError(
            technical_detail=f"Hostname '{clean_host}' is not a fully qualified public domain."
        )
    return False


def _validate_addrinfo_entries(hostname: str, entries: list[Any]) -> None:
    """Verify that getaddrinfo returned at least one address and every entry is strictly public."""
    if not entries:
        raise InvalidURLError(technical_detail=f"No DNS records returned for '{hostname}'.")
    for entry in entries:
        sockaddr = entry[4]
        resolved_ip = str(sockaddr[0]).split("%")[0]
        _assert_public_ip(resolved_ip)


def validate_url(url: str, *, resolve_dns: bool = True) -> str:
    """Validate HTTP/HTTPS syntax and guard against SSRF by inspecting all DNS records."""
    if not isinstance(url, str):
        raise InvalidURLError(technical_detail="Provided URL is not a string.")

    cleaned = url.strip()
    if not cleaned or len(cleaned) > _MAX_URL_LENGTH:
        raise InvalidURLError(technical_detail="URL is empty or exceeds maximum allowed length.")

    if any(ch in cleaned for ch in ("\r", "\n", "\x00", "\t")):
        raise InvalidURLError(technical_detail="URL contains invalid control characters.")

    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise InvalidURLError(technical_detail=f"Failed to parse URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidURLError(
            technical_detail=f"Scheme '{scheme}' is not allowed. Use http or https only."
        )

    if parsed.username is not None or parsed.password is not None:
        raise InvalidURLError(technical_detail="Embedded URL credentials are not allowed.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidURLError(technical_detail=f"Invalid URL port: {exc}") from exc

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    is_literal = _assert_public_hostname(hostname)
    if is_literal:
        return cleaned

    if resolve_dns:
        target_port = port or (443 if scheme == "https" else 80)
        try:
            addr_info = socket.getaddrinfo(
                hostname,
                target_port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise InvalidURLError(
                technical_detail=f"Unable to resolve DNS for '{hostname}': {exc}"
            ) from exc

        _validate_addrinfo_entries(hostname, addr_info)

    return cleaned


def _dispatch_guarded_getaddrinfo(
    host: Any,
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[Any]:
    """Context-aware DNS dispatcher: enforces SSRF validation only in guarded execution contexts."""
    with _DNS_GUARD_LOCK:
        underlying = _BASE_GETADDRINFO or _STDLIB_GETADDRINFO

    if _DNS_GUARD_DEPTH.get() <= 0:
        return underlying(host, port, family, type, proto, flags)

    host_str = host.decode("utf-8", errors="ignore") if isinstance(host, bytes) else str(host)
    _assert_public_hostname(host_str)
    results = underlying(host, port, family, type, proto, flags)
    _validate_addrinfo_entries(host_str, results)
    return results


@contextmanager
def guarded_dns_resolution(*, enabled: bool = True) -> Iterator[None]:
    """
    Reentrant, concurrency-safe context manager guarding DNS resolution in the active Context.

    - Isolates interception via `ContextVar` (`_DNS_GUARD_DEPTH`), leaving external threads
      (such as Streamlit's Tornado loop or unrelated threads) untouched.
    - Uses lock-protected reference counting (`_ACTIVE_DNS_GUARDS`) to restore the exact
      original `socket.getaddrinfo` even on out-of-order context exits or exceptions.
    - Note: worker threads do not inherit `ContextVar` automatically unless run via
      `run_in_guarded_context()` or calling `SafeYoutubeDL.urlopen()` directly.
    """
    global _ACTIVE_DNS_GUARDS, _BASE_GETADDRINFO
    if not enabled:
        yield
        return

    with _DNS_GUARD_LOCK:
        if _ACTIVE_DNS_GUARDS == 0 or socket.getaddrinfo is not _dispatch_guarded_getaddrinfo:
            _BASE_GETADDRINFO = socket.getaddrinfo
            socket.getaddrinfo = _dispatch_guarded_getaddrinfo  # type: ignore[assignment]
        _ACTIVE_DNS_GUARDS += 1

    _DNS_GUARD_DEPTH.set(_DNS_GUARD_DEPTH.get() + 1)
    try:
        yield
    finally:
        _DNS_GUARD_DEPTH.set(max(0, _DNS_GUARD_DEPTH.get() - 1))
        with _DNS_GUARD_LOCK:
            _ACTIVE_DNS_GUARDS = max(0, _ACTIVE_DNS_GUARDS - 1)
            if _ACTIVE_DNS_GUARDS == 0 and _BASE_GETADDRINFO is not None:
                socket.getaddrinfo = _BASE_GETADDRINFO  # type: ignore[assignment]
                _BASE_GETADDRINFO = None


def run_in_guarded_context(
    fn: Callable[..., _T],
    *args: Any,
    parent_context: Context | None = None,
    enabled: bool = True,
    **kwargs: Any,
) -> _T:
    """
    Execute `fn` in a worker thread with guaranteed Anti-SSRF DNS guard propagation.

    When `parent_context` is supplied via `copy_context()` and already has an active
    DNS guard, it preserves the dispatcher guard inside the worker thread; otherwise, it
    enters `guarded_dns_resolution(enabled=enabled)` explicitly on the worker thread.
    """
    ctx = parent_context if parent_context is not None else copy_context()
    current_depth = ctx.get(_DNS_GUARD_DEPTH, 0)
    if enabled and (current_depth is None or current_depth <= 0):

        def _wrapped() -> _T:
            with guarded_dns_resolution(enabled=True):
                return fn(*args, **kwargs)

        return ctx.run(_wrapped)
    return ctx.run(fn, *args, **kwargs)


def validate_redirect_hop(
    source_url: str, location_header: str, *, resolve_dns: bool = True
) -> str:
    """Resolve a relative or absolute Location redirect target and validate against SSRF."""
    if not location_header or not isinstance(location_header, str):
        raise InvalidURLError(technical_detail="Empty redirect Location header.")
    resolved_target = urljoin(source_url, location_header.strip())
    return validate_url(resolved_target, resolve_dns=resolve_dns)


class SafeYoutubeDL(YoutubeDL):
    """
    YoutubeDL subclass controlling yt-dlp HTTP/socket transport for SSRF protection.

    Covered backends:
    - yt-dlp's standard HTTP request handlers (`urllib` and `requests` over Python's
      `socket` module) run inside `guarded_dns_resolution()` on each `urlopen()` call,
      blocking non-public IPs before TCP connect (including redirects and DNS rebinding).
    - Because `FragmentFD` concurrent fragment threads execute `self.ydl.urlopen()`,
      each fragment worker thread also enters `guarded_dns_resolution()` individually,
      and `concurrent_fragment_downloads` defaults to `1` for sequential determinism.
    """

    def __init__(
        self,
        params: dict[str, Any] | None = None,
        auto_init: bool = True,
        *,
        resolve_dns: bool = True,
    ) -> None:
        self._resolve_dns = resolve_dns
        merged_params: dict[str, Any] = dict(params or {})
        merged_params.setdefault("allowed_extractors", ["default"])
        merged_params.setdefault("concurrent_fragment_downloads", 1)
        super().__init__(params=merged_params, auto_init=auto_init)  # type: ignore[arg-type]

    def urlopen(self, req: Any) -> Any:
        """Validate target URL, bind DNS guard for the thread/context, and revalidate final URL."""
        req_url = (
            req
            if isinstance(req, str)
            else getattr(req, "url", None) or getattr(req, "full_url", None)
        )
        if isinstance(req_url, str):
            validate_url(req_url, resolve_dns=self._resolve_dns)

        with guarded_dns_resolution(enabled=self._resolve_dns):
            response = super().urlopen(req)

        final_url = getattr(response, "url", None)
        if final_url is None and hasattr(response, "geturl"):
            final_url = response.geturl()
        if isinstance(final_url, str) and final_url:
            validate_url(final_url, resolve_dns=self._resolve_dns)

        return response


def parse_timestamp(raw_value: str) -> float:
    """Parse timestamp string ('SS', 'MM:SS', or 'HH:MM:SS') into non-negative float seconds."""
    if not isinstance(raw_value, str):
        raise InvalidURLError(
            "Invalid time format for clipping.",
            technical_detail="Timestamp is not a string.",
        )
    cleaned = raw_value.strip()
    if not cleaned or not _TIMESTAMP_RE.match(cleaned):
        raise InvalidURLError(
            "Invalid time format. Use SS, MM:SS, or HH:MM:SS (e.g. 01:30).",
            technical_detail=f"Invalid timestamp: '{raw_value}'",
        )

    parts = cleaned.split(":")
    if len(parts) == 1:
        return round(float(parts[0]), 3)
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        if seconds >= 60.0:
            raise InvalidURLError(
                "Seconds in MM:SS format must be between 00 and 59.",
                technical_detail=f"Seconds >= 60 in '{raw_value}'",
            )
        return round(minutes * 60.0 + seconds, 3)

    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    if minutes >= 60 or seconds >= 60.0:
        raise InvalidURLError(
            "Minutes and seconds in HH:MM:SS format must be between 00 and 59.",
            technical_detail=f"Minutes/seconds >= 60 in '{raw_value}'",
        )
    return round(hours * 3600.0 + minutes * 60.0 + seconds, 3)


def validate_time_range(
    start_raw: str | None,
    end_raw: str | None,
    *,
    media_duration: float | None = None,
    is_live: bool = False,
) -> TimeRange | None:
    """Validate a temporal clipping range against media duration and live-stream status."""
    s_clean = (start_raw or "").strip()
    e_clean = (end_raw or "").strip()
    if not s_clean and not e_clean:
        return None

    if is_live:
        raise ResourceLimitExceededError(
            "Temporal clipping is not available for live streams.",
            technical_detail="Attempted to apply TimeRange to an is_live=True stream.",
        )

    if not s_clean or not e_clean:
        raise InvalidURLError(
            "Provide both start and end timestamps to clip a segment.",
            technical_detail=f"Incomplete TimeRange: start='{s_clean}', end='{e_clean}'",
        )

    start_sec = parse_timestamp(s_clean)
    end_sec = parse_timestamp(e_clean)
    if end_sec <= start_sec:
        raise InvalidURLError(
            "End time must be greater than start time.",
            technical_detail=f"end_sec ({end_sec}) <= start_sec ({start_sec})",
        )

    if end_sec - start_sec < 1.0:
        raise InvalidURLError(
            "Clipped segment duration must be at least 1 second.",
            technical_detail=f"Clip duration too short: {end_sec - start_sec}s",
        )

    if media_duration is not None and media_duration > 0 and end_sec > media_duration + 0.5:
        raise ResourceLimitExceededError(
            "Clip end time exceeds the total media duration.",
            technical_detail=f"end_sec ({end_sec}) > media_duration ({media_duration})",
        )

    return TimeRange(start_seconds=start_sec, end_seconds=end_sec)


def validate_media_limits(
    *,
    duration_seconds: float | None = None,
    filesize_bytes: int | None = None,
    workspace_bytes: int | None = None,
    global_workspace_bytes: int | None = None,
    limits: OperationalLimits | None = None,
) -> None:
    """Validate duration, per-media filesize, session workspace, and global storage quotas."""
    active_limits = limits or OperationalLimits()

    if duration_seconds is not None and duration_seconds > active_limits.max_duration_seconds:
        max_min = active_limits.max_duration_seconds // 60
        cur_min = int(duration_seconds // 60)
        raise ResourceLimitExceededError(
            f"Media duration ({cur_min} min) exceeds the maximum allowed ({max_min} min).",
            technical_detail=(
                f"duration_seconds={duration_seconds} > "
                f"max_duration_seconds={active_limits.max_duration_seconds}"
            ),
        )

    if filesize_bytes is not None and filesize_bytes > active_limits.max_filesize_bytes:
        max_mib = active_limits.max_filesize_bytes / (1024 * 1024)
        cur_mib = filesize_bytes / (1024 * 1024)
        raise ResourceLimitExceededError(
            f"File size ({cur_mib:.1f} MB) exceeds the per-media limit ({max_mib:.0f} MB).",
            technical_detail=(
                f"filesize_bytes={filesize_bytes} > "
                f"max_filesize_bytes={active_limits.max_filesize_bytes}"
            ),
        )

    if workspace_bytes is not None and workspace_bytes > active_limits.max_workspace_bytes:
        max_ws_mib = active_limits.max_workspace_bytes / (1024 * 1024)
        cur_ws_mib = workspace_bytes / (1024 * 1024)
        raise ResourceLimitExceededError(
            f"Operation workspace ({cur_ws_mib:.1f} MB) exceeds the session limit ({max_ws_mib:.0f} MB).",
            technical_detail=(
                f"workspace_bytes={workspace_bytes} > "
                f"max_workspace_bytes={active_limits.max_workspace_bytes}"
            ),
        )

    if (
        global_workspace_bytes is not None
        and global_workspace_bytes > active_limits.max_global_workspace_bytes
    ):
        max_g_mib = active_limits.max_global_workspace_bytes / (1024 * 1024)
        cur_g_mib = global_workspace_bytes / (1024 * 1024)
        raise ResourceLimitExceededError(
            f"Global temporary workspace quota ({cur_g_mib:.1f} MB) exceeds the limit ({max_g_mib:.0f} MB).",
            technical_detail=(
                f"global_workspace_bytes={global_workspace_bytes} > "
                f"max_global_workspace_bytes={active_limits.max_global_workspace_bytes}"
            ),
        )


class ConcurrencyGuard:
    """Thread-safe concurrency limiter for active operations within the Python process."""

    def __init__(self, max_concurrent: int = 2) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._max_concurrent = max_concurrent
        self._semaphore = threading.BoundedSemaphore(max_concurrent)

    @property
    def max_concurrent(self) -> int:
        """Return the maximum number of simultaneous operations."""
        return self._max_concurrent

    @contextmanager
    def acquire_slot(self, timeout: float = 0.05) -> Iterator[None]:
        """Acquire an execution slot with timeout or raise ResourceLimitExceededError."""
        acquired = self._semaphore.acquire(
            blocking=timeout > 0,
            timeout=timeout if timeout > 0 else None,
        )
        if not acquired:
            raise ResourceLimitExceededError(
                "Maximum concurrent operations reached. Wait for an active task to finish.",
                technical_detail=f"Semaphore exhausted (max_concurrent={self._max_concurrent}).",
            )
        try:
            yield
        finally:
            self._semaphore.release()


_DEFAULT_GUARDS: dict[int, ConcurrencyGuard] = {}
_GUARD_LOCK = threading.Lock()


@contextmanager
def operation_slot(
    limits: OperationalLimits | None = None,
    *,
    guard: ConcurrencyGuard | None = None,
    timeout: float = 0.05,
) -> Iterator[None]:
    """Context manager enforcing concurrent operation limits with guaranteed release."""
    if guard is not None:
        with guard.acquire_slot(timeout=timeout):
            yield
        return

    active_limits = limits or OperationalLimits()
    with _GUARD_LOCK:
        target_guard = _DEFAULT_GUARDS.get(active_limits.max_concurrent_operations)
        if target_guard is None:
            target_guard = ConcurrencyGuard(active_limits.max_concurrent_operations)
            _DEFAULT_GUARDS[active_limits.max_concurrent_operations] = target_guard

    with target_guard.acquire_slot(timeout=timeout):
        yield
