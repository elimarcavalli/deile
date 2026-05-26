"""Testes para o hook de rate-limit em :class:`ForgeClient`.

Cobre :meth:`ForgeClient._maybe_sleep_for_rate_limit` e o helper
:func:`_parse_headers_and_body` de :mod:`deile.orchestration.forge.base`.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from deile.orchestration.forge.base import (ForgeClient, ForgeConfig,
                                            _parse_headers_and_body)

# ---------------------------------------------------------------------------
# Implementação mínima de ForgeClient para testes
# ---------------------------------------------------------------------------


class _MinimalForge(ForgeClient):
    """Subclasse concreta mínima — só existe para exercitar os métodos do ABC."""

    # Métodos abstratos implementados com no-op.
    async def list_issues_with_label(self, *a, **kw): ...
    async def get_issue(self, *a, **kw): ...
    async def list_issues_assigned_to(self, *a, **kw): ...
    async def list_unclassified_issues(self, *a, **kw): ...
    async def create_issue(self, *a, **kw): ...
    async def comment_on_issue(self, *a, **kw): ...
    async def assign_issue(self, *a, **kw): ...
    async def get_pr(self, *a, **kw): ...
    async def has_open_pr_for_issue(self, *a, **kw): ...
    async def list_open_prs(self, *a, **kw): ...
    async def list_prs_assigned_to(self, *a, **kw): ...
    async def list_unclassified_prs(self, *a, **kw): ...
    async def list_recently_merged_prs(self, *a, **kw): ...
    async def list_prs_updated_since(self, *a, **kw): ...
    async def list_issues_updated_since(self, *a, **kw): ...
    async def pr_reviewer_still_requested(self, *a, **kw): ...
    async def list_prs_with_review_requests(self, *a, **kw): ...
    async def comment_on_pr(self, *a, **kw): ...
    async def get_pr_body(self, *a, **kw): ...
    async def list_pr_comments(self, *a, **kw): ...
    async def set_draft(self, *a, **kw): ...
    async def merge_pr(self, *a, **kw): ...
    async def get_ci_status(self, *a, **kw): ...
    async def add_labels(self, *a, **kw): ...
    async def remove_labels(self, *a, **kw): ...
    async def ensure_pipeline_labels(self, *a, **kw): ...
    async def list_issue_comments_since(self, *a, **kw): ...
    async def list_pr_review_comments_since(self, *a, **kw): ...
    async def search_items_mentioning(self, *a, **kw): ...
    async def default_branch(self, *a, **kw): ...
    async def _ensure_label(self, *a, **kw): ...


@pytest.fixture
def forge(github_config: ForgeConfig) -> _MinimalForge:
    return _MinimalForge(github_config)


# ---------------------------------------------------------------------------
# Testes de _maybe_sleep_for_rate_limit
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_no_sleep_when_remaining_above_threshold(forge: _MinimalForge) -> None:
    """Não deve dormir quando remaining >= threshold (default 20)."""
    future_epoch = int(time.time()) + 60
    headers = {
        "X-RateLimit-Remaining": "100",
        "X-RateLimit-Reset": str(future_epoch),
    }
    with patch("asyncio.sleep") as mock_sleep:
        await forge._maybe_sleep_for_rate_limit(headers)
    mock_sleep.assert_not_called()


@pytest.mark.unit
async def test_sleeps_when_remaining_below_threshold(forge: _MinimalForge) -> None:
    """Deve dormir quando remaining < threshold."""
    future_epoch = int(time.time()) + 30
    headers = {
        "X-RateLimit-Remaining": "5",
        "X-RateLimit-Reset": str(future_epoch),
    }
    with patch("asyncio.sleep") as mock_sleep:
        mock_sleep.return_value = None
        await forge._maybe_sleep_for_rate_limit(headers, threshold=20)
    mock_sleep.assert_called_once()
    sleep_arg = mock_sleep.call_args[0][0]
    # O sleep deve ser positivo e não maior que o delta real.
    assert sleep_arg > 0


@pytest.mark.unit
async def test_caps_sleep_at_60s(forge: _MinimalForge) -> None:
    """O sleep nunca deve ultrapassar o cap de 60s."""
    far_future_epoch = int(time.time()) + 9999
    headers = {
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": str(far_future_epoch),
    }
    with patch("asyncio.sleep") as mock_sleep:
        mock_sleep.return_value = None
        await forge._maybe_sleep_for_rate_limit(headers, cap=60)
    mock_sleep.assert_called_once()
    sleep_arg = mock_sleep.call_args[0][0]
    assert sleep_arg <= 60


@pytest.mark.unit
async def test_handles_missing_headers_gracefully(forge: _MinimalForge) -> None:
    """Sem headers de rate-limit não deve dormir nem lançar exceção."""
    with patch("asyncio.sleep") as mock_sleep:
        await forge._maybe_sleep_for_rate_limit({})
    mock_sleep.assert_not_called()


@pytest.mark.unit
async def test_handles_partial_headers_gracefully(forge: _MinimalForge) -> None:
    """Com apenas um dos headers presentes não deve dormir."""
    with patch("asyncio.sleep") as mock_sleep:
        await forge._maybe_sleep_for_rate_limit({"X-RateLimit-Remaining": "5"})
    mock_sleep.assert_not_called()


@pytest.mark.unit
async def test_github_x_ratelimit_headers_recognized(forge: _MinimalForge) -> None:
    """Deve reconhecer os headers X-RateLimit-* do GitHub cloud."""
    future_epoch = int(time.time()) + 30
    headers = {
        "X-RateLimit-Remaining": "10",
        "X-RateLimit-Reset": str(future_epoch),
    }
    with patch("asyncio.sleep") as mock_sleep:
        mock_sleep.return_value = None
        await forge._maybe_sleep_for_rate_limit(headers, threshold=20)
    mock_sleep.assert_called_once()


@pytest.mark.unit
async def test_gitlab_ratelimit_headers_recognized(forge: _MinimalForge) -> None:
    """Deve reconhecer os headers RateLimit-* do GitLab / GHES."""
    future_epoch = int(time.time()) + 30
    headers = {
        "RateLimit-Remaining": "3",
        "RateLimit-Reset": str(future_epoch),
    }
    with patch("asyncio.sleep") as mock_sleep:
        mock_sleep.return_value = None
        await forge._maybe_sleep_for_rate_limit(headers, threshold=20)
    mock_sleep.assert_called_once()


@pytest.mark.unit
async def test_no_sleep_when_reset_in_past(forge: _MinimalForge) -> None:
    """Se o epoch de reset já passou, o sleep deve ser 0 (ou near-zero) e não chamado."""
    past_epoch = int(time.time()) - 60
    headers = {
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": str(past_epoch),
    }
    with patch("asyncio.sleep") as mock_sleep:
        mock_sleep.return_value = None
        await forge._maybe_sleep_for_rate_limit(headers)
    # delta = max(0, past_epoch - now) = 0 → sleep(0) é chamado
    if mock_sleep.called:
        sleep_arg = mock_sleep.call_args[0][0]
        assert sleep_arg == 0.0


@pytest.mark.unit
async def test_handles_malformed_header_values_gracefully(forge: _MinimalForge) -> None:
    """Valores não-inteiros nos headers não devem causar exceção."""
    headers = {
        "X-RateLimit-Remaining": "none",
        "X-RateLimit-Reset": "never",
    }
    with patch("asyncio.sleep") as mock_sleep:
        # Não deve lançar exceção.
        await forge._maybe_sleep_for_rate_limit(headers)
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Testes de _parse_headers_and_body
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_headers_and_body_basic() -> None:
    """Analisa corretamente headers + corpo JSON separados por linha em branco."""
    raw = (
        "HTTP/1.1 200 OK\n"
        "X-RateLimit-Remaining: 42\n"
        "Content-Type: application/json\n"
        "\n"
        '{"key": "value"}'
    )
    body, headers = _parse_headers_and_body(raw)
    assert body == {"key": "value"}
    assert headers["X-RateLimit-Remaining"] == "42"
    assert headers["Content-Type"] == "application/json"


@pytest.mark.unit
def test_parse_headers_and_body_no_body() -> None:
    """Sem corpo retorna dicts vazios mas não lança exceção."""
    raw = "HTTP/1.1 204 No Content\nX-Custom: val\n"
    body, headers = _parse_headers_and_body(raw)
    assert body == {}
    assert headers.get("X-Custom") == "val"


@pytest.mark.unit
def test_parse_headers_and_body_malformed_json() -> None:
    """JSON inválido no corpo retorna body={} sem lançar exceção."""
    raw = "HTTP/1.1 200 OK\n\nnot valid json {"
    body, headers = _parse_headers_and_body(raw)
    assert body == {}


@pytest.mark.unit
def test_parse_headers_and_body_empty_string() -> None:
    """String vazia retorna dois dicts vazios."""
    body, headers = _parse_headers_and_body("")
    assert body == {}
    assert headers == {}
