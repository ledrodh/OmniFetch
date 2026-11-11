"""UUID4 workspace isolation, Download Lease, RawIOBase streaming, atomic global quota, and TTL cleanup."""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from utils.error_handler import (
    ResourceLimitExceededError,
    StorageError,
    get_sanitized_logger,
)

logger = get_sanitized_logger(__name__)

_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')
_WHITESPACE_RE = re.compile(r"\s+")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_UUID4_HEX_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_SAFE_FULL_PATH = 235
DEFAULT_STREAM_CHUNK_BYTES = 64 * 1024  # 64 KiB fixed streaming chunk size

# Process-wide atomic storage reservation ledger preventing TOCTOU races across concurrent sessions
_GLOBAL_STORAGE_LOCK = threading.RLock()
_GLOBAL_RESERVATIONS: dict[tuple[str, str], int] = {}


def sanitize_filename(raw_name: str, *, max_length: int = 100, default: str = "media") -> str:
    """Sanitize a filename against illegal characters, path traversal, and Windows MAX_PATH limits."""
    if not raw_name or not isinstance(raw_name, str):
        return default

    normalized = unicodedata.normalize("NFKC", raw_name)
    filtered_chars = [
        ch for ch in normalized if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn")
    ]
    cleaned = _INVALID_FILENAME_CHARS_RE.sub("_", "".join(filtered_chars))
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "_")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip(" ._")

    if not cleaned:
        return default

    stem_candidate = cleaned.split(".")[0].upper()
    if stem_candidate in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    effective_max = max(8, max_length)
    if len(cleaned) > effective_max:
        if "." in cleaned:
            stem, ext = cleaned.rsplit(".", 1)
            if len(ext) <= 10:
                allowed_stem = max(4, effective_max - len(ext) - 1)
                cleaned = f"{stem[:allowed_stem].rstrip(' ._')}.{ext}"
            else:
                cleaned = cleaned[:effective_max].rstrip(" ._")
        else:
            cleaned = cleaned[:effective_max].rstrip(" ._")

    return cleaned or default


def is_valid_operation_id(operation_id: str) -> bool:
    """Return True iff `operation_id` is a valid UUID4 string."""
    return bool(isinstance(operation_id, str) and _UUID4_HEX_RE.match(operation_id))


def _dir_media_bytes(directory: Path) -> int:
    """Return actual media bytes under `directory`, excluding `.lease` markers and symlinks."""
    if not directory.exists():
        return 0
    total = 0
    try:
        for item in directory.rglob("*"):
            if item.is_file() and item.name != ".lease" and not item.is_symlink():
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


class LeasedFileReader(io.RawIOBase):
    """
    `io.RawIOBase`-compliant reader holding a reference-counted lease until `close()`.

    Preserves the standard `RawIOBase` contract (`read(-1)`/`read(None)` reads until EOF)
    while providing `readinto()` to reduce intermediate buffer allocations and
    `iter_chunks()` for bounded 64 KiB streaming consumers.
    """

    def __init__(
        self,
        file_path: Path,
        *,
        on_close_callback: Callable[[], None],
        chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES,
    ) -> None:
        super().__init__()
        self._raw_stream = file_path.open("rb", buffering=chunk_size)
        self._on_close_callback = on_close_callback
        self._chunk_size = max(4096, chunk_size)
        self._callback_invoked = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._raw_stream.seek(offset, whence)

    def tell(self) -> int:
        return self._raw_stream.tell()

    def read(self, size: int | None = -1) -> bytes:
        try:
            if size is None:
                return self._raw_stream.read(-1)
            return self._raw_stream.read(size)
        except Exception:
            self.close()
            raise

    def readinto(self, b: Any) -> int:
        """Read directly into a writable caller buffer, reducing intermediate copies."""
        try:
            readinto_fn = getattr(self._raw_stream, "readinto", None)
            if callable(readinto_fn):
                n = readinto_fn(b)
                return int(n) if isinstance(n, int) else 0
            data = self._raw_stream.read(len(b))
            n = len(data)
            b[:n] = data
            return n
        except Exception:
            self.close()
            raise

    def iter_chunks(self, chunk_size: int | None = None) -> Iterator[bytes]:
        """Stream file contents in fixed-size chunks and close the lease on completion."""
        step = self._chunk_size if chunk_size is None else max(4096, chunk_size)
        try:
            while True:
                chunk = self.read(step)
                if not chunk:
                    break
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        try:
            if not self._raw_stream.closed:
                self._raw_stream.close()
        finally:
            if not self._callback_invoked:
                self._callback_invoked = True
                self._on_close_callback()
            super().close()


class TempFileManager:
    """Manage UUID4 workspaces, Download Leases, atomic global quotas, active readers, and TTL."""

    def __init__(
        self,
        base_dir: Path | None = None,
        ttl_seconds: int = 1800,
    ) -> None:
        env_dir = os.environ.get("OMNIFETCH_TEMP_DIR")
        default_dir = Path(env_dir) if env_dir else (Path(tempfile.gettempdir()) / "omnifetch_temp")
        self._base_dir = (base_dir or default_dir).resolve()
        self._ttl_seconds = ttl_seconds
        self._active_operations: set[str] = set()
        self._leases: dict[str, float] = {}
        self._reader_counts: dict[str, int] = {}
        self._pending_deletion: set[str] = set()
        self._lock = threading.Lock()

    @property
    def base_dir(self) -> Path:
        """Return the resolved base workspace directory."""
        return self._base_dir

    @property
    def ttl_seconds(self) -> int:
        """Return the default workspace time-to-live (TTL) in seconds."""
        return self._ttl_seconds

    def get_active_reader_count(self, operation_id: str) -> int:
        """Return the number of active readers currently streaming `operation_id`."""
        with self._lock:
            return self._reader_counts.get(operation_id, 0)

    def is_pending_deletion(self, operation_id: str) -> bool:
        """Return True if cleanup was deferred until active readers close."""
        with self._lock:
            return operation_id in self._pending_deletion

    def get_total_workspace_bytes(self) -> int:
        """Calculate actual disk bytes occupied across all operations under `base_dir`."""
        return _dir_media_bytes(self._base_dir)

    def get_committed_workspace_bytes(self) -> int:
        """
        Calculate total committed bytes (max of physical disk usage and active atomic reservations)
        across every session sharing this `base_dir`.
        """
        base_key = str(self._base_dir)
        with _GLOBAL_STORAGE_LOCK:
            reservations = {
                op_id: reserved
                for (bdir, op_id), reserved in _GLOBAL_RESERVATIONS.items()
                if bdir == base_key
            }
            if not self._base_dir.exists():
                return sum(reservations.values())

            total = 0
            seen_ops: set[str] = set()
            try:
                for child in self._base_dir.iterdir():
                    if child.is_symlink():
                        continue
                    if child.is_dir() and is_valid_operation_id(child.name):
                        seen_ops.add(child.name)
                        actual = _dir_media_bytes(child)
                        total += max(actual, reservations.get(child.name, 0))
                    elif child.is_dir():
                        total += _dir_media_bytes(child)
                    elif child.is_file() and child.name != ".lease":
                        try:
                            total += child.stat().st_size
                        except OSError:
                            continue
            except OSError:
                pass

            for op_id, reserved in reservations.items():
                if op_id not in seen_ops:
                    total += reserved
            return total

    def reserve_space(
        self,
        operation_id: str,
        requested_bytes: int,
        max_global_bytes: int,
    ) -> int:
        """
        Atomically reserve or expand storage capacity for `operation_id` under global lock.

        Prevents TOCTOU quota overcommit when concurrent sessions initiate downloads
        simultaneously before bytes are written to disk.
        """
        if requested_bytes < 0:
            requested_bytes = 0
        base_key = str(self._base_dir)
        op_key = (base_key, operation_id)

        with _GLOBAL_STORAGE_LOCK:
            current_committed = self.get_committed_workspace_bytes()
            op_disk = _dir_media_bytes(self._base_dir / operation_id)
            prev_reserved = _GLOBAL_RESERVATIONS.get(op_key, 0)
            prev_contribution = max(op_disk, prev_reserved)
            new_contribution = max(op_disk, requested_bytes)
            projected_total = (current_committed - prev_contribution) + new_contribution

            if projected_total > max_global_bytes:
                max_g_mib = max_global_bytes / (1024 * 1024)
                proj_g_mib = projected_total / (1024 * 1024)
                raise ResourceLimitExceededError(
                    f"Global temporary workspace quota ({proj_g_mib:.1f} MB) exceeds the limit ({max_g_mib:.0f} MB).",
                    technical_detail=(
                        f"Atomic reservation rejected for operation_id={operation_id}: "
                        f"projected_total={projected_total} > max_global_bytes={max_global_bytes}"
                    ),
                )

            _GLOBAL_RESERVATIONS[op_key] = requested_bytes
            return projected_total

    def release_reservation(self, operation_id: str) -> None:
        """Release the logical atomic space reservation for `operation_id`."""
        op_key = (str(self._base_dir), operation_id)
        with _GLOBAL_STORAGE_LOCK:
            _GLOBAL_RESERVATIONS.pop(op_key, None)

    def ensure_base_dir(self) -> Path:
        """Ensure the temporary workspace root directory exists."""
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            return self._base_dir
        except OSError as exc:
            raise StorageError(
                technical_detail=f"Failed to create base temp directory {self._base_dir}: {exc}"
            ) from exc

    def create_operation_dir(self, operation_id: str | None = None) -> tuple[str, Path]:
        """Create an isolated UUID4 operation subdirectory guarded against symlinks and traversal."""
        op_id = operation_id or str(uuid.uuid4())
        if not is_valid_operation_id(op_id):
            raise StorageError(technical_detail=f"Invalid operation_id (expected UUID4): {op_id}")

        self.ensure_base_dir()
        raw_op_dir = self._base_dir / op_id
        if raw_op_dir.is_symlink():
            raise StorageError(
                technical_detail=f"Symlink detected and blocked for operation dir: {raw_op_dir}"
            )

        op_dir = raw_op_dir.resolve()
        if not op_dir.is_relative_to(self._base_dir):
            raise StorageError(technical_detail=f"Path escape attempt outside base dir: {op_dir}")

        try:
            op_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                technical_detail=f"Failed to create operation dir {op_id}: {exc}"
            ) from exc

        return op_id, op_dir

    def resolve_safe_path(self, operation_id: str, filename: str) -> Path:
        """Resolve a safe path inside `operation_id` enforcing containment, anti-symlink, and MAX_PATH."""
        if not is_valid_operation_id(operation_id):
            raise StorageError(technical_detail=f"Invalid operation_id: {operation_id}")

        op_dir = (self._base_dir / operation_id).resolve()
        prefix_len = len(str(op_dir)) + 1
        dynamic_max_len = min(120, max(16, _MAX_SAFE_FULL_PATH - prefix_len))
        safe_name = sanitize_filename(filename, max_length=dynamic_max_len)

        raw_candidate = op_dir / safe_name
        if raw_candidate.is_symlink():
            raise StorageError(
                technical_detail=f"Symlink blocked in resolve_safe_path: {raw_candidate}"
            )

        candidate = raw_candidate.resolve()
        if not candidate.is_relative_to(op_dir):
            raise StorageError(technical_detail=f"Path confinement violation detected: {candidate}")
        return candidate

    def acquire_lease(
        self,
        operation_id: str,
        *,
        lease_seconds: int | None = None,
        now: float | None = None,
    ) -> float:
        """Create or renew a Download Lease protecting an operation folder from TTL sweep."""
        if not is_valid_operation_id(operation_id):
            raise StorageError(technical_detail=f"Invalid operation_id for lease: {operation_id}")

        current_time = time.time() if now is None else now
        duration = self._ttl_seconds if lease_seconds is None else lease_seconds
        expires_at = current_time + max(1, duration)

        with self._lock:
            self._leases[operation_id] = expires_at
            self._pending_deletion.discard(operation_id)

        op_dir = self._base_dir / operation_id
        if op_dir.exists() and not op_dir.is_symlink():
            lease_file = op_dir / ".lease"
            try:
                lease_file.write_text(f"{expires_at:.3f}", encoding="utf-8")
                os.utime(lease_file, (current_time, current_time))
            except OSError:
                pass

        return expires_at

    def release_lease(self, operation_id: str, *, now: float | None = None) -> bool:
        """Release the Download Lease so TTL expiration begins from this timestamp."""
        if not is_valid_operation_id(operation_id):
            return False

        current_time = time.time() if now is None else now
        with self._lock:
            had_lease = self._leases.pop(operation_id, None) is not None

        op_dir = self._base_dir / operation_id
        if op_dir.exists() and not op_dir.is_symlink():
            lease_file = op_dir / ".lease"
            try:
                if lease_file.exists():
                    had_lease = True
                lease_file.unlink(missing_ok=True)
                os.utime(op_dir, (current_time, current_time))
            except OSError:
                pass

        return had_lease

    def has_active_lease(self, operation_id: str, *, now: float | None = None) -> bool:
        """Check whether `operation_id` holds a non-expired Download Lease."""
        current_time = time.time() if now is None else now
        with self._lock:
            exp = self._leases.get(operation_id)
            if exp is not None:
                if current_time < exp:
                    return True
                self._leases.pop(operation_id, None)

        lease_file = self._base_dir / operation_id / ".lease"
        if lease_file.is_file() and not lease_file.is_symlink():
            try:
                disk_exp = float(lease_file.read_text(encoding="utf-8").strip())
                if current_time < disk_exp:
                    with self._lock:
                        self._leases[operation_id] = disk_exp
                    return True
                lease_file.unlink(missing_ok=True)
            except (OSError, ValueError):
                try:
                    lease_file.unlink(missing_ok=True)
                except OSError:
                    pass
        return False

    @contextmanager
    def active_operation(
        self,
        operation_id: str,
        *,
        retain_lease_on_success: bool = False,
    ) -> Iterator[Path]:
        """Protect the operation directory while active and optionally grant a Download Lease."""
        op_id, op_dir = self.create_operation_dir(operation_id)
        with self._lock:
            self._active_operations.add(op_id)
        succeeded = False
        try:
            yield op_dir
            succeeded = True
        finally:
            if succeeded and retain_lease_on_success:
                self.acquire_lease(op_id)
            self.release_reservation(op_id)
            with self._lock:
                self._active_operations.discard(op_id)

    def open_leased_reader(
        self,
        operation_id: str,
        file_path: Path,
    ) -> LeasedFileReader:
        """Open a reference-counted `LeasedFileReader` protecting the file until `close()`."""
        resolved = self.resolve_safe_path(operation_id, file_path.name)
        if not resolved.exists() or not resolved.is_file() or resolved.is_symlink():
            raise StorageError(technical_detail=f"File unavailable for transfer: {resolved}")

        with self._lock:
            self._reader_counts[operation_id] = self._reader_counts.get(operation_id, 0) + 1
            self._active_operations.add(operation_id)

        self.acquire_lease(operation_id)

        def _on_close() -> None:
            should_delete_now = False
            with self._lock:
                remaining = self._reader_counts.get(operation_id, 1) - 1
                if remaining <= 0:
                    self._reader_counts.pop(operation_id, None)
                    self._active_operations.discard(operation_id)
                    if operation_id in self._pending_deletion:
                        self._pending_deletion.discard(operation_id)
                        should_delete_now = True
                else:
                    self._reader_counts[operation_id] = remaining

            if should_delete_now:
                self._remove_dir_on_disk(operation_id)

        try:
            return LeasedFileReader(resolved, on_close_callback=_on_close)
        except Exception as exc:
            _on_close()
            raise StorageError(technical_detail=f"Failed to open file for transfer: {exc}") from exc

    def build_transfer_loader(
        self,
        operation_id: str,
        file_path: Path,
    ) -> Callable[[], LeasedFileReader]:
        """Return a thread-safe callable producing a `LeasedFileReader` for `st.download_button`."""

        def _open_stream() -> LeasedFileReader:
            return self.open_leased_reader(operation_id, file_path)

        return _open_stream

    def _remove_dir_on_disk(self, operation_id: str) -> bool:
        """Remove an operation directory physically and clear any residual space reservation."""
        self.release_reservation(operation_id)
        op_dir = (self._base_dir / operation_id).resolve()
        if not op_dir.is_relative_to(self._base_dir) or not op_dir.exists():
            return False
        try:
            shutil.rmtree(op_dir, ignore_errors=False)
            return True
        except OSError as exc:
            logger.warning("Unable to remove directory %s: %s", op_dir, exc)
            return False

    def cleanup_operation(self, operation_id: str, *, force: bool = True) -> bool:
        """Remove an operation folder immediately or defer deletion while active readers remain."""
        if not is_valid_operation_id(operation_id):
            return False

        if force:
            self.release_lease(operation_id)
        elif self.has_active_lease(operation_id):
            return False

        with self._lock:
            active_readers = self._reader_counts.get(operation_id, 0)
            if active_readers > 0:
                self._pending_deletion.add(operation_id)
                return False

        return self._remove_dir_on_disk(operation_id)

    @staticmethod
    def _get_latest_mtime(directory: Path) -> float:
        """Return the most recent modification timestamp across a directory and its media files."""
        latest = directory.stat().st_mtime
        for item in directory.rglob("*"):
            if item.name == ".lease":
                continue
            try:
                mtime = item.stat().st_mtime
                if mtime > latest:
                    latest = mtime
            except OSError:
                continue
        return latest

    def cleanup_expired_files(
        self,
        ttl_seconds: int | None = None,
        *,
        now: float | None = None,
    ) -> int:
        """Sweep expired workspaces or abandoned leases that have no active readers."""
        if not self._base_dir.exists():
            return 0

        effective_ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        current_time = time.time() if now is None else now
        removed_count = 0

        with self._lock:
            active_snapshot = set(self._active_operations) | {
                op_id for op_id, cnt in self._reader_counts.items() if cnt > 0
            }

        for child in self._base_dir.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            if not is_valid_operation_id(child.name):
                continue
            if child.name in active_snapshot:
                continue

            lease_marker = child / ".lease"
            expired_lease_ts: float | None = None
            if lease_marker.is_file() and not lease_marker.is_symlink():
                try:
                    parsed_exp = float(lease_marker.read_text(encoding="utf-8").strip())
                    if current_time >= parsed_exp:
                        expired_lease_ts = parsed_exp
                except (OSError, ValueError):
                    expired_lease_ts = 0.0

            if self.has_active_lease(child.name, now=current_time):
                continue

            try:
                latest_mtime = self._get_latest_mtime(child)
                age = current_time - latest_mtime
                if expired_lease_ts is not None or age >= effective_ttl:
                    shutil.rmtree(child, ignore_errors=False)
                    self.release_reservation(child.name)
                    with self._lock:
                        self._leases.pop(child.name, None)
                        self._pending_deletion.discard(child.name)
                    removed_count += 1
            except OSError as exc:
                logger.warning("Failed to clean up expired directory %s: %s", child, exc)

        return removed_count
