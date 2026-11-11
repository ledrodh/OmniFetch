"""Exhaustive unit tests for `utils/validators.py` (Anti-SSRF, DNS guard, TimeRange, concurrency)."""

from __future__ import annotations

import ipaddress
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.models import ErrorCategory, OperationalLimits
from utils.error_handler import InvalidURLError, ResourceLimitExceededError
from utils.validators import (
    ConcurrencyGuard,
    SafeYoutubeDL,
    _assert_public_ip,
    extract_embedded_ipv4,
    guarded_dns_resolution,
    operation_slot,
    parse_timestamp,
    run_in_guarded_context,
    validate_media_limits,
    validate_redirect_hop,
    validate_time_range,
    validate_url,
)


class TestIPv4MappedAndCompatibility:
    """Valida compatibilidade Python 3.10-3.13+ para IPv4 embutido em IPv6 e redes especiais."""

    @pytest.mark.parametrize(
        ("ipv6_str", "expected_v4"),
        [
            ("::ffff:127.0.0.1", "127.0.0.1"),
            ("::ffff:169.254.169.254", "169.254.169.254"),
            ("64:ff9b::10.0.0.1", "10.0.0.1"),  # NAT64 RFC 6052
            ("2002:7f00:0001::1", "127.0.0.1"),  # 6to4 RFC 3056 (127.0.0.1)
            ("2001:db8::1", None),  # IPv6 sem IPv4 embutido
        ],
    )
    def test_extracts_embedded_ipv4_across_mechanisms(
        self, ipv6_str: str, expected_v4: str | None
    ) -> None:
        v6 = ipaddress.IPv6Address(ipv6_str)
        extracted = extract_embedded_ipv4(v6)
        if expected_v4 is None:
            assert extracted is None
        else:
            assert extracted == ipaddress.IPv4Address(expected_v4)

    def test_extract_embedded_ipv4_with_ipv4_input_and_binary_fallbacks(self) -> None:
        v4 = ipaddress.IPv4Address("93.184.216.34")
        assert extract_embedded_ipv4(v4) == v4

        v6_mapped = ipaddress.IPv6Address("::ffff:192.168.1.50")
        v6_6to4 = ipaddress.IPv6Address("2002:7f00:0001::1")
        with (
            patch.object(ipaddress.IPv6Address, "ipv4_mapped", None),
            patch.object(ipaddress.IPv6Address, "sixtofour", None),
        ):
            assert extract_embedded_ipv4(v6_mapped) == ipaddress.IPv4Address("192.168.1.50")
            assert extract_embedded_ipv4(v6_6to4) == ipaddress.IPv4Address("127.0.0.1")

    def test_assert_public_ip_rejects_invalid_ip_string(self) -> None:
        with pytest.raises(InvalidURLError):
            _assert_public_ip("not_an_ip_address")


class TestURLValidationAndAntiSSRF:
    """Valida protecao Anti-SSRF, sintaxe, multi-A/AAAA DNS e DNS rebinding."""

    @pytest.mark.parametrize(
        "bad_url",
        [
            "",
            "   ",
            "https://example.com/" + ("a" * 2100),
            "ftp://example.com/video.mp4",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ytsearch:funny cats",
            "http://user:password@example.com/video",
            "http://:pass@example.com/video",
            "http://example.com:99999/video",
            "http:///path-without-host",
            "http://localhost:8000/video",
            "https://sub.localhost/test",
            "http://127.0.0.1/admin",
            "http://127.0.0.53:8080/secret",
            "http://[::1]/loopback",
            "http://[::ffff:127.0.0.1]/mapped",
            "http://[64:ff9b::127.0.0.1]/nat64-loopback",
            "http://[64:ff9b:1::1]/nat64-local",
            "http://[2002:7f00:1::]/6to4-loopback",
            "http://[2001:db8::1]/doc-ipv6",
            "http://[fc00::1]/ula",
            "http://[fe80::1]/link-local",
            "http://10.0.0.1/internal",
            "http://172.16.5.4/router",
            "http://192.168.1.1/setup",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/cgnat",
            "http://192.0.0.1/ietf",
            "http://198.18.0.1/benchmark",
            "http://0.0.0.0/zero",
            "http://printer.local/stream",
            "http://service.internal/api",
            "http://intranet/video",
            "https://example.com/video\r\nX-Injected: true",
        ],
    )
    def test_blocks_malicious_or_private_urls(self, bad_url: str) -> None:
        with pytest.raises(InvalidURLError) as exc_info:
            validate_url(bad_url, resolve_dns=False)
        structured = exc_info.value.to_structured()
        assert structured.category == ErrorCategory.INVALID_URL

    def test_rejects_non_string_url(self) -> None:
        with pytest.raises(InvalidURLError):
            validate_url(None, resolve_dns=False)  # type: ignore[arg-type]

    def test_blocks_multi_record_dns_when_any_ip_is_private(self) -> None:
        """Se o DNS retornar um IP publico e um IP privado (mixed record), deve bloquear."""
        mixed_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.42", 443)),
        ]
        with patch("socket.getaddrinfo", return_value=mixed_addrinfo):
            with pytest.raises(InvalidURLError):
                validate_url("https://mixed-dns.example.com/watch", resolve_dns=True)

    def test_blocks_dns_resolution_failure_or_empty(self) -> None:
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("NXDOMAIN")):
            with pytest.raises(InvalidURLError):
                validate_url("https://nonexistent.example.com/v", resolve_dns=True)

        with patch("socket.getaddrinfo", return_value=[]):
            with pytest.raises(InvalidURLError):
                validate_url("https://empty-dns.example.com/v", resolve_dns=True)

    def test_allows_valid_public_url_and_public_ip(self) -> None:
        fake_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0),
            ),
        ]
        with patch("socket.getaddrinfo", return_value=fake_addrinfo):
            cleaned = validate_url("  https://www.example.com/watch?v=abc  ", resolve_dns=True)
        assert cleaned == "https://www.example.com/watch?v=abc"
        assert validate_url("https://93.184.216.34/video.mp4", resolve_dns=True) == (
            "https://93.184.216.34/video.mp4"
        )


class TestGuardedDNSConcurrencyAndSafeYoutubeDL:
    """
    Valida os 5 cenarios exigidos na auditoria para o Guardiao DNS concorrente
    e a interceptacao de transporte em SafeYoutubeDL.
    """

    def test_urlopen_blocks_ssrf_before_network_call(self) -> None:
        with SafeYoutubeDL({"quiet": True}, auto_init=False, resolve_dns=False) as ydl:
            with pytest.raises(InvalidURLError):
                ydl.urlopen("http://169.254.169.254/latest/meta-data/")

    def test_urlopen_blocks_ssrf_on_http_redirect_response(self) -> None:
        fake_response = MagicMock()
        fake_response.url = "http://127.0.0.1:8080/internal-admin"

        with (
            patch("yt_dlp.YoutubeDL.YoutubeDL.urlopen", return_value=fake_response),
            SafeYoutubeDL({"quiet": True}, auto_init=False, resolve_dns=False) as ydl,
        ):
            with pytest.raises(InvalidURLError):
                ydl.urlopen("https://93.184.216.34/public-shortlink")

    def test_urlopen_allows_clean_redirect_with_geturl(self) -> None:
        fake_response = MagicMock(spec=["geturl"])
        fake_response.geturl.return_value = "https://93.184.216.34/final-stream.mp4"
        req_obj = MagicMock()
        req_obj.url = "https://93.184.216.34/initial"

        with (
            patch("yt_dlp.YoutubeDL.YoutubeDL.urlopen", return_value=fake_response),
            SafeYoutubeDL({"quiet": True}, auto_init=False, resolve_dns=False) as ydl,
        ):
            resp = ydl.urlopen(req_obj)
            assert resp is fake_response

    def test_guarded_dns_blocks_dns_rebinding_on_second_resolution(self) -> None:
        """Simula DNS rebinding: 1a chamada retorna IP publico, 2a chamada (na conexao) retorna 127.0.0.1."""
        call_count = 0

        def _rebinding_addrinfo(
            host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
        ) -> list[Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

        def _fake_super_urlopen(req: Any) -> Any:
            socket.getaddrinfo("rebind.attacker.example.com", 443)
            return MagicMock(url="https://rebind.attacker.example.com/stream.mp4")

        with (
            patch("socket.getaddrinfo", side_effect=_rebinding_addrinfo),
            patch("yt_dlp.YoutubeDL.YoutubeDL.urlopen", side_effect=_fake_super_urlopen),
            SafeYoutubeDL({"quiet": True}, auto_init=False, resolve_dns=True) as ydl,
        ):
            with pytest.raises(InvalidURLError):
                ydl.urlopen("https://rebind.attacker.example.com/stream.mp4")
        assert call_count == 2

    def test_overlapping_context_managers_exiting_out_of_order_restore_original_getaddrinfo(
        self,
    ) -> None:
        """
        Cenario Critico da Auditoria (Tabela Sessao A vs Sessao B):
        1. Sessao A entra no guarded_dns_resolution;
        2. Sessao B entra no guarded_dns_resolution;
        3. Sessao A encerra PRIMEIRO;
        4. Sessao B continua protegida e depois encerra;
        5. Ao final, socket.getaddrinfo DEVE ser exatamente a funcao original (sem wrapper residual).
        """

        def _base_resolver(
            host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
        ) -> list[Any]:
            h = host.decode() if isinstance(host, bytes) else str(host)
            if "evil" in h:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        with patch("socket.getaddrinfo", _base_resolver):
            assert socket.getaddrinfo is _base_resolver

            cm_a = guarded_dns_resolution(enabled=True)
            cm_b = guarded_dns_resolution(enabled=True)

            # Momento 1: Sessao A entra
            cm_a.__enter__()
            # Momento 2: Sessao B entra
            cm_b.__enter__()

            # Momento 3: Sessao A sai ANTES da Sessao B
            cm_a.__exit__(None, None, None)

            # Sessao B ainda esta ativa: resolucao maliciosa ainda deve ser bloqueada!
            with pytest.raises(InvalidURLError):
                socket.getaddrinfo(b"evil.example.com", 443)

            # Momento 4: Sessao B sai
            cm_b.__exit__(None, None, None)

            # A funcao original DEVE ter sido restaurada sem efeito residual!
            assert socket.getaddrinfo is _base_resolver
            # E agora que ambas sairam, _base_resolver nao tem mais o wrapper interceptador
            res = socket.getaddrinfo("evil.example.com", 443)
            assert res[0][4][0] == "10.0.0.1"

    def test_two_simultaneous_threads_with_independent_dns_resolutions_and_unguarded_third_thread(
        self,
    ) -> None:
        """
        Testa simultaneamente em threads reais:
        - Thread A (protegida): resolve dominio publico (sucesso) e dominio privado (bloqueado);
        - Thread B (protegida, saindo fora de ordem em relacao a A): resolve dominio publico (sucesso);
        - Thread C (NAO protegida, ex: loop interno do Streamlit resolvendo localhost): executa DURANTE
          a interceptacao de A e B e NAO sofre bloqueio indevido!
        """

        def _mock_resolver(
            host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
        ) -> list[Any]:
            h = str(host)
            if h in ("localhost", "internal.corp"):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8501))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        barrier_inside = threading.Barrier(3, timeout=5.0)
        event_a_exited = threading.Event()
        errors: list[Exception] = []
        results: dict[str, Any] = {}

        def _worker_a() -> None:
            try:
                with guarded_dns_resolution(enabled=True):
                    barrier_inside.wait()
                    res_pub = socket.getaddrinfo("video-a.example.com", 443)
                    results["a_pub"] = res_pub[0][4][0]
                    with pytest.raises(InvalidURLError):
                        socket.getaddrinfo("internal.corp", 443)
                    results["a_blocked_private"] = True
                # Thread A sai do context manager primeiro
                event_a_exited.set()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
                event_a_exited.set()

        def _worker_b() -> None:
            try:
                with guarded_dns_resolution(enabled=True):
                    barrier_inside.wait()
                    # Aguarda Thread A sair primeiro (encerramento cruzado entre threads)
                    event_a_exited.wait(timeout=5.0)
                    res_pub = socket.getaddrinfo("video-b.example.com", 443)
                    results["b_pub"] = res_pub[0][4][0]
                    with pytest.raises(InvalidURLError):
                        socket.getaddrinfo("localhost", 8501)
                    results["b_blocked_localhost"] = True
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def _worker_c_unguarded() -> None:
            try:
                # Thread C NAO usa guarded_dns_resolution (representa outra biblioteca/servidor no processo)
                barrier_inside.wait()
                local_res = socket.getaddrinfo("localhost", 8501)
                results["c_unguarded_localhost"] = local_res[0][4][0]
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        with patch("socket.getaddrinfo", _mock_resolver):
            t_a = threading.Thread(target=_worker_a)
            t_b = threading.Thread(target=_worker_b)
            t_c = threading.Thread(target=_worker_c_unguarded)
            t_a.start()
            t_b.start()
            t_c.start()
            t_a.join(timeout=5.0)
            t_b.join(timeout=5.0)
            t_c.join(timeout=5.0)

            # Verifica ausencia de erros e restauracao exata da funcao original
            assert errors == []
            assert socket.getaddrinfo is _mock_resolver

        assert results["a_pub"] == "93.184.216.34"
        assert results["a_blocked_private"] is True
        assert results["b_pub"] == "93.184.216.34"
        assert results["b_blocked_localhost"] is True
        assert results["c_unguarded_localhost"] == "127.0.0.1"

    def test_original_getaddrinfo_restored_after_exception_and_no_residual_effects(self) -> None:
        def _empty_resolver(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        with patch("socket.getaddrinfo", _empty_resolver):
            with pytest.raises(InvalidURLError):
                with guarded_dns_resolution(enabled=True):
                    socket.getaddrinfo("public.example.com", 443)
            # Even after an exception inside the guarded block, original resolver is restored
            assert socket.getaddrinfo is _empty_resolver
            assert socket.getaddrinfo("localhost", 80) == []

    def test_concurrent_fragment_worker_threads_enforce_dns_guard_and_context_propagation(
        self,
    ) -> None:
        """Verify worker threads (ThreadPoolExecutor) retain SSRF protection via urlopen and copy_context."""

        def _fragment_resolver(
            host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
        ) -> list[Any]:
            h = host.decode() if isinstance(host, bytes) else str(host)
            if "rebind" in h:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        def _fake_super_urlopen(req: Any) -> Any:
            target = req if isinstance(req, str) else getattr(req, "url", "")
            host = "rebind.attacker.example.com" if "frag-evil" in target else "cdn.example.com"
            resolved = socket.getaddrinfo(host, 443)
            return MagicMock(url=target, resolved_ip=resolved[0][4][0])

        with (
            patch("socket.getaddrinfo", _fragment_resolver),
            patch("yt_dlp.YoutubeDL.YoutubeDL.urlopen", side_effect=_fake_super_urlopen),
            SafeYoutubeDL({"quiet": True}, auto_init=False, resolve_dns=True) as ydl,
        ):
            assert ydl.params.get("concurrent_fragment_downloads") == 1

            # 1. Simulate multi-threaded fragment pool calling SafeYoutubeDL.urlopen directly
            urls = [
                "https://93.184.216.34/frag-1.m4s",
                "https://93.184.216.34/frag-2.m4s",
                "https://93.184.216.34/frag-evil.m4s",
                "https://93.184.216.34/frag-4.m4s",
            ]
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [pool.submit(ydl.urlopen, u) for u in urls]
                outcomes: list[str] = []
                for idx, fut in enumerate(futs):
                    if idx == 2:
                        with pytest.raises(InvalidURLError):
                            fut.result()
                        outcomes.append("blocked")
                    else:
                        outcomes.append(fut.result().resolved_ip)

            assert outcomes == ["93.184.216.34", "93.184.216.34", "blocked", "93.184.216.34"]

            # 2. Verify explicit parent ContextVar propagation across child threads via run_in_guarded_context
            with guarded_dns_resolution(enabled=True):
                captured_ctx = copy_context()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    blocked_fut = pool.submit(
                        run_in_guarded_context,
                        socket.getaddrinfo,
                        "rebind.example.com",
                        443,
                        parent_context=captured_ctx,
                    )
                    allowed_fut = pool.submit(
                        run_in_guarded_context,
                        socket.getaddrinfo,
                        "cdn.example.com",
                        443,
                    )
                    with pytest.raises(InvalidURLError):
                        blocked_fut.result()
                    assert allowed_fut.result()[0][4][0] == "93.184.216.34"

            assert socket.getaddrinfo is _fragment_resolver

    def test_validate_redirect_hop_blocks_private_location_before_connect(self) -> None:
        with pytest.raises(InvalidURLError):
            validate_redirect_hop("https://93.184.216.34/start", "", resolve_dns=False)
        with pytest.raises(InvalidURLError):
            validate_redirect_hop(
                "https://93.184.216.34/start",
                "http://169.254.169.254/latest/meta-data/",
                resolve_dns=False,
            )
        assert (
            validate_redirect_hop(
                "https://93.184.216.34/dir/page",
                "/new-public-path.mp4",
                resolve_dns=False,
            )
            == "https://93.184.216.34/new-public-path.mp4"
        )


class TestTimeRangeAndMediaLimitsAndConcurrency:
    """Valida parser/validador de recorte temporal (Fase 4), limites globais e semaforo."""

    def test_parse_timestamp_and_validate_time_range(self) -> None:
        assert parse_timestamp("45") == 45.0
        assert parse_timestamp("01:30") == 90.0
        assert parse_timestamp("01:02:03.5") == 3723.5

        for bad_ts in ("", "abc", "01:60", "01:60:00", "01:00:65", "-5"):
            with pytest.raises(InvalidURLError):
                parse_timestamp(bad_ts)

        with pytest.raises(InvalidURLError):
            parse_timestamp(None)  # type: ignore[arg-type]

        assert validate_time_range("", "") is None
        tr = validate_time_range("00:10", "01:15", media_duration=120.0, is_live=False)
        assert tr is not None
        assert tr.start_seconds == 10.0
        assert tr.end_seconds == 75.0
        assert tr.duration_seconds == 65.0
        assert tr.label == "00:10 - 01:15"

        tr_hours = validate_time_range("00:00:10", "01:00:20")
        assert tr_hours is not None
        assert tr_hours.label == "00:10 - 01:00:20"

        # Live stream bloqueado
        with pytest.raises(ResourceLimitExceededError):
            validate_time_range("00:00", "00:10", is_live=True)

        # Incompleto (apenas inicio ou apenas fim)
        with pytest.raises(InvalidURLError):
            validate_time_range("00:10", "")

        # Fim <= Inicio
        with pytest.raises(InvalidURLError):
            validate_time_range("00:30", "00:20")

        # Trecho menor que 1 segundo
        with pytest.raises(InvalidURLError):
            validate_time_range("00:10.0", "00:10.5")

        # Ultrapassa duracao da midia
        with pytest.raises(ResourceLimitExceededError):
            validate_time_range("00:10", "02:00", media_duration=60.0)

    def test_validate_media_limits_including_global_workspace_quota(self) -> None:
        limits = OperationalLimits(
            max_duration_seconds=600,
            max_filesize_bytes=1024,
            max_workspace_bytes=2048,
            max_global_workspace_bytes=4096,
        )
        validate_media_limits(
            duration_seconds=300,
            filesize_bytes=512,
            workspace_bytes=1500,
            global_workspace_bytes=3000,
            limits=limits,
        )

        with pytest.raises(ResourceLimitExceededError):
            validate_media_limits(duration_seconds=601, limits=limits)

        with pytest.raises(ResourceLimitExceededError):
            validate_media_limits(filesize_bytes=2048, limits=limits)

        with pytest.raises(ResourceLimitExceededError):
            validate_media_limits(workspace_bytes=2049, limits=limits)

        with pytest.raises(ResourceLimitExceededError):
            validate_media_limits(global_workspace_bytes=4097, limits=limits)

    def test_concurrency_guard_rejects_invalid_init_and_recovers_after_exception(self) -> None:
        with pytest.raises(ValueError):
            ConcurrencyGuard(max_concurrent=0)

        guard = ConcurrencyGuard(max_concurrent=1)
        with pytest.raises(RuntimeError):
            with operation_slot(guard=guard, timeout=0.01):
                raise RuntimeError("Simulated crash during download")

        with operation_slot(guard=guard, timeout=0.01):
            assert guard.max_concurrent == 1

        with operation_slot(limits=OperationalLimits(max_concurrent_operations=2), timeout=0.01):
            pass
