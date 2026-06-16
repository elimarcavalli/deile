"""StatusServer + StatusClient — Unix socket ``/status`` para introspecção viva.

Fase 2 da issue #303. Cada processo DEILE expõe um socket Unix em
``<runtime_dir>/<instance_id>.sock`` com protocolo line-based simples:

  - ``STATUS\\n``  → JSON snapshot do :class:`InstanceState` (mesmo schema do
    state file) em uma linha + ``\\n``.
  - ``METRICS\\n`` → exposição Prometheus textual + ``\\n``.
  - ``FLUSH\\n``   → ``OK\\n`` (força reescrita atômica do state file; usado
    em diagnóstico).
  - qualquer outra coisa → ``ERR unknown command\\n`` e fecha.

O servidor é asyncio-native (``asyncio.start_unix_server``); o cliente é
síncrono por desenho — o painel TUI (``infra/k8s/_panel*``) consome
sincronamente a partir de threads do ``BackgroundRefresher``, e mudá-lo
para async empurraria um event loop dentro de cada worker thread.

Garantias:
  - **Cross-platform:** Unix socket é POSIX-only. Em Windows, :meth:`StatusServer.start`
    loga um warning e vira no-op silencioso (``socket_path`` continua None).
  - **Permissão:** ``chmod 0o600`` após criar o socket — só o dono lê/escreve.
  - **Limites defensivos:** linha de input máx. 1KB; conexão é fechada após
    1 request (não keep-alive). Linhas com NUL byte ou sem ``\\n`` antes do
    limite são rejeitadas.
  - **Sem segredos:** o payload do socket é estritamente o ``snapshot()`` do
    :class:`InstanceState` — mesma regra do pilar 08 que cobre o state file.

Ver decisão #36 (status server + registry) em ``docs/system_design/DECISOES.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket as _socket
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from deile.runtime.instance_state import InstanceState

__all__ = [
    "StatusServer",
    "StatusClient",
    "format_metrics",
    "MAX_LINE_BYTES",
]

logger = logging.getLogger(__name__)

# 1KB é folgado para os comandos atuais (``STATUS``/``METRICS``/``FLUSH``);
# bloqueia injection por linha gigante sem custo perceptível.
MAX_LINE_BYTES = 1024

# Permissão do socket file: só o dono lê/escreve. Mesma proteção que o
# state file ``.json`` herda do umask padrão; aplicada explicitamente aqui
# porque ``bind()`` cria com mode da umask do processo (pode ser 0o022 →
# 0o755 efetivo, leitura global). Pilar 08 — segurança por padrão.
SOCKET_FILE_MODE = 0o600

_VALID_COMMANDS = frozenset({"STATUS", "METRICS", "FLUSH"})


def _is_posix() -> bool:
    """True em macOS/Linux; False em Windows."""
    return os.name == "posix" and sys.platform != "win32"


# ── Prometheus exposition ────────────────────────────────────────────────


def format_metrics(
    snapshot: Dict[str, Any], *, uptime_s: float, busy_kind: Optional[str]
) -> str:
    """Serializa um snapshot do :class:`InstanceState` em formato Prometheus text.

    Formato segue https://prometheus.io/docs/instrumenting/exposition_formats/
    (cada métrica precede de ``# HELP`` e ``# TYPE``). Labels: ``instance``
    (instance_id) e ``role``; ``direction`` para o counter de tokens;
    ``kind`` para o gauge ``deile_busy``.

    Valores numéricos são serializados com :func:`repr` para float (preserva
    precisão) e :class:`int` direto. Strings em label são escaped por
    :func:`_escape_label` (regra Prometheus: ``\\``, ``"``, ``\\n``).
    """
    instance_id = str(snapshot.get("instance_id", ""))
    role = str(snapshot.get("role", ""))
    stats = snapshot.get("stats") or {}
    if not isinstance(stats, dict):
        stats = {}
    action = snapshot.get("current_action") or None
    busy = 1 if action is not None else 0
    if busy_kind is None and isinstance(action, dict):
        busy_kind = str(action.get("kind") or "")
    busy_kind = busy_kind or ""

    base = _format_labels({"instance": instance_id, "role": role})

    def _int(key: str) -> int:
        try:
            return int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _float(key: str) -> float:
        try:
            return float(stats.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    tokens_in = _int("tokens_in")
    tokens_out = _int("tokens_out")
    cost_usd = _float("cost_usd")
    turns = _int("turns")
    tool_calls = _int("tool_calls")
    errors = _int("errors")

    tokens_in_labels = _format_labels(
        {"instance": instance_id, "role": role, "direction": "in"}
    )
    tokens_out_labels = _format_labels(
        {"instance": instance_id, "role": role, "direction": "out"}
    )
    busy_labels = _format_labels(
        {"instance": instance_id, "role": role, "kind": busy_kind}
    )

    lines: List[str] = []
    lines += [
        "# HELP deile_tokens_total Total tokens consumidos desde startup do processo",
        "# TYPE deile_tokens_total counter",
        f"deile_tokens_total{tokens_in_labels} {tokens_in}",
        f"deile_tokens_total{tokens_out_labels} {tokens_out}",
        "# HELP deile_cost_usd_total Custo USD acumulado desde startup",
        "# TYPE deile_cost_usd_total counter",
        f"deile_cost_usd_total{base} {_fmt_float(cost_usd)}",
        "# HELP deile_turns_total Turns processadas desde startup",
        "# TYPE deile_turns_total counter",
        f"deile_turns_total{base} {turns}",
        "# HELP deile_tool_calls_total Chamadas de tool desde startup",
        "# TYPE deile_tool_calls_total counter",
        f"deile_tool_calls_total{base} {tool_calls}",
        "# HELP deile_errors_total Erros desde startup",
        "# TYPE deile_errors_total counter",
        f"deile_errors_total{base} {errors}",
        "# HELP deile_uptime_seconds Segundos desde startup do processo",
        "# TYPE deile_uptime_seconds gauge",
        f"deile_uptime_seconds{base} {_fmt_float(max(uptime_s, 0.0))}",
        "# HELP deile_busy Indica se o processo esta executando uma acao (1=sim, 0=idle)",
        "# TYPE deile_busy gauge",
        f"deile_busy{busy_labels} {busy}",
    ]
    return "\n".join(lines) + "\n"


def _escape_label(value: str) -> str:
    """Escape de label value conforme spec Prometheus."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: Dict[str, str]) -> str:
    """Formata `{k=v}` em ``{k="v",k2="v2"}`` (ordem de inserção)."""
    if not labels:
        return ""
    parts = [f'{k}="{_escape_label(v)}"' for k, v in labels.items()]
    return "{" + ",".join(parts) + "}"


def _fmt_float(v: float) -> str:
    """Formata float preservando precisão (repr) sem notação científica
    para valores comuns. Inf/NaN viram ``+Inf``/``NaN`` conforme spec.
    """
    if v != v:  # NaN
        return "NaN"
    if v == float("inf"):
        return "+Inf"
    if v == float("-inf"):
        return "-Inf"
    # ``repr`` preserva precisão; usamos string normal para evitar ``1e-05``
    # quando não é necessário — o painel/Prometheus aceitam ambos, mas
    # o output fica mais legível.
    return (
        repr(v)
        if abs(v) < 1e-4 or abs(v) > 1e15
        else f"{v:.6f}".rstrip("0").rstrip(".") or "0"
    )


# ── Server ────────────────────────────────────────────────────────────────


class StatusServer:
    """Servidor asyncio sobre Unix socket que expõe o estado do
    :class:`InstanceState`.

    Vida curta: criado pelo InstanceState (Fase 3 da integração) ou pelo
    bootstrap do agente, parado no shutdown. Não tem rate-limit nem auth
    — a permissão do socket file (``0o600``) já restringe ao usuário do
    processo.

    Em Windows o construtor é tolerante (não levanta), mas :meth:`start`
    vira no-op (loga warning uma vez). O painel se vira lendo o state
    file diretamente nesse caso.
    """

    def __init__(
        self, instance_state: "InstanceState", socket_path: Optional[Path] = None
    ) -> None:
        self._instance_state = instance_state
        self._socket_path = (
            Path(socket_path)
            if socket_path is not None
            else self._default_socket_path(instance_state)
        )
        self._server: Optional[asyncio.AbstractServer] = None
        self._started_at_monotonic: Optional[float] = None
        self._stopped: bool = False
        # Lock protege start/stop concorrentes (uso por testes; em produção
        # o lifecycle é sequencial no bootstrap).
        self._lifecycle_lock = asyncio.Lock()

    @staticmethod
    def _default_socket_path(instance_state: "InstanceState") -> Path:
        return instance_state.runtime_dir / f"{instance_state.instance_id}.sock"

    # ── identidade ────────────────────────────────────────────────────────

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def endpoint(self) -> str:
        """``unix:///absolute/path/<id>.sock`` — formato consumido pelo Registry."""
        return f"unix://{self._socket_path.resolve()}"

    @property
    def is_serving(self) -> bool:
        """True quando :meth:`start` foi chamado e o servidor está aceitando."""
        return self._server is not None and not self._stopped

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Cria/escuta o socket. Idempotente — chamadas extras não fazem nada.

        Em Windows: loga warning e retorna (no-op). O ``enable_status_server``
        do :class:`InstanceState` continua True; apenas a Fase 2 não está
        disponível nessa plataforma.
        """
        async with self._lifecycle_lock:
            if self._server is not None:
                return
            if not _is_posix():
                logger.warning(
                    "StatusServer indisponível em %s (POSIX-only); pulando.",
                    sys.platform,
                )
                self._stopped = True
                return
            # Garante o diretório (pode ter sido removido externamente após
            # __init__ do InstanceState).
            self._socket_path.parent.mkdir(parents=True, exist_ok=True)
            # Remove socket file legado: pode existir se o processo anterior
            # crashou sem cleanup. ``unlink(missing_ok=True)`` é seguro.
            try:
                self._socket_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                # Se não conseguimos remover (permissão), ``start_unix_server``
                # vai falhar logo abaixo com erro claro — propagamos.
                logger.warning(
                    "StatusServer não conseguiu remover socket legado %s: %s",
                    self._socket_path,
                    exc,
                )
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(self._socket_path),
            )
            self._started_at_monotonic = time.monotonic()
            # Aplica permissão restritiva (umask pode ter aberto demais).
            try:
                os.chmod(self._socket_path, SOCKET_FILE_MODE)
            except OSError as exc:
                logger.warning(
                    "StatusServer chmod %s falhou: %s (socket continua usável)",
                    self._socket_path,
                    exc,
                )
            logger.debug(
                "StatusServer iniciado: %s (instance=%s)",
                self._socket_path,
                self._instance_state.instance_id,
            )

    async def stop(self) -> None:
        """Fecha o servidor e remove o socket file. Idempotente."""
        async with self._lifecycle_lock:
            if self._stopped and self._server is None:
                return
            self._stopped = True
            server = self._server
            self._server = None
            if server is not None:
                server.close()
                try:
                    await server.wait_closed()
                except Exception as exc:  # noqa: BLE001 — shutdown best-effort
                    logger.debug(
                        "StatusServer wait_closed levantou (id=%s): %s",
                        self._instance_state.instance_id,
                        exc,
                    )
            try:
                self._socket_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.debug(
                    "StatusServer unlink %s falhou: %s",
                    self._socket_path,
                    exc,
                )
            logger.debug(
                "StatusServer parado: %s (instance=%s)",
                self._socket_path,
                self._instance_state.instance_id,
            )

    async def serve_forever(self) -> None:
        """Bloqueia até :meth:`stop`. Tipicamente agendado como task asyncio
        pelo bootstrap (mesmo padrão do ``heartbeat_loop``).

        Re-raise em ``CancelledError`` (princípio 6). Em Windows ou após
        ``stop()`` retorna imediatamente.
        """
        if self._server is None:
            # ``start()`` não foi chamado, ou estamos em Windows — sai limpo.
            return
        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            logger.debug(
                "StatusServer.serve_forever cancelled (id=%s)",
                self._instance_state.instance_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001 — qualquer erro do server
            logger.warning(
                "StatusServer.serve_forever encerrou com erro (id=%s): %s",
                self._instance_state.instance_id,
                exc,
            )

    # ── request handling ──────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Atende 1 request por conexão. Sem keep-alive."""
        try:
            try:
                raw = await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                # Cliente fechou sem mandar ``\n`` — devolve erro e sai.
                await self._write_line(writer, b"ERR empty request")
                return
            except asyncio.LimitOverrunError:
                # Linha excedeu o buffer default (64KB) — segurança.
                await self._write_line(writer, b"ERR line too long")
                return
            if len(raw) > MAX_LINE_BYTES:
                await self._write_line(writer, b"ERR line too long")
                return
            if b"\x00" in raw:
                # Rejeita NUL byte — sinal claro de input malformado.
                await self._write_line(writer, b"ERR invalid char")
                return
            command = raw.decode("utf-8", errors="replace").strip()
            response = self._dispatch(command)
            await self._write_bytes(writer, response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "StatusServer handler error (id=%s): %s",
                self._instance_state.instance_id,
                exc,
            )
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    def _dispatch(self, command: str) -> bytes:
        """Resolve o comando em bytes a serem escritos (incluindo ``\\n``)."""
        if command not in _VALID_COMMANDS:
            return b"ERR unknown command\n"
        if command == "STATUS":
            snap = self._instance_state.snapshot()
            payload = json.dumps(snap, ensure_ascii=False, sort_keys=True)
            return (payload + "\n").encode("utf-8")
        if command == "METRICS":
            snap = self._instance_state.snapshot()
            uptime = self._uptime_s()
            action = snap.get("current_action") if isinstance(snap, dict) else None
            busy_kind = (
                str(action.get("kind"))
                if isinstance(action, dict) and action.get("kind")
                else None
            )
            return format_metrics(snap, uptime_s=uptime, busy_kind=busy_kind).encode(
                "utf-8"
            )
        if command == "FLUSH":
            # Forçar flush: o ``InstanceState`` flusha em toda mutação, mas
            # exposed como debug hook. Acessamos o internal ``_heartbeat``
            # para reescrever sem mudar timestamps de ação.
            try:
                self._instance_state._heartbeat()  # noqa: SLF001 — debug hook
            except Exception as exc:  # noqa: BLE001
                logger.debug("FLUSH falhou: %s", exc)
                return b"ERR flush failed\n"
            return b"OK\n"
        # Inalcançável (validado acima), mas garante retorno bytes.
        return b"ERR unknown command\n"  # pragma: no cover

    def _uptime_s(self) -> float:
        """Segundos desde :meth:`start`; 0 se não iniciado."""
        if self._started_at_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at_monotonic)

    # ── I/O helpers ───────────────────────────────────────────────────────

    @staticmethod
    async def _write_line(writer: asyncio.StreamWriter, payload: bytes) -> None:
        await StatusServer._write_bytes(writer, payload + b"\n")

    @staticmethod
    async def _write_bytes(writer: asyncio.StreamWriter, payload: bytes) -> None:
        try:
            writer.write(payload)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("StatusServer write error: %s", exc)


# ── Client ────────────────────────────────────────────────────────────────


class StatusClient:
    """Cliente síncrono pro :class:`StatusServer` — usado pelo painel TUI.

    Síncrono por desenho: o painel (``infra/k8s/_panel*``) consome em
    threads do ``BackgroundRefresher`` (não-async). Mover para async
    exigiria embedar um event loop por thread. A latência típica é <5ms,
    bem abaixo do timeout default.

    Em erro/timeout/socket-ausente: :meth:`status` e :meth:`metrics`
    retornam None; :meth:`flush` retorna False. Caller decide se é fatal.
    """

    DEFAULT_TIMEOUT_S = 0.5

    def __init__(self, socket_path: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._socket_path = Path(socket_path)
        self._timeout_s = float(timeout_s)

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def status(self) -> Optional[Dict[str, Any]]:
        """Envia ``STATUS\\n`` e retorna o dict decodificado; None em erro."""
        raw = self._send_command(b"STATUS\n")
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def metrics(self) -> Optional[str]:
        """Envia ``METRICS\\n`` e retorna o texto Prometheus; None em erro."""
        raw = self._send_command(b"METRICS\n")
        if raw is None:
            return None
        return raw

    def flush(self) -> bool:
        """Envia ``FLUSH\\n``; True se respondeu ``OK``."""
        raw = self._send_command(b"FLUSH\n")
        return raw is not None and raw.strip() == "OK"

    # ── internals ─────────────────────────────────────────────────────────

    # Defensive cap: respostas legítimas (STATUS/METRICS) ficam em ~5KB; se
    # passar 64KB algo está muito errado.
    _MAX_RESPONSE_BYTES = 65536

    def _recv_until_eof(self, sock: _socket.socket) -> Optional[bytes]:
        """Lê até o servidor fechar; retorna None se exceder cap defensivo."""
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > self._MAX_RESPONSE_BYTES:
                return None
        return b"".join(chunks)

    def _send_command(self, command: bytes) -> Optional[str]:
        """Conexão one-shot. Retorna o payload UTF-8 sem trailing newline
        ou None em qualquer erro (timeout, socket ausente, falha de I/O).
        """
        if not _is_posix() or not self._socket_path.exists():
            return None
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(self._timeout_s)
        try:
            sock.connect(str(self._socket_path))
            sock.sendall(command)
            data = self._recv_until_eof(sock)
        except _socket.timeout:
            logger.debug("StatusClient timeout em %s", self._socket_path)
            return None
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        except OSError as exc:
            logger.debug(
                "StatusClient I/O error em %s: %s",
                self._socket_path,
                exc,
            )
            return None
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if not data:
            return None
        payload = data.decode("utf-8", errors="replace")
        # Tira o ``\n`` final (server adiciona sempre).
        if payload.endswith("\n"):
            payload = payload[:-1]
        return payload
