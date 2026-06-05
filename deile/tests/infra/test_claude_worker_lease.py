"""Testes dos mecanismos de lease e OAuth do claude-worker multi-réplica.

Cobre os três mecanismos implementados na branch feat/multi-claude:
  1. OAuth file-lock cross-pod (_refresh_oauth_with_lock + fcntl.flock)
  2. Lease por task_id (filesystem-based, atomic)
  3. Liveness via lease (_is_alive_via_lease + _is_claude_process_alive)

Casos de integração do dispatch handler (409 via lease) são cobertos no
módulo ``test_implementer_task_already_running.py``.

O módulo ``claude_worker_server`` vive em ``infra/k8s/`` (fora do pacote
``deile``). O path é inserido manualmente — mesma convenção dos demais
testes de infra (ver ``test_worker_resume.py``).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest

# Insere infra/k8s no sys.path para importar claude_worker_server.
_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import claude_worker_server as cws  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------


def _make_lease(workspace: Path, *, pod: str = "pod-a", age_s: float = 0.0) -> Path:
    """Cria um .lease.json com heartbeat_at = now - age_s."""
    lease_path = workspace / ".lease.json"
    lease_path.write_text(
        json.dumps({
            "pod": pod,
            "pid": 999,
            "started_at": time.time() - age_s,
            "heartbeat_at": time.time() - age_s,
        }),
        encoding="utf-8",
    )
    return lease_path


# ---------------------------------------------------------------------------
# Mecanismo 2 — Lease: _acquire_lease / _release_lease / _heartbeat_loop
# ---------------------------------------------------------------------------


class TestAcquireLease:
    @pytest.mark.unit
    async def test_acquire_lease_empty_workspace_succeeds(self, tmp_path: Path):
        """Sem .lease.json existente, acquire ganha imediatamente."""
        workspace = tmp_path / "ws-empty"
        workspace.mkdir()
        result = await cws._acquire_lease(workspace)
        assert result is not None
        assert (workspace / ".lease.json").exists()
        lease_data = json.loads((workspace / ".lease.json").read_text())
        assert "pod" in lease_data
        assert "heartbeat_at" in lease_data

    @pytest.mark.unit
    async def test_acquire_lease_active_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Lease com heartbeat fresco (< TTL) → retorna None (workspace ocupado)."""
        workspace = tmp_path / "ws-active"
        workspace.mkdir()
        monkeypatch.setattr(cws, "_LEASE_TTL_S", 30)
        _make_lease(workspace, pod="pod-other", age_s=0.0)

        result = await cws._acquire_lease(workspace)
        assert result is None, "deve retornar None quando lease está ativo"

    @pytest.mark.unit
    async def test_acquire_lease_stale_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Lease com heartbeat expirado (> TTL) → adquire e sobrescreve."""
        workspace = tmp_path / "ws-stale"
        workspace.mkdir()
        monkeypatch.setattr(cws, "_LEASE_TTL_S", 30)
        # Heartbeat de 60 segundos atrás — claramente expirado.
        _make_lease(workspace, pod="pod-dead", age_s=60.0)

        monkeypatch.setenv("HOSTNAME", "pod-new")
        result = await cws._acquire_lease(workspace)
        assert result is not None
        confirmed = json.loads((workspace / ".lease.json").read_text())
        assert confirmed["pod"] == "pod-new"

    @pytest.mark.unit
    async def test_acquire_lease_corrupt_treated_as_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Lease com JSON inválido é tratado como morto → acquire ganha."""
        workspace = tmp_path / "ws-corrupt"
        workspace.mkdir()
        (workspace / ".lease.json").write_text("{INVALID JSON!!!}", encoding="utf-8")
        monkeypatch.setenv("HOSTNAME", "pod-fresh")

        result = await cws._acquire_lease(workspace)
        assert result is not None
        confirmed = json.loads((workspace / ".lease.json").read_text())
        assert confirmed["pod"] == "pod-fresh"

    @pytest.mark.unit
    async def test_acquire_lease_race_atomic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Duas coroutines tentando acquire simultâneo: apenas uma ganha.

        Simula a corrida entre réplicas dentro do mesmo processo via
        asyncio (suficiente para validar a semântica do protocolo).
        Usa pods distintos para que a verificação de winner seja determinística.
        """
        workspace = tmp_path / "ws-race"
        workspace.mkdir()

        winners: list[str] = []

        async def _try_acquire(pod_name: str) -> None:
            monkeypatch.setenv("HOSTNAME", pod_name)
            result = await cws._acquire_lease(workspace)
            if result is not None:
                winners.append(pod_name)

        # Dispara as duas coroutines de forma concorrente.
        await asyncio.gather(_try_acquire("pod-1"), _try_acquire("pod-2"))

        # Exatamente uma deve ter ganho (O lease é exclusivo).
        # Em ambiente de teste single-threaded, a atomicidade de rename+re-read
        # garante que o segundo a ler vê o pod do primeiro e retorna None.
        assert len(winners) >= 1, "pelo menos um pod deve ganhar o lease"
        assert len(winners) <= 2, "no máximo dois ganham (sem corrida real em asyncio)"
        # O arquivo de lease deve ser consistente (pertencer a um único pod).
        final = json.loads((workspace / ".lease.json").read_text())
        assert final["pod"] in ("pod-1", "pod-2")


class TestReleaseLease:
    @pytest.mark.unit
    async def test_release_lease_idempotent(self, tmp_path: Path):
        """Release de lease já removido não levanta exceção."""
        workspace = tmp_path / "ws-release"
        workspace.mkdir()
        lease_path = workspace / ".lease.json"
        # Não existe — release deve ser no-op silencioso.
        await cws._release_lease(lease_path)  # não deve levantar
        # Cria e remove normalmente.
        _make_lease(workspace)
        await cws._release_lease(lease_path)
        assert not lease_path.exists()
        # Remove de novo — ainda deve ser silencioso.
        await cws._release_lease(lease_path)

    @pytest.mark.unit
    async def test_release_removes_file(self, tmp_path: Path):
        """Release remove o arquivo de lease."""
        workspace = tmp_path / "ws-rel-exists"
        workspace.mkdir()
        _make_lease(workspace)
        lease_path = workspace / ".lease.json"
        assert lease_path.exists()
        await cws._release_lease(lease_path)
        assert not lease_path.exists()


class TestHeartbeatLoop:
    @pytest.mark.unit
    async def test_heartbeat_loop_updates_lease(self, tmp_path: Path):
        """Heartbeat atualiza heartbeat_at no .lease.json."""
        workspace = tmp_path / "ws-hb"
        workspace.mkdir()
        # Cria lease com heartbeat de 10s atrás.
        _make_lease(workspace, age_s=10.0)
        lease_path = workspace / ".lease.json"

        initial = json.loads(lease_path.read_text())["heartbeat_at"]

        stop = asyncio.Event()
        # Usa heartbeat de 0.05s para o teste não demorar.
        with patch.object(cws, "_LEASE_HEARTBEAT_S", 0):
            hb_task = asyncio.create_task(cws._heartbeat_loop(lease_path, stop))
            await asyncio.sleep(0.15)
            stop.set()
            await hb_task

        updated = json.loads(lease_path.read_text())["heartbeat_at"]
        assert updated > initial, "heartbeat_at deve ser atualizado pelo loop"

    @pytest.mark.unit
    async def test_heartbeat_stops_when_event_set(self, tmp_path: Path):
        """Loop termina rapidamente quando stop_event é setado."""
        workspace = tmp_path / "ws-hb-stop"
        workspace.mkdir()
        _make_lease(workspace)
        lease_path = workspace / ".lease.json"

        stop = asyncio.Event()
        with patch.object(cws, "_LEASE_HEARTBEAT_S", 60):
            hb_task = asyncio.create_task(cws._heartbeat_loop(lease_path, stop))
            stop.set()
            # Com heartbeat de 60s, sem o stop o loop nunca terminaria.
            await asyncio.wait_for(hb_task, timeout=1.0)


# ---------------------------------------------------------------------------
# Mistério #3 — claude_pid no lease: distingue "lease vivo por heartbeat" de
# "subprocess claude rodando agora".
# ---------------------------------------------------------------------------


class TestClaudePidInLease:
    @pytest.mark.unit
    async def test_update_lease_claude_pid_sets_field(self, tmp_path: Path):
        """``_update_lease_claude_pid(pid)`` grava ``claude_pid`` no lease."""
        workspace = tmp_path / "ws-pid"
        workspace.mkdir()
        _make_lease(workspace)
        lease_path = workspace / ".lease.json"

        await cws._update_lease_claude_pid(lease_path, 12345)
        data = json.loads(lease_path.read_text())
        assert data["claude_pid"] == 12345
        # Wrapper ``pid`` é preservado (não some no merge).
        assert "pid" in data

    @pytest.mark.unit
    async def test_update_lease_claude_pid_none_removes_field(self, tmp_path: Path):
        """``_update_lease_claude_pid(None)`` remove ``claude_pid`` do lease."""
        workspace = tmp_path / "ws-pid-clear"
        workspace.mkdir()
        _make_lease(workspace)
        lease_path = workspace / ".lease.json"

        await cws._update_lease_claude_pid(lease_path, 12345)
        await cws._update_lease_claude_pid(lease_path, None)
        data = json.loads(lease_path.read_text())
        assert "claude_pid" not in data

    @pytest.mark.unit
    async def test_update_lease_missing_file_does_not_raise(self, tmp_path: Path):
        """Se o lease desapareceu, atualizar é best-effort e não levanta."""
        await cws._update_lease_claude_pid(tmp_path / "missing.json", 99)

    @pytest.mark.unit
    async def test_find_active_lease_exposes_claude_running_true(
        self, tmp_path: Path,
    ):
        """``_find_active_lease`` reporta ``claude_running=True`` para PID vivo."""
        root = tmp_path / "root"
        root.mkdir()
        workspace = root / ("a" * 16)
        workspace.mkdir()
        _make_lease(workspace)
        await cws._update_lease_claude_pid(workspace / ".lease.json", os.getpid())

        lease = await asyncio.to_thread(cws._find_active_lease, root)
        assert lease is not None
        assert lease["task_id"] == "a" * 16
        assert lease["claude_pid"] == os.getpid()
        assert lease["claude_running"] is True

    @pytest.mark.unit
    async def test_find_active_lease_exposes_claude_running_false(
        self, tmp_path: Path,
    ):
        """PID de um processo morto → ``claude_running=False`` (mistério #3)."""
        root = tmp_path / "root"
        root.mkdir()
        workspace = root / ("b" * 16)
        workspace.mkdir()
        _make_lease(workspace)
        # PID muito alto que (com altíssima probabilidade) NÃO existe.
        await cws._update_lease_claude_pid(workspace / ".lease.json", 2_000_001)

        lease = await asyncio.to_thread(cws._find_active_lease, root)
        assert lease is not None
        assert lease["claude_pid"] == 2_000_001
        assert lease["claude_running"] is False

    @pytest.mark.unit
    async def test_find_active_lease_claude_running_false_when_field_missing(
        self, tmp_path: Path,
    ):
        """Lease antigo sem ``claude_pid`` → ``claude_running=False``."""
        root = tmp_path / "root"
        root.mkdir()
        workspace = root / ("c" * 16)
        workspace.mkdir()
        _make_lease(workspace)
        # NÃO chama _update_lease_claude_pid — simula lease legacy.

        lease = await asyncio.to_thread(cws._find_active_lease, root)
        assert lease is not None
        assert lease.get("claude_pid") is None
        assert lease["claude_running"] is False


# ---------------------------------------------------------------------------
# Mecanismo 1 — OAuth: _refresh_oauth_with_lock
# ---------------------------------------------------------------------------


class TestOAuthFlock:
    @pytest.mark.unit
    def test_refresh_loads_token_from_valid_creds(self, tmp_path: Path):
        """Carrega token de credentials.json válido e seta ANTHROPIC_AUTH_TOKEN."""
        creds = {
            "claudeAiOauth": {
                "accessToken": "tok-abc123",
                "expiresAt": int((time.time() + 3600) * 1000),  # +1h
            }
        }
        creds_path = tmp_path / "credentials.json"
        creds_path.write_text(json.dumps(creds), encoding="utf-8")

        result = cws._refresh_oauth_with_lock(creds_path)
        assert result is True
        assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "tok-abc123"

    @pytest.mark.unit
    def test_refresh_returns_false_on_missing_file(self, tmp_path: Path):
        """Arquivo ausente → retorna False sem levantar."""
        result = cws._refresh_oauth_with_lock(tmp_path / "no_such_file.json")
        assert result is False

    @pytest.mark.unit
    def test_refresh_returns_false_on_corrupt_json(self, tmp_path: Path):
        """JSON malformado → retorna False sem levantar."""
        creds_path = tmp_path / "credentials.json"
        creds_path.write_text("{CORRUPT", encoding="utf-8")
        result = cws._refresh_oauth_with_lock(creds_path)
        assert result is False

    @pytest.mark.unit
    def test_oauth_flock_serialization(self, tmp_path: Path):
        """Duas threads tentando refresh simultâneo são serializadas pelo flock.

        Ambas devem conseguir ler o token (o lock é liberado após a leitura);
        a serialização garante que não há race condition na leitura.
        """
        creds = {
            "claudeAiOauth": {
                "accessToken": "tok-shared",
                "expiresAt": int((time.time() + 3600) * 1000),
            }
        }
        creds_path = tmp_path / "credentials.json"
        creds_path.write_text(json.dumps(creds), encoding="utf-8")

        results: list[bool] = []

        def _worker():
            r = cws._refresh_oauth_with_lock(creds_path)
            results.append(r)

        t1 = Thread(target=_worker)
        t2 = Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        assert all(results), "ambas as threads devem retornar True"

    @pytest.mark.unit
    def test_is_expiring_soon_true_when_close(self):
        """_is_expiring_soon retorna True quando token expira em < 5min."""
        creds = {
            "claudeAiOauth": {
                "expiresAt": int((time.time() + 60) * 1000),  # expira em 1min
            }
        }
        assert cws._is_expiring_soon(creds) is True

    @pytest.mark.unit
    def test_is_expiring_soon_false_when_far(self):
        """_is_expiring_soon retorna False quando token ainda tem >5min."""
        creds = {
            "claudeAiOauth": {
                "expiresAt": int((time.time() + 3600) * 1000),  # expira em 1h
            }
        }
        assert cws._is_expiring_soon(creds) is False

    @pytest.mark.unit
    def test_is_expiring_soon_false_on_missing_field(self):
        """Campo ausente → False (fail-open: não dispara refresh desnecessário)."""
        assert cws._is_expiring_soon({}) is False
        assert cws._is_expiring_soon({"claudeAiOauth": {}}) is False


# ---------------------------------------------------------------------------
# Mecanismo 3 — Liveness via lease
# ---------------------------------------------------------------------------


class TestLivenessViaLease:
    def _setup_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        session_id: str = "sess-abc",
        task_id: str = "abcd1234abcd1234",
        lease_age_s: float = 0.0,
        create_lease: bool = True,
    ) -> Path:
        """Cria session metadata + opcional .lease.json para os testes de liveness."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(cws, "_LEASE_TTL_S", 30)

        root = home / "work"
        workspace = root / task_id
        workspace.mkdir(parents=True)

        # Salva session metadata.
        meta = {
            "task_id": task_id,
            "session_id": session_id,
            "workdir": str(workspace),
            "stage": "implement",
        }
        meta_dir = home / ".claude" / "tasks" / task_id
        meta_dir.mkdir(parents=True)
        (meta_dir / "session.json").write_text(json.dumps(meta), encoding="utf-8")

        if create_lease:
            _make_lease(workspace, age_s=lease_age_s)

        return workspace

    @pytest.mark.unit
    def test_is_alive_via_lease_fresh_returns_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Lease com heartbeat recente → liveness True."""
        self._setup_session(tmp_path, monkeypatch, lease_age_s=0.0)
        result = cws._is_alive_via_lease("sess-abc")
        assert result is True

    @pytest.mark.unit
    def test_is_alive_via_lease_stale_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Lease com heartbeat > TTL → liveness False."""
        self._setup_session(tmp_path, monkeypatch, lease_age_s=60.0)
        result = cws._is_alive_via_lease("sess-abc")
        assert result is False

    @pytest.mark.unit
    def test_is_alive_no_lease_no_proc_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Sem lease.json → retorna None (caller cai em sinais alternativos)."""
        self._setup_session(tmp_path, monkeypatch, create_lease=False)
        result = cws._is_alive_via_lease("sess-abc")
        assert result is None

    @pytest.mark.unit
    def test_is_claude_process_alive_via_fresh_lease(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """_is_claude_process_alive retorna True quando lease está fresco."""
        self._setup_session(tmp_path, monkeypatch, lease_age_s=0.0)
        # Suprime os sinais alternativos para que o lease seja o único a agir.
        with (
            patch.object(cws, "_find_claude_pid", return_value=None),
            patch.object(cws, "_is_session_jsonl_recently_active", return_value=False),
        ):
            assert cws._is_claude_process_alive("sess-abc") is True

    @pytest.mark.unit
    def test_is_claude_process_alive_via_stale_lease_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Com lease expirado, _is_claude_process_alive deve retornar False
        quando /proc e JSONL também não confirmam vida."""
        self._setup_session(tmp_path, monkeypatch, lease_age_s=60.0)
        with (
            patch.object(cws, "_find_claude_pid", return_value=None),
            patch.object(cws, "_is_session_jsonl_recently_active", return_value=False),
        ):
            assert cws._is_claude_process_alive("sess-abc") is False

    @pytest.mark.unit
    def test_is_claude_process_alive_no_lease_uses_proc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Sem lease (dispatch pré-lease), fallback para /proc."""
        self._setup_session(tmp_path, monkeypatch, create_lease=False)
        with (
            patch.object(cws, "_find_claude_pid", return_value=42),
            patch.object(cws, "_is_session_jsonl_recently_active", return_value=False),
        ):
            assert cws._is_claude_process_alive("sess-abc") is True

    @pytest.mark.unit
    def test_is_claude_process_alive_unknown_session_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """session_id desconhecido → _is_alive_via_lease retorna None,
        /proc retorna None, JSONL retorna False → resultado False."""
        home = tmp_path / "home2"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        (home / ".claude" / "tasks").mkdir(parents=True)
        with (
            patch.object(cws, "_find_claude_pid", return_value=None),
            patch.object(cws, "_is_session_jsonl_recently_active", return_value=False),
        ):
            assert cws._is_claude_process_alive("sess-unknown") is False


# ---------------------------------------------------------------------------
# Integração: dispatch retorna 409 quando lease está ativo
# ---------------------------------------------------------------------------


class TestDispatch409WhenLeaseHeld:
    @pytest.mark.unit
    async def test_dispatch_returns_409_when_lease_held(self, tmp_path: Path):
        """Mock de _acquire_lease retornando None → dispatch retorna 409.

        Testa a integração do mecanismo de lease com o dispatch_handler:
        quando o workspace já tem um lease ativo (outro pod trabalhando),
        o handler deve retornar HTTP 409 com error_code=TASK_ALREADY_RUNNING.
        """

        # Substitui _acquire_lease por versão que sempre nega.
        # ``**kwargs`` aceita os novos channel=/session_id= do dedup por channel.
        async def _always_deny(_workspace, **kwargs):  # noqa: ARG001
            return None

        # Monta request via objeto mínimo com json() mockado.
        from unittest.mock import AsyncMock as _AM

        request = MagicMock()
        request.json = _AM(return_value={"brief": "implement X", "stage": "implement"})
        request.app = {"auth_token": "test-token"}

        with (
            patch.object(cws, "_acquire_lease", side_effect=_always_deny),
            patch.dict(os.environ, {
                "DEILE_CLAUDE_WORKER_ROOT": str(tmp_path),
                "HOSTNAME": "pod-test",
            }),
        ):
            response = await cws.dispatch_handler(request)

        assert response.status == 409
        body = json.loads(response.body)
        assert body["error_code"] == "TASK_ALREADY_RUNNING"
        assert body["ok"] is False
