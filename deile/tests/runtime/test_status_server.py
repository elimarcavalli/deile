"""Testes do :mod:`deile.runtime.status_server` (issue #303 — Fase 2).

Cobre o ciclo de vida do servidor (start/stop idempotente, permissão do socket,
serve_forever, cancelamento limpo), as três respostas válidas (STATUS, METRICS,
FLUSH), o tratamento defensivo (comando desconhecido, linha gigante, NUL byte,
conexão sem ``\\n``) e o cliente síncrono (timeout, socket ausente, payloads
inválidos).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket as _socket
import stat
from pathlib import Path

import pytest

from deile.runtime.instance_state import InstanceState
from deile.runtime.status_server import MAX_LINE_BYTES, StatusClient, format_metrics

# ── helpers ──────────────────────────────────────────────────────────────


async def _make_running_server(role: str = "cli") -> tuple[InstanceState, asyncio.Task]:
    """Cria InstanceState + StatusServer + serve_forever() task. Caller fecha."""
    state = InstanceState(
        role=role,
        enable_registry=False,
        enable_status_server=True,
    )
    assert state.status_server is not None
    await state.status_server.start()
    task = asyncio.create_task(state.status_server.serve_forever())
    # Yield ao loop para a aceitação ficar pronta antes do primeiro request.
    await asyncio.sleep(0)
    return state, task


async def _stop_server(state: InstanceState, task: asyncio.Task) -> None:
    """Tear-down simétrico de :func:`_make_running_server`."""
    try:
        if state.status_server is not None:
            await state.status_server.stop()
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        state.close()


async def _client_send(
    socket_path: Path, command: bytes, timeout_s: float = 1.0
) -> bytes:
    """Cliente cru em executor — evita bloquear o loop async com socket sync."""
    loop = asyncio.get_event_loop()

    def _send() -> bytes:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        try:
            sock.connect(str(socket_path))
            sock.sendall(command)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            sock.close()

    return await loop.run_in_executor(None, _send)


# ── lifecycle ────────────────────────────────────────────────────────────


async def test_status_server_start_creates_socket_with_0600_perm():
    state, task = await _make_running_server()
    try:
        sp = state.status_server.socket_path
        assert sp.exists()
        # Permissão deve ser exatamente 0o600 (só dono lê/escreve).
        mode = stat.S_IMODE(os.stat(sp).st_mode)
        assert mode == 0o600
    finally:
        await _stop_server(state, task)


async def test_status_server_start_is_idempotent():
    state = InstanceState(
        role="cli",
        enable_registry=False,
        enable_status_server=True,
    )
    try:
        server = state.status_server
        await server.start()
        await server.start()  # segunda chamada não deve levantar nem rebindar
        assert server.is_serving
        assert server.socket_path.exists()
    finally:
        await state.status_server.stop()
        state.close()


async def test_status_server_stop_is_idempotent():
    state, task = await _make_running_server()
    try:
        sp = state.status_server.socket_path
        await state.status_server.stop()
        await state.status_server.stop()  # second stop no-op
        assert not sp.exists()
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        state.close()


async def test_serve_forever_reraises_cancelled_error():
    """Princípio 6 — ``CancelledError`` não pode ser silenciado."""
    state, task = await _make_running_server()
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await _stop_server(state, task)


async def test_serve_forever_returns_when_not_started():
    """Sem ``start()``, ``serve_forever`` deve sair limpo (no-op)."""
    state = InstanceState(
        role="cli",
        enable_registry=False,
        enable_status_server=True,
    )
    try:
        # Não chamamos start — apenas serve_forever.
        await asyncio.wait_for(state.status_server.serve_forever(), timeout=1.0)
    finally:
        state.close()


async def test_endpoint_string_uses_unix_scheme():
    state, task = await _make_running_server()
    try:
        endpoint = state.status_server.endpoint
        assert endpoint.startswith("unix://")
        assert endpoint.endswith(".sock")
        # O caminho deve ser absoluto.
        assert "/" in endpoint.removeprefix("unix://")
    finally:
        await _stop_server(state, task)


# ── STATUS ───────────────────────────────────────────────────────────────


async def test_status_returns_json_snapshot():
    state, task = await _make_running_server()
    try:
        state.update_stats(tokens_in=10, tokens_out=5, cost_usd=0.001)
        state.update_action("tool_execution", detail="execute_bash")
        raw = await _client_send(state.status_server.socket_path, b"STATUS\n")
        # Termina com \n + payload é JSON válido em uma linha.
        assert raw.endswith(b"\n")
        payload = json.loads(raw.decode("utf-8").strip())
        assert payload["instance_id"] == state.instance_id
        assert payload["pid"] == os.getpid()
        assert payload["stats"]["tokens_in"] == 10
        assert payload["current_action"]["kind"] == "tool_execution"
        assert payload["current_action"]["detail"] == "execute_bash"
    finally:
        await _stop_server(state, task)


async def test_status_reflects_live_state_changes():
    """Snapshot tem que vir do dict em memória, não de leitura do file."""
    state, task = await _make_running_server()
    try:
        # Apaga o state file deliberadamente — server lê do dict, não do FS.
        state.path.unlink()
        state.update_stats(tool_calls=42)
        raw = await _client_send(state.status_server.socket_path, b"STATUS\n")
        payload = json.loads(raw.decode("utf-8").strip())
        assert payload["stats"]["tool_calls"] == 42
    finally:
        await _stop_server(state, task)


# ── METRICS ──────────────────────────────────────────────────────────────


async def test_metrics_emits_prometheus_text_format():
    state, task = await _make_running_server()
    try:
        state.update_stats(tokens_in=100, tokens_out=20, cost_usd=0.05)
        raw = await _client_send(state.status_server.socket_path, b"METRICS\n")
        text = raw.decode("utf-8")
        # Cada metric tem HELP + TYPE + linha de dado.
        for metric in (
            "deile_tokens_total",
            "deile_cost_usd_total",
            "deile_turns_total",
            "deile_tool_calls_total",
            "deile_errors_total",
            "deile_uptime_seconds",
            "deile_busy",
        ):
            assert f"# HELP {metric} " in text
            assert f"# TYPE {metric} " in text
        assert f'instance="{state.instance_id}"' in text
        assert 'direction="in"' in text
        assert 'direction="out"' in text
        # tokens_in deve aparecer com valor 100.
        assert "100" in text
    finally:
        await _stop_server(state, task)


async def test_metrics_marks_busy_when_action_active():
    state, task = await _make_running_server()
    try:
        state.update_action(
            "llm_call", detail="generate_stream", model="deepseek:v4-pro"
        )
        raw = await _client_send(state.status_server.socket_path, b"METRICS\n")
        text = raw.decode("utf-8")
        # ``deile_busy{...kind="llm_call"} 1`` deve aparecer.
        assert "deile_busy" in text
        assert 'kind="llm_call"' in text
        # Última coluna do gauge é o valor 1.
        assert " 1\n" in text or text.rstrip().endswith(" 1")
    finally:
        await _stop_server(state, task)


def test_format_metrics_handles_missing_stats():
    """``format_metrics`` é tolerante a snapshot incompleto (forward-compat)."""
    snap = {"instance_id": "cli-abc", "role": "cli"}
    text = format_metrics(snap, uptime_s=12.5, busy_kind=None)
    assert "deile_tokens_total" in text
    assert 'instance="cli-abc"' in text
    assert "deile_busy" in text


def test_format_metrics_escapes_label_special_chars():
    """Labels com aspas/backslash/newline são escapadas — spec Prometheus."""
    snap = {"instance_id": 'weird"id\\here', "role": "cli\nrole"}
    text = format_metrics(snap, uptime_s=1.0, busy_kind=None)
    # Aspas viram \" e backslash vira \\ no output.
    assert r'instance="weird\"id\\here"' in text
    assert r'role="cli\nrole"' in text


# ── FLUSH ────────────────────────────────────────────────────────────────


async def test_flush_returns_ok_and_rewrites_state_file():
    state, task = await _make_running_server()
    try:
        state.path.unlink()  # apaga para confirmar reescrita
        raw = await _client_send(state.status_server.socket_path, b"FLUSH\n")
        assert raw.strip() == b"OK"
        assert state.path.exists()
    finally:
        await _stop_server(state, task)


# ── error handling ──────────────────────────────────────────────────────


async def test_unknown_command_returns_err():
    state, task = await _make_running_server()
    try:
        raw = await _client_send(state.status_server.socket_path, b"FROBNICATE\n")
        assert raw.startswith(b"ERR")
        assert b"unknown command" in raw
    finally:
        await _stop_server(state, task)


async def test_oversize_line_rejected():
    """Linha > 1KB é cortada com ``ERR line too long``."""
    state, task = await _make_running_server()
    try:
        # Excede MAX_LINE_BYTES pra forçar o limite.
        huge = b"X" * (MAX_LINE_BYTES * 2) + b"\n"
        raw = await _client_send(state.status_server.socket_path, huge)
        assert raw.startswith(b"ERR")
        assert b"line too long" in raw
    finally:
        await _stop_server(state, task)


async def test_nul_byte_rejected():
    """Linha contendo NUL byte vira ``ERR invalid char``."""
    state, task = await _make_running_server()
    try:
        raw = await _client_send(state.status_server.socket_path, b"STA\x00TUS\n")
        assert raw.startswith(b"ERR")
        assert b"invalid char" in raw
    finally:
        await _stop_server(state, task)


async def test_empty_request_when_client_closes_without_newline():
    """Cliente que fecha sem ``\\n`` recebe ``ERR empty request``."""
    state, task = await _make_running_server()
    try:
        # Manda byte solto sem newline e fecha.
        loop = asyncio.get_event_loop()

        def _send_then_close() -> bytes:
            sp = state.status_server.socket_path
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.connect(str(sp))
                sock.sendall(b"hi")
                sock.shutdown(_socket.SHUT_WR)
                chunks: list[bytes] = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                sock.close()

        raw = await loop.run_in_executor(None, _send_then_close)
        assert raw.startswith(b"ERR")
    finally:
        await _stop_server(state, task)


# ── StatusClient ────────────────────────────────────────────────────────


async def test_client_status_returns_dict():
    state, task = await _make_running_server()
    try:
        loop = asyncio.get_event_loop()
        client = StatusClient(state.status_server.socket_path, timeout_s=1.0)
        result = await loop.run_in_executor(None, client.status)
        assert isinstance(result, dict)
        assert result["instance_id"] == state.instance_id
    finally:
        await _stop_server(state, task)


async def test_client_metrics_returns_string():
    state, task = await _make_running_server()
    try:
        loop = asyncio.get_event_loop()
        client = StatusClient(state.status_server.socket_path, timeout_s=1.0)
        text = await loop.run_in_executor(None, client.metrics)
        assert isinstance(text, str)
        assert "deile_tokens_total" in text
    finally:
        await _stop_server(state, task)


async def test_client_flush_returns_true_on_ok():
    state, task = await _make_running_server()
    try:
        loop = asyncio.get_event_loop()
        client = StatusClient(state.status_server.socket_path, timeout_s=1.0)
        ok = await loop.run_in_executor(None, client.flush)
        assert ok is True
    finally:
        await _stop_server(state, task)


def test_client_returns_none_when_socket_missing(short_runtime_dir):
    """Sem socket no path → :meth:`status` retorna None (não levanta)."""
    fake = short_runtime_dir / "nonexistent.sock"
    client = StatusClient(fake, timeout_s=0.1)
    assert client.status() is None
    assert client.metrics() is None
    assert client.flush() is False


async def test_client_handles_partial_json_gracefully(short_runtime_dir):
    """Servidor retorna lixo → ``status()`` devolve None sem levantar."""
    # Sobe um servidor Unix mock que devolve "not json\n".
    sp = short_runtime_dir / "fake.sock"

    async def _handle(reader, writer):
        await reader.readuntil(b"\n")
        writer.write(b"not a json\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(_handle, path=str(sp))
    try:
        loop = asyncio.get_event_loop()
        client = StatusClient(sp, timeout_s=1.0)
        result = await loop.run_in_executor(None, client.status)
        assert result is None
    finally:
        server.close()
        await server.wait_closed()


async def test_client_timeout_returns_none(short_runtime_dir):
    """Servidor que aceita mas nunca responde → cliente retorna None."""
    sp = short_runtime_dir / "slow.sock"
    # Evento usado pra liberar o handler quando o teste terminar — assim
    # ``server.wait_closed()`` não bloqueia esperando o sleep do mock.
    release = asyncio.Event()

    async def _handle(reader, writer):
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise
        finally:
            writer.close()

    server = await asyncio.start_unix_server(_handle, path=str(sp))
    try:
        loop = asyncio.get_event_loop()
        client = StatusClient(sp, timeout_s=0.1)
        result = await loop.run_in_executor(None, client.status)
        assert result is None
    finally:
        release.set()
        server.close()
        await server.wait_closed()
