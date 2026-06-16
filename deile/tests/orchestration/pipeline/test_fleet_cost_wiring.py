"""Integração da fiação de custo central da frota CLI (issue #638).

Prova ponta-a-ponta a FIAÇÃO (não só unit): um dispatch real simulado de um
worker da frota CLI através do ``WorkerImplementer._dispatch`` grava registros no
``UsageRepository`` central com tokens+custo+modelo+stage corretos. Espelha a
harness de ``test_implementer_cli_model_routing.py`` (worker CLI sintético
registrado em runtime + ``_FakeClient`` devolvendo a resposta do dispatch já com
o bloco ``usage`` estruturado que o ``cli_worker_server`` reporta).

Cobre os critérios de aceite #638:
  * dispatch ``wait`` da frota → 1+ registro central com schema correto;
  * dispatch fire-and-forget → custo capturado no reconcile (read-back dedupado);
  * workers núcleo (deile) → NÃO gravam central por esta via (sem regressão).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from deile.config.settings import reset_settings
from deile.orchestration.pipeline.implementer import WorkerImplementer
from deile.orchestration.pipeline.model_resolver import PIPELINE_STAGES
from deile.storage.usage_repository import UsageRepository

_REPO = Path(__file__).resolve().parents[4]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cli_adapters  # noqa: E402

_CLI_KIND = "zzz_costwire"
_CLI_DISPATCHER = f"{_CLI_KIND}-worker"
_CLI_PORT = 8795


@pytest.fixture
def cli_worker_adapter(monkeypatch):
    """Registra um worker CLI sintético e o aponta no stage ``classify``."""
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / f"{_CLI_KIND}.py"
    mod_path.write_text(
        textwrap.dedent(f"""\
            from cli_adapters.base import BaseCliAdapter, WorkResult


            class CostWireAdapter(BaseCliAdapter):
                def build_argv(self, *, brief_path, model, reasoning, workdir, resume, task_id=""):
                    return ["true"]

                def parse_output(self, *, stdout, stderr, rc):
                    return WorkResult(ok=True)


            ADAPTER = CostWireAdapter(kind="{_CLI_KIND}", default_port={_CLI_PORT})
            """),
        encoding="utf-8",
    )
    cli_adapters.reload_adapters()
    # Endpoint na forma REAL ``http://<kind>-worker:<port>`` (não localhost): o
    # _worker_kind_from_url deriva o kind do host no caminho fire-and-forget. O
    # _FakeClient ignora a URL — isto só garante a derivação correta do worker.
    monkeypatch.setenv(
        f"DEILE_{_CLI_KIND.upper()}_WORKER_ENDPOINT",
        f"http://{_CLI_DISPATCHER}:18795",
    )
    from deile.orchestration.pipeline.cli_worker_scaler import (
        EnsureReplicaOutcome,
        ScaleResult,
    )

    async def _ready(_dispatcher):
        return EnsureReplicaOutcome(ScaleResult.READY, "test: ready")

    monkeypatch.setattr(
        "deile.orchestration.pipeline.cli_worker_scaler.ensure_replica",
        _ready,
    )
    try:
        yield
    finally:
        mod_path.unlink(missing_ok=True)
        sys.modules.pop(f"cli_adapters.{_CLI_KIND}", None)
        cli_adapters.reload_adapters()


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_MODEL_{stage.upper()}", raising=False)
        monkeypatch.delenv(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_DISPATCH_MODE", raising=False)
    monkeypatch.delenv("DEILE_PREFERRED_MODEL", raising=False)
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def central_repo(tmp_path, monkeypatch):
    """UsageRepository central isolado + injeção no singleton lido pelo recorder."""
    repo = UsageRepository(db_path=tmp_path / "usage.db")
    import deile.storage.usage_repository as ur

    monkeypatch.setattr(ur, "_usage_repository", repo)
    return repo


class _FakeClient:
    """Devolve a resposta do dispatch já com o bloco ``usage`` estruturado.

    ``get_resume_info`` alimenta o read-back fire-and-forget do reconcile.
    """

    def __init__(self, response, resume_info=None):
        self._response = response
        self._resume_info = resume_info
        self.last_payload = None

    async def dispatch(self, payload, *, wait, endpoint_url=None):
        self.last_payload = payload
        return self._response

    async def get_resume_info(self, task_id, *, endpoint_url=None):
        return self._resume_info


def _all(repo: UsageRepository) -> list:
    with repo._connect() as conn:  # noqa: SLF001 — leitura direta no teste
        return [
            dict(r) for r in conn.execute("SELECT * FROM usage_records ORDER BY id")
        ]


# --------------------------------------------------------------------------- #
# Caminho wait: dispatch da frota → registro central                           #
# --------------------------------------------------------------------------- #
class TestWaitPathWiring:
    async def test_cli_dispatch_wait_records_central_usage(
        self,
        cli_worker_adapter,
        central_repo,
        monkeypatch,
    ):
        """AC #638: um dispatch real (wait) da frota grava 1 registro central com
        tokens+custo+modelo+stage corretos — fiação ponta-a-ponta."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_CLASSIFY", _CLI_DISPATCHER)
        monkeypatch.setenv(
            "DEILE_PIPELINE_MODEL_CLASSIFY", "openrouter/deepseek/deepseek-v4-pro"
        )
        reset_settings()
        response = {
            "ok": True,
            "summary": "done",
            "task_id": "deadbeef",
            "usage": {
                "worker": _CLI_KIND,
                "model": "openrouter/deepseek/deepseek-v4-pro",
                "tokens_by_model": {
                    "openrouter/deepseek/deepseek-v4-pro": {
                        "in": 1500,
                        "out": 300,
                        "cache_read": 21415,
                        "cache_write": 0,
                    },
                },
            },
        }
        impl = WorkerImplementer(client=_FakeClient(response))
        # _dispatch é o ponto de fiação real (mesma chamada que critique/refine/
        # mention fazem); nowait=False exercita o caminho wait que lê o bloco usage.
        await impl._dispatch(
            "brief",
            channel_id="pipeline-issue-242",
            stage="classify",
            nowait=False,
        )
        recs = _all(central_repo)
        assert len(recs) == 1, recs
        r = recs[0]
        assert r["provider_id"] == _CLI_KIND  # worker
        assert r["tier"] == "classify"  # stage
        assert r["session_id"] == "pipeline-issue-242"
        assert r["model_id"] == "openrouter/deepseek/deepseek-v4-pro"
        assert r["prompt_tokens"] == 1500
        assert r["completion_tokens"] == 300
        assert r["cached_tokens"] == 21415
        assert r["cost_usd"] > 0

    async def test_cli_dispatch_without_usage_block_writes_nothing(
        self,
        cli_worker_adapter,
        central_repo,
        monkeypatch,
    ):
        """Worker antigo (sem bloco usage) → no-op central, sem quebrar dispatch."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_CLASSIFY", _CLI_DISPATCHER)
        reset_settings()
        impl = WorkerImplementer(client=_FakeClient({"ok": True, "summary": "x"}))
        out = await impl._dispatch(
            "brief",
            channel_id="pipeline-issue-1",
            stage="classify",
            nowait=False,
        )
        assert out.ok is True
        assert _all(central_repo) == []

    async def test_deile_worker_does_not_write_fleet_central(
        self,
        central_repo,
    ):
        """Sem regressão: deile-worker (núcleo) NÃO grava central por esta via —
        ele contabiliza no SQLite do próprio pod. O recorder só roda p/ CLI."""
        reset_settings()
        response = {
            "ok": True,
            "summary": "ok",
            "task_id": "t1",
            # Mesmo que viesse um bloco usage, o gate is_cli_worker barra.
            "usage": {
                "worker": "deile",
                "model": "x:y",
                "tokens_by_model": {"x:y": {"in": 9, "out": 9}},
            },
        }
        impl = WorkerImplementer(client=_FakeClient(response))
        await impl._dispatch(
            "brief",
            channel_id="pipeline-issue-2",
            stage="classify",
            nowait=False,
        )
        assert _all(central_repo) == []


# --------------------------------------------------------------------------- #
# Caminho fire-and-forget: reconcile lê resume-info → registro central dedupado #
# --------------------------------------------------------------------------- #
class TestFireAndForgetWiring:
    async def test_reconcile_records_central_usage_from_resume_info(
        self,
        cli_worker_adapter,
        central_repo,
        monkeypatch,
    ):
        """AC #638: implement paralelo (fire-and-forget) tem o custo capturado no
        reconcile (a resposta do 202 é descartada). DONE + bloco usage → grava."""
        from deile.orchestration.pipeline import stages

        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", _CLI_DISPATCHER)
        reset_settings()
        resume_info = {
            "task_id": "abc123",
            "workdir_exists": True,
            "claude_alive": False,
            "last_completed_at": 1_700_000_000,
            "last_is_error": False,
            "cli_model": "openrouter/qwen3-coder-plus",
            "usage": {
                "model": "openrouter/qwen3-coder-plus",
                "tokens_by_model": {
                    "openrouter/qwen3-coder-plus": {
                        "in": 4000,
                        "out": 900,
                        "cache_read": 0,
                        "cache_write": 0,
                    },
                },
            },
        }
        impl = WorkerImplementer(client=_FakeClient({}, resume_info=resume_info))
        monitor = SimpleNamespace(implementer=impl)

        state, _info = await stages._fetch_reconcile_state(
            monitor,
            "abc123",
            "implement",
            channel_id="pipeline-issue-50",
        )
        assert state == stages._RECON_DONE
        recs = _all(central_repo)
        assert len(recs) == 1
        r = recs[0]
        assert r["provider_id"] == _CLI_KIND
        assert r["tier"] == "implement"
        assert r["session_id"] == "pipeline-issue-50#abc123"  # dedup key
        assert r["model_id"] == "openrouter/qwen3-coder-plus"
        assert r["prompt_tokens"] == 4000 and r["completion_tokens"] == 900

        # Reconcile roda a cada tick: 2ª leitura DONE → no-op idempotente.
        await stages._fetch_reconcile_state(
            monitor,
            "abc123",
            "implement",
            channel_id="pipeline-issue-50",
        )
        assert len(_all(central_repo)) == 1
