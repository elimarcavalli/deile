"""WorkerImplementer routing — endpoint resolution per-stage (issue #309 fase 2).

Cobre o caminho de roteamento ``stage → dispatcher → endpoint URL`` via
:mod:`deile.orchestration.pipeline.dispatch_resolver`. Complementa
``test_implementer_per_stage_model.py`` (que cobre o eixo ortogonal de
*modelo*); aqui o foco é qual **worker pod** recebe o POST.

Cada teste patcha ``WorkerImplementer._post_dispatch`` (a costura HTTP)
para capturar a URL resolvida sem tocar em I/O real. Os métodos
``implement`` / ``review`` / ``mention`` podem disparar erros downstream
(render de brief com stubs simplificados); o que importa é a URL
passada ao ``_post_dispatch`` *antes* de qualquer falha — daí o
``try/except`` defensivo em cada teste.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from deile.orchestration.pipeline.implementer import WorkerImplementer


def _make_monitor():
    """Stub mínimo de PipelineMonitor para uso nos métodos do implementer."""
    monitor = SimpleNamespace()
    monitor.config = SimpleNamespace(
        repo="elimarcavalli/deile",
        main_branch="main",
        base_repo_path=Path("/tmp/fake"),
        mention_handle="@deile-one",
    )
    monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
    from deile.orchestration.forge.base import ForgeConfig, ForgeKind
    monitor.forge = SimpleNamespace(
        config=ForgeConfig(
            kind=ForgeKind.GITHUB,
            host="github.com",
            project_path="elimarcavalli/deile",
            cli_path="/usr/bin/gh",
        ),
    )
    return monitor


def _issue(number=42, labels=()):
    return SimpleNamespace(number=number, title="t", body="b", labels=labels)


def _pr(number=100):
    return SimpleNamespace(
        number=number, title="t", head_ref=f"auto/issue-{number}",
        url=f"https://github.com/elimarcavalli/deile/pull/{number}",
    )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Limpa env vars de dispatch entre testes para evitar bleed-through."""
    for var in (
        "DEILE_PIPELINE_DISPATCH_CLASSIFY",
        "DEILE_PIPELINE_DISPATCH_REFINE",
        "DEILE_PIPELINE_DISPATCH_IMPLEMENT",
        "DEILE_PIPELINE_DISPATCH_PR_REVIEW",
        "DEILE_PIPELINE_DISPATCH_FOLLOW_UPS",
        "DEILE_PIPELINE_DISPATCH_MODE",
        "DEILE_WORKER_ENDPOINT",
        "DEILE_CLAUDE_WORKER_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


class TestEndpointResolution:
    """O método público ``_resolve_endpoint(stage)`` é a unidade de
    decisão; testar separado das chamadas async simplifica e dá
    cobertura focada no resolver."""

    def test_override_takes_precedence_over_env(self, monkeypatch):
        """``endpoint_override`` ganha de qualquer env var."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
        monkeypatch.setenv("DEILE_CLAUDE_WORKER_ENDPOINT", "http://from-env:1111")
        impl = WorkerImplementer(endpoint_override="http://forced:9999")
        assert impl._resolve_endpoint("implement") == "http://forced:9999"
        # E vale para QUALQUER stage — o override é absoluto.
        assert impl._resolve_endpoint("pr_review") == "http://forced:9999"

    def test_resolves_implement_via_stage_env(self, monkeypatch):
        """Sem override, usa env stage-específica → claude-worker."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
        impl = WorkerImplementer()
        url = impl._resolve_endpoint("implement")
        assert "claude-worker:8767" in url

    def test_resolves_pr_review_independent_of_implement(self, monkeypatch):
        """Cada stage resolve sua própria env var sem cross-contamination."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_PR_REVIEW", "deile-worker")
        impl = WorkerImplementer()
        assert "claude-worker:8767" in impl._resolve_endpoint("implement")
        assert "deile-worker:8766" in impl._resolve_endpoint("pr_review")

    def test_default_when_no_env(self):
        """Sem env vars, default = deile-worker:8766."""
        impl = WorkerImplementer()
        url = impl._resolve_endpoint("implement")
        assert "deile-worker:8766" in url

    def test_endpoint_url_env_override(self, monkeypatch):
        """``DEILE_WORKER_ENDPOINT`` sobrescreve URL default (dev local)."""
        monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "http://localhost:7777")
        impl = WorkerImplementer()
        assert impl._resolve_endpoint("implement") == "http://localhost:7777"


class TestPostDispatchRouting:
    """Confirma que cada método (implement/review/mention) chama
    ``_post_dispatch`` com a URL resolvida pela stage correta. Patcha
    o seam para isolar do client real."""

    async def test_implement_uses_implement_stage(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
        impl = WorkerImplementer()
        with patch.object(impl, "_post_dispatch", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"ok": True, "summary": ""}
            await impl.implement(_make_monitor(), _issue())
            called_url = mock_post.call_args[0][0]
            assert "claude-worker:8767" in called_url

    async def test_review_uses_pr_review_stage(self, monkeypatch):
        """review() deve resolver pelo stage 'pr_review', não 'implement'."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_PR_REVIEW", "deile-worker")
        impl = WorkerImplementer()
        with patch.object(impl, "_post_dispatch", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"ok": True, "summary": ""}
            await impl.review(_make_monitor(), _pr())
            called_url = mock_post.call_args[0][0]
            assert "deile-worker:8766" in called_url

    async def test_endpoint_override_wins(self, monkeypatch):
        """``endpoint_override`` na construção é absoluto — ignora env."""
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
        impl = WorkerImplementer(endpoint_override="http://forced:9999")
        with patch.object(impl, "_post_dispatch", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"ok": True, "summary": ""}
            await impl.implement(_make_monitor(), _issue())
            called_url = mock_post.call_args[0][0]
            assert called_url == "http://forced:9999"

    async def test_default_endpoint_when_no_env(self):
        """Sem env vars nem override, default = deile-worker:8766."""
        impl = WorkerImplementer()
        with patch.object(impl, "_post_dispatch", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"ok": True, "summary": ""}
            await impl.implement(_make_monitor(), _issue())
            called_url = mock_post.call_args[0][0]
            assert "deile-worker:8766" in called_url
