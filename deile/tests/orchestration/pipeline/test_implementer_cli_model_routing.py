"""Roteamento do campo de modelo por dispatcher (frota multi-CLI, Fase 3 B2/B3).

A ÚNICA ramificação nova no cliente HTTP do ``WorkerImplementer``: quando o
dispatcher de um stage é um worker da frota CLI (``*-worker``), o payload carrega
``cli_model`` (id nativo do CLI, string livre) em vez de ``preferred_model``
(``provider:model`` do deile-worker). Os workers núcleo (deile/claude) continuam
recebendo ``preferred_model`` exatamente como antes — esta suíte prova os dois
caminhos lado a lado sem relaxar o validator ``provider:model``.

O worker CLI sintético é registrado em runtime via ``cli_adapters.reload_adapters``
(pacote em ``infra/k8s/`` — path inserido manualmente, convenção dos testes de
infra). Apontar o stage a ele é feito por ``DEILE_PIPELINE_DISPATCH_<STAGE>``.
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

_REPO = Path(__file__).resolve().parents[4]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cli_adapters  # noqa: E402

_CLI_KIND = "zzz_clirouter"
_CLI_DISPATCHER = f"{_CLI_KIND}-worker"
_CLI_PORT = 8796


@pytest.fixture
def cli_worker_adapter(monkeypatch):
    """Registra um worker CLI sintético e o aponta no stage ``implement``."""
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / f"{_CLI_KIND}.py"
    mod_path.write_text(
        textwrap.dedent(
            f'''\
            from cli_adapters.base import BaseCliAdapter, WorkResult


            class CliRouterAdapter(BaseCliAdapter):
                def build_argv(self, *, brief_path, model, reasoning, workdir, resume, task_id=""):
                    return ["true"]

                def parse_output(self, *, stdout, stderr, rc):
                    return WorkResult(ok=True)


            ADAPTER = CliRouterAdapter(kind="{_CLI_KIND}", default_port={_CLI_PORT})
            '''
        ),
        encoding="utf-8",
    )
    cli_adapters.reload_adapters()
    # Endpoint local pra evitar DNS de Service do cluster (o client é fake de
    # qualquer forma; isto só evita resolução acidental).
    monkeypatch.setenv(
        f"DEILE_{_CLI_KIND.upper()}_WORKER_ENDPOINT", "http://localhost:18796"
    )
    # Ensure-replica (scale-to-zero, plano B5) corre antes do POST para workers
    # CLI. Aqui o foco é o roteamento do modelo/endpoint, não o scaling — então
    # forçamos READY (worker "já no ar") para o dispatch chegar ao client fake.
    from deile.orchestration.pipeline.cli_worker_scaler import (
        EnsureReplicaOutcome, ScaleResult)

    async def _ready(_dispatcher):
        return EnsureReplicaOutcome(ScaleResult.READY, "test: ready")

    monkeypatch.setattr(
        "deile.orchestration.pipeline.cli_worker_scaler.ensure_replica", _ready,
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


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.last_payload = None
        self.last_endpoint = None

    async def dispatch(self, payload, *, wait, endpoint_url=None):
        self.last_payload = payload
        self.last_endpoint = endpoint_url
        return self._response


def _make_monitor():
    from deile.orchestration.forge.base import ForgeConfig, ForgeKind

    monitor = SimpleNamespace()
    monitor.config = SimpleNamespace(
        repo="owner/name", main_branch="main", base_repo_path=Path("/tmp/x"),
        mention_handle="@deile-one",
    )
    monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
    monitor.forge = SimpleNamespace(
        config=ForgeConfig(
            kind=ForgeKind.GITHUB, host="github.com",
            project_path="owner/name", cli_path="/usr/bin/gh",
        ),
    )
    return monitor


def _issue(number=242, labels=()):
    return SimpleNamespace(number=number, title="t", body="b", labels=labels)


class TestCliModelRouting:
    async def test_cli_worker_stage_sends_cli_model_not_preferred_model(
        self, cli_worker_adapter, monkeypatch,
    ):
        """Stage roteado a worker CLI → payload tem ``cli_model`` (string livre),
        sem ``preferred_model``. O MESMO env var de modelo per-stage é lido."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", _CLI_DISPATCHER)
        # Id nativo do CLI: string livre que NÃO casa o regex provider:model.
        monkeypatch.setenv(
            "DEILE_PIPELINE_MODEL_IMPLEMENT", "openrouter/deepseek/deepseek-chat"
        )
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert client.last_payload["cli_model"] == "openrouter/deepseek/deepseek-chat"
        assert "preferred_model" not in client.last_payload

    async def test_cli_worker_endpoint_resolves_from_registry(
        self, cli_worker_adapter, monkeypatch,
    ):
        """O dispatch vai para o endpoint do worker CLI (env override aqui)."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", _CLI_DISPATCHER)
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert client.last_endpoint == "http://localhost:18796"

    async def test_cli_worker_unset_model_omits_both_fields(
        self, cli_worker_adapter, monkeypatch,
    ):
        """Sem override de modelo, nem ``cli_model`` nem ``preferred_model`` vão
        no wire — o worker CLI usa o modelo default do adapter/imagem."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", _CLI_DISPATCHER)
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert "cli_model" not in client.last_payload
        assert "preferred_model" not in client.last_payload

    async def test_deile_worker_stage_keeps_preferred_model(self, monkeypatch):
        """Caminho núcleo intacto: deile-worker recebe ``preferred_model``
        (provider:model) e NUNCA ``cli_model`` — sem regressão da #305."""
        # Sem DISPATCH override → default deile-worker.
        monkeypatch.setenv(
            "DEILE_PIPELINE_MODEL_IMPLEMENT", "deepseek:deepseek-v4-pro"
        )
        reset_settings()
        client = _FakeClient({"ok": True, "summary": "done"})
        await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert client.last_payload["preferred_model"] == "deepseek:deepseek-v4-pro"
        assert "cli_model" not in client.last_payload
