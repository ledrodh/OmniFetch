"""Unit, contract, concurrency, and memory tests for `utils/file_manager.py`."""

from __future__ import annotations

import hashlib
import os
import threading
import tracemalloc
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from utils.error_handler import ResourceLimitExceededError, StorageError
from utils.file_manager import (
    DEFAULT_STREAM_CHUNK_BYTES,
    TempFileManager,
    is_valid_operation_id,
    sanitize_filename,
)


class TestFilenameSanitization:
    """Valida sanitizacao de nomes contra Path Traversal, Unicode combinante, controle e MAX_PATH."""

    def test_removes_dangerous_characters_and_traversal(self) -> None:
        raw = '../../etc/passwd<>:"/\\|?*\x00\x1f\x7fvideo.mp4'
        cleaned = sanitize_filename(raw)
        assert ".." not in cleaned
        assert "/" not in cleaned
        assert "\\" not in cleaned
        assert "\x00" not in cleaned

    def test_unicode_combining_and_invisible_format_chars_and_trailing_dots(self) -> None:
        raw = "Caf\u0065\u0301 \u200b\u202e Vídeo . . .   "
        cleaned = sanitize_filename(raw)
        assert "\u200b" not in cleaned
        assert "\u202e" not in cleaned
        assert not cleaned.endswith(".")
        assert not cleaned.endswith(" ")
        assert "Café" in cleaned

    def test_handles_windows_reserved_device_names(self) -> None:
        assert sanitize_filename("CON.mp4").startswith("_")
        assert sanitize_filename("NUL").startswith("_")
        assert sanitize_filename("COM1.txt").startswith("_")
        assert sanitize_filename("LPT9.mkv").startswith("_")

    def test_truncates_preserving_extension_and_handles_empty(self) -> None:
        long_title = ("A" * 200) + ".mp4"
        truncated = sanitize_filename(long_title, max_length=50)
        assert len(truncated) <= 50
        assert truncated.endswith(".mp4")
        assert sanitize_filename("", default="fallback") == "fallback"
        assert sanitize_filename("   ...___   ", default="fallback") == "fallback"


class TestTempFileManagerAndDownloadLease:
    """Valida isolamento por operacao, Download Lease, memoria constante e expurgo por TTL."""

    def test_validates_uuid4_operation_id(self) -> None:
        valid_id = str(uuid.uuid4())
        assert is_valid_operation_id(valid_id) is True
        assert is_valid_operation_id("../escape") is False
        assert is_valid_operation_id("not-a-uuid") is False

    def test_create_resolve_global_quota_and_cleanup_operation_dir(self, tmp_path: Path) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "omnifetch_store", ttl_seconds=60)
        assert mgr.ttl_seconds == 60
        assert mgr.get_total_workspace_bytes() == 0

        op_id, op_dir = mgr.create_operation_dir()
        assert op_dir.exists()
        assert op_dir.parent == mgr.base_dir

        safe_file = mgr.resolve_safe_path(op_id, "../Tentativa: Escapar?.mp4")
        assert safe_file.parent == op_dir
        assert len(str(safe_file)) <= 240
        safe_file.write_bytes(b"1234567890")
        mgr.acquire_lease(op_id)
        # .lease nao e contabilizado no espaco util de midia
        assert mgr.get_total_workspace_bytes() == 10

        assert mgr.cleanup_operation(op_id) is True
        assert not op_dir.exists()
        assert mgr.get_total_workspace_bytes() == 0
        assert mgr.cleanup_operation("invalid-id") is False

    def test_blocks_symlinks_in_operation_dir_and_resolved_file(self, tmp_path: Path) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "symlink_test")
        op_id, _ = mgr.create_operation_dir()

        with patch.object(Path, "is_symlink", return_value=True):
            with pytest.raises(StorageError):
                mgr.create_operation_dir(str(uuid.uuid4()))
            with pytest.raises(StorageError):
                mgr.resolve_safe_path(op_id, "video.mp4")

    def test_download_lease_protects_file_after_processing_and_during_transfer(
        self, tmp_path: Path
    ) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "lease_store", ttl_seconds=100)
        op_id = str(uuid.uuid4())
        old_ts = 1_000_000.0

        with mgr.active_operation(op_id, retain_lease_on_success=True) as op_dir:
            out_file = op_dir / "ready.mp4"
            out_file.write_bytes(b"video_payload_bytes")
            os.utime(out_file, (old_ts, old_ts))
            os.utime(op_dir, (old_ts, old_ts))

        assert mgr.has_active_lease(op_id) is True
        removed_while_leased = mgr.cleanup_expired_files(ttl_seconds=100)
        assert removed_while_leased == 0
        assert out_file.exists()

        assert mgr.cleanup_operation(op_id, force=False) is False
        assert out_file.exists()

        loader = mgr.build_transfer_loader(op_id, out_file)
        with loader() as reader:
            assert reader.readable() is True
            assert reader.seekable() is True
            assert reader.tell() == 0
            assert reader.read(5) == b"video"
            reader.seek(0)
            buf = bytearray(5)
            assert reader.readinto(buf) == 5
            assert bytes(buf) == b"video"
            assert reader.read() == b"_payload_bytes"

        assert mgr.release_lease(op_id, now=old_ts) is True
        assert mgr.has_active_lease(op_id, now=old_ts + 10) is False

        removed_after_release = mgr.cleanup_expired_files(ttl_seconds=100, now=old_ts + 250)
        assert removed_after_release == 1
        assert not op_dir.exists()

    def test_leased_file_reader_bounded_memory_during_concurrent_large_transfers(
        self, tmp_path: Path
    ) -> None:
        """
        Teste de Desempenho e Memoria (Auditoria Item 3):
        Transfere simultaneamente um arquivo de 32 MiB em duas conexoes paralelas usando
        LeasedFileReader.iter_chunks() e readinto(), comprovando que o pico de alocacao
        no heap do Python permanece limitado (< 2 MiB) em vez de carregar 32 MiB/64 MiB em RAM.
        """
        mgr = TempFileManager(base_dir=tmp_path / "mem_bench")
        op_id, op_dir = mgr.create_operation_dir()
        large_file = op_dir / "large_stream.mp4"
        file_size_bytes = 32 * 1024 * 1024  # 32 MiB
        block = b"X" * (1024 * 1024)
        with large_file.open("wb") as f:
            for _ in range(32):
                f.write(block)
        del block

        tracemalloc.start()
        tracemalloc.reset_peak()

        reader_a = mgr.open_leased_reader(op_id, large_file)
        reader_b = mgr.open_leased_reader(op_id, large_file)
        assert mgr.get_active_reader_count(op_id) == 2

        total_a = 0
        total_b = 0
        reusable_buf = bytearray(DEFAULT_STREAM_CHUNK_BYTES)

        for chunk_a in reader_a.iter_chunks(DEFAULT_STREAM_CHUNK_BYTES):
            total_a += len(chunk_a)
            n_b = reader_b.readinto(reusable_buf)
            total_b += n_b

        reader_b.close()
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert total_a == file_size_bytes
        assert total_b == file_size_bytes
        assert mgr.get_active_reader_count(op_id) == 0
        # Pico de memoria alocada durante a leitura dupla de 32 MiB (64 MiB transferidos) < 2 MiB!
        assert peak_bytes < 2 * 1024 * 1024, f"Pico de memoria excessivo: {peak_bytes} bytes"

    def test_concurrent_readers_defer_deletion_until_last_reader_closes(
        self, tmp_path: Path
    ) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "concurrent_readers", ttl_seconds=300)
        op_id, op_dir = mgr.create_operation_dir()
        target_file = op_dir / "movie.mp4"
        target_file.write_bytes(b"0123456789ABCDEF")

        reader1 = mgr.open_leased_reader(op_id, target_file)
        reader2 = mgr.open_leased_reader(op_id, target_file)
        assert mgr.get_active_reader_count(op_id) == 2

        deleted_immediately = mgr.cleanup_operation(op_id, force=True)
        assert deleted_immediately is False
        assert mgr.is_pending_deletion(op_id) is True
        assert target_file.exists()

        assert mgr.cleanup_expired_files(ttl_seconds=0) == 0
        assert target_file.exists()

        assert reader1.read(4) == b"0123"
        reader1.close()
        assert mgr.get_active_reader_count(op_id) == 1
        assert target_file.exists()

        assert reader2.read() == b"0123456789ABCDEF"
        reader2.close()
        assert mgr.get_active_reader_count(op_id) == 0
        assert mgr.is_pending_deletion(op_id) is False
        assert not op_dir.exists()

    def test_abandoned_lease_after_crash_is_cleaned_up_on_expiration(self, tmp_path: Path) -> None:
        base = tmp_path / "abandoned_lease_store"
        mgr_before_crash = TempFileManager(base_dir=base, ttl_seconds=60)
        op_id, op_dir = mgr_before_crash.create_operation_dir()
        (op_dir / "video.mp4").write_bytes(b"data")
        mgr_before_crash.acquire_lease(op_id, lease_seconds=60, now=1_000_000.0)

        mgr_after_restart = TempFileManager(base_dir=base, ttl_seconds=60)
        assert mgr_after_restart.cleanup_expired_files(ttl_seconds=60, now=1_000_030.0) == 0
        assert op_dir.exists()

        assert mgr_after_restart.cleanup_expired_files(ttl_seconds=60, now=1_000_065.0) == 1
        assert not op_dir.exists()

    def test_corrupted_lease_file_and_interrupted_read_and_permission_failures(
        self, tmp_path: Path
    ) -> None:
        mgr = TempFileManager(base_dir=tmp_path / "err_store")
        op_id, op_dir = mgr.create_operation_dir()
        fpath = op_dir / "stream.mp4"
        fpath.write_bytes(b"stream_data")

        # Lease corrompido em disco deve ser tratado como expirado e removido com seguranca
        mgr._leases.clear()
        (op_dir / ".lease").write_text("not-a-float", encoding="utf-8")
        assert mgr.has_active_lease(op_id) is False

        reader = mgr.open_leased_reader(op_id, fpath)
        assert mgr.get_active_reader_count(op_id) == 1
        with patch.object(reader._raw_stream, "read", side_effect=OSError("Disk read failure")):
            with pytest.raises(OSError):
                reader.read(4)
        assert mgr.get_active_reader_count(op_id) == 0

        # Testa readinto com erro e fallback sem readinto
        reader2 = mgr.open_leased_reader(op_id, fpath)
        with patch.object(reader2._raw_stream, "readinto", side_effect=OSError("readinto error")):
            with pytest.raises(OSError):
                reader2.readinto(bytearray(4))
        assert mgr.get_active_reader_count(op_id) == 0

        with patch("shutil.rmtree", side_effect=PermissionError("Access denied")):
            assert mgr.cleanup_operation(op_id, force=True) is False

    def test_disk_lease_recovery_across_instances(self, tmp_path: Path) -> None:
        base = tmp_path / "shared_store"
        mgr1 = TempFileManager(base_dir=base, ttl_seconds=300)
        op_id, _ = mgr1.create_operation_dir()
        mgr1.acquire_lease(op_id, lease_seconds=300, now=2_000_000.0)

        mgr2 = TempFileManager(base_dir=base, ttl_seconds=300)
        assert mgr2.has_active_lease(op_id, now=2_000_100.0) is True
        assert mgr2.has_active_lease(op_id, now=2_000_500.0) is False

    def test_edge_cases_in_sanitization_and_cleanup(self, tmp_path: Path) -> None:
        assert len(sanitize_filename("B" * 150, max_length=30)) == 30
        assert len(sanitize_filename(("C" * 100) + ".superlongextensionname", max_length=30)) == 30

        mgr = TempFileManager(base_dir=tmp_path / "nonexistent_base")
        assert mgr.cleanup_expired_files() == 0
        assert mgr.release_lease("not-a-uuid") is False

        with pytest.raises(StorageError):
            mgr.resolve_safe_path("invalid-uuid", "a.mp4")
        with pytest.raises(StorageError):
            mgr.acquire_lease("invalid-uuid")

        op_id, op_dir = mgr.create_operation_dir()
        (mgr.base_dir / "not_a_uuid_dir").mkdir(exist_ok=True)
        (mgr.base_dir / "loose_file.txt").write_text("hi", encoding="utf-8")
        assert mgr.cleanup_expired_files(ttl_seconds=9999) == 0

        missing_path = op_dir / "deleted.mp4"
        loader = mgr.build_transfer_loader(op_id, missing_path)
        with pytest.raises(StorageError):
            loader()

    def test_leased_file_reader_preserves_full_rawiobase_contract(self, tmp_path: Path) -> None:
        """
        Validate all 6 RawIOBase contracts required by audit Item 2:
        1. `read(1024)` returns up to 1024 bytes;
        2. `read(-1)` returns the full file (no silent 8 MiB truncation on a 9 MiB payload);
        3. Sequential `readinto()` calls read every byte without loss;
        4. Reading past EOF returns empty `b""`;
        5. Concurrent threaded readers preserve identical SHA-256 digests;
        6. Early `close()` immediately decrements reader counts and releases pending cleanup.
        """
        mgr = TempFileManager(base_dir=tmp_path / "contract_store")
        op_id, op_dir = mgr.create_operation_dir()
        payload_file = op_dir / "nine_mib.bin"

        # Construct a 9 MiB deterministic payload (> previous 8 MiB boundary)
        chunk_1m = bytes(i % 251 for i in range(1024 * 1024))
        expected_hasher = hashlib.sha256()
        with payload_file.open("wb") as f:
            for _ in range(9):
                f.write(chunk_1m)
                expected_hasher.update(chunk_1m)
        expected_digest = expected_hasher.hexdigest()
        expected_size = 9 * 1024 * 1024

        # 1 & 4. read(1024) + EOF returns b"" + read(-1) returns complete remainder
        with mgr.open_leased_reader(op_id, payload_file) as reader:
            head = reader.read(1024)
            assert len(head) == 1024
            rest = reader.read(-1)
            assert len(head) + len(rest) == expected_size
            assert hashlib.sha256(head + rest).hexdigest() == expected_digest
            assert reader.read(1024) == b""
            assert reader.read(-1) == b""

        # 2. read(None) / read() returns full 9 MiB file
        with mgr.open_leased_reader(op_id, payload_file) as reader:
            full_bytes = reader.read(None)
            assert len(full_bytes) == expected_size
            assert hashlib.sha256(full_bytes).hexdigest() == expected_digest

        # 3. Multiple readinto() calls assemble complete file with zero dropped bytes
        with mgr.open_leased_reader(op_id, payload_file) as reader:
            buf = bytearray(65536)
            h_readinto = hashlib.sha256()
            total_readinto = 0
            while True:
                n = reader.readinto(buf)
                if n == 0:
                    break
                h_readinto.update(memoryview(buf)[:n])
                total_readinto += n
            assert total_readinto == expected_size
            assert h_readinto.hexdigest() == expected_digest
            assert reader.readinto(buf) == 0

        # 5. Concurrent readers in worker threads (simulating Streamlit transfer callable threads)
        loader = mgr.build_transfer_loader(op_id, payload_file)

        def _consume_transfer_thread() -> tuple[int, str]:
            h_worker = hashlib.sha256()
            total = 0
            with loader() as r:
                for chunk in r.iter_chunks(DEFAULT_STREAM_CHUNK_BYTES):
                    h_worker.update(chunk)
                    total += len(chunk)
            return total, h_worker.hexdigest()

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: _consume_transfer_thread(), range(3)))
        for byte_count, digest in results:
            assert byte_count == expected_size
            assert digest == expected_digest
        assert mgr.get_active_reader_count(op_id) == 0

        # 6. Early close releases pending deletion immediately
        early_reader = mgr.open_leased_reader(op_id, payload_file)
        assert early_reader.read(64) == chunk_1m[:64]
        assert mgr.cleanup_operation(op_id, force=True) is False
        assert mgr.is_pending_deletion(op_id) is True
        early_reader.close()
        assert mgr.get_active_reader_count(op_id) == 0
        assert not op_dir.exists()

    def test_atomic_global_space_reservation_prevents_toctou_race(self, tmp_path: Path) -> None:
        """Verify two concurrent sessions cannot exceed max_global_workspace_bytes via TOCTOU."""
        shared_base = tmp_path / "shared_global_quota"
        mgr_a = TempFileManager(base_dir=shared_base)
        mgr_b = TempFileManager(base_dir=shared_base)

        # Pre-seed 700 bytes on disk against a 1000-byte global quota (300 bytes free)
        existing_op, existing_dir = mgr_a.create_operation_dir()
        (existing_dir / "existing.bin").write_bytes(b"E" * 700)
        assert mgr_a.get_committed_workspace_bytes() == 700

        op_a = str(uuid.uuid4())
        op_b = str(uuid.uuid4())
        barrier = threading.Barrier(2, timeout=5.0)
        outcomes: list[str] = []
        outcome_lock = threading.Lock()

        def _try_reserve(manager: TempFileManager, op_id: str) -> None:
            barrier.wait()
            try:
                manager.reserve_space(op_id, 250, max_global_bytes=1000)
                with outcome_lock:
                    outcomes.append(f"ok:{op_id}")
            except ResourceLimitExceededError:
                with outcome_lock:
                    outcomes.append(f"rejected:{op_id}")

        t1 = threading.Thread(target=_try_reserve, args=(mgr_a, op_a))
        t2 = threading.Thread(target=_try_reserve, args=(mgr_b, op_b))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        ok_count = sum(1 for item in outcomes if item.startswith("ok:"))
        rej_count = sum(1 for item in outcomes if item.startswith("rejected:"))
        assert ok_count == 1
        assert rej_count == 1
        assert mgr_a.get_committed_workspace_bytes() == 950

        # Releasing the winner's reservation immediately frees logical quota again
        mgr_a.release_reservation(op_a)
        mgr_b.release_reservation(op_b)
        assert mgr_a.get_committed_workspace_bytes() == 700

        # Non-UUID subdir, loose file, and negative requested_bytes clamping
        non_uuid_dir = shared_base / "misc_folder"
        non_uuid_dir.mkdir(exist_ok=True)
        (non_uuid_dir / "extra.bin").write_bytes(b"X" * 50)
        (shared_base / "loose.bin").write_bytes(b"Y" * 25)
        assert mgr_a.get_committed_workspace_bytes() == 775
        assert mgr_a.reserve_space(existing_op, -10, max_global_bytes=1000) == 775
        mgr_a.release_reservation(existing_op)

        assert mgr_a.cleanup_operation(existing_op, force=True) is True
