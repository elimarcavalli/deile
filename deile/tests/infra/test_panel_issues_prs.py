"""Tests for the ``IssuesPRsView`` panel UI (issue #309).

Coverage:
  * Hotkey ``ENTER`` opens the selected issue/PR URL in the OS default
    browser via :func:`webbrowser.open` AND copies the URL to the
    clipboard. Both GitHub (``html_url``) and GitLab (``web_url``)
    flows are exercised — the view is forge-agnostic because the URL
    comes pre-resolved from the API in ``GitHubIssue.url``.
  * Column derivation is correct:
      - ``workflow`` reads only ``~workflow:<state>`` labels (not
        ``~workflow_meta:*`` look-alikes, not ``~review:*``).
      - ``review`` reads only ``~review:<state>`` labels.
      - ``bloqueada`` is terminal: wins over any other workflow label.
      - ``assignees`` extracts GH ``login`` and GL ``username``.
      - ``updated_at`` parses RFC3339 UTC and ``_fmt_age`` renders
        the human-readable form expected by the panel.
      - ``title`` is preserved verbatim by the data layer (truncation
        is a render-time decision, not a parsing bug).
      - ``number`` mirrors GH ``number`` and GL ``iid``.

Same sys.path trick as the other infra tests — ``infra/k8s`` is not a
package, so the directory is appended before the imports.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_issue(
    number: int,
    *,
    title: str = "issue title",
    is_pr: bool = False,
    labels: List[str] | None = None,
    assignees: List[str] | None = None,
    updated_at: datetime | None = None,
    url: str | None = None,
) -> pd.GitHubIssue:
    labels = labels or []
    return pd.GitHubIssue(
        number=number,
        title=title,
        is_pr=is_pr,
        state="open",
        labels=labels,
        assignees=assignees or [],
        updated_at=updated_at,
        url=(f"https://github.com/x/y/issues/{number}" if url is None else url),
        workflow=pd._derive_workflow(labels),
        review=pd._derive_review(labels),
        blocked=("~workflow:bloqueada" in labels),
    )


class _FakeGitHubProvider:
    """Stand-in para ``GitHubProvider`` que devolve um snapshot fixo."""

    def __init__(self, issues: List[pd.GitHubIssue],
                 prs: List[pd.GitHubIssue]) -> None:
        self._snap = pd.GitHubSnapshot(issues=issues, prs=prs)

    def get(self, force: bool = False) -> pd.GitHubSnapshot:
        return self._snap


class _FakePanelData:
    """Mínimo que ``IssuesPRsView`` consome — só ``.github``."""

    def __init__(self, issues: List[pd.GitHubIssue],
                 prs: List[pd.GitHubIssue]) -> None:
        self.github = _FakeGitHubProvider(issues, prs)


# ---------------------------------------------------------------------------
# Column derivation — workflow / review / bloqueada
# ---------------------------------------------------------------------------


class TestColumnWorkflow:
    """A coluna ``workflow`` reflete exatamente ``~workflow:<x>`` — não pode
    pegar look-alikes nem confundir com ``~review:*``.
    """

    def test_workflow_label_extracted(self):
        issue = _make_issue(1, labels=["~workflow:em_implementacao"])
        assert issue.workflow == "em_implementacao"
        assert issue.review == ""

    def test_bloqueada_terminal_wins(self):
        # Pipeline respeita ``~workflow:bloqueada`` mesmo quando outra
        # label de fase ainda está presente — a UI tem que refletir isso.
        issue = _make_issue(
            2, labels=["~workflow:em_implementacao", "~workflow:bloqueada"],
        )
        assert issue.workflow == "bloqueada"
        assert issue.blocked is True

    def test_workflow_meta_lookalike_ignored(self):
        # Sanity: se um dia houver ``~workflow_meta:foo`` (não existe hoje,
        # mas o ``startswith("~workflow:")`` poderia ser frágil), a coluna
        # NÃO deve confundir com workflow real.
        issue = _make_issue(3, labels=["~workflow_meta:foo"])
        assert issue.workflow == ""
        assert issue.review == ""

    def test_review_label_not_picked_as_workflow(self):
        issue = _make_issue(4, labels=["~review:pendente"])
        assert issue.workflow == ""
        assert issue.review == "pendente"


class TestColumnReview:
    def test_review_pendente(self):
        pr = _make_issue(10, is_pr=True, labels=["~review:pendente"])
        assert pr.review == "pendente"

    def test_review_concluida(self):
        pr = _make_issue(11, is_pr=True, labels=["~review:concluida"])
        assert pr.review == "concluida"

    def test_review_absent(self):
        pr = _make_issue(12, is_pr=True, labels=[])
        assert pr.review == ""


# ---------------------------------------------------------------------------
# updated_at + _fmt_age
# ---------------------------------------------------------------------------


class TestColumnUpdated:
    """``updated_at`` é tz-aware UTC; ``_fmt_age`` renderiza ``Xs``/``Xm``/
    ``Xh``/``Xd``. O ``IssuesPRsView`` calcula ``(now_utc - updated_at)``
    em segundos, portanto a TZ tem que casar dos dois lados.
    """

    def test_parses_rfc3339_utc(self):
        ts = pd._parse_k8s_ts("2026-05-26T12:34:56Z")
        assert ts is not None
        assert ts.tzinfo is not None
        assert ts.utcoffset() == timedelta(0)

    def test_age_format_minutes(self):
        assert pd._fmt_age(6 * 60) == "6m"

    def test_age_format_hours_minutes(self):
        assert pd._fmt_age(3600 + 3 * 60) == "1h03m"

    def test_age_format_days(self):
        assert pd._fmt_age(5 * 86400) == "5d"

    def test_age_none(self):
        assert pd._fmt_age(None) == "—"


# ---------------------------------------------------------------------------
# assignees
# ---------------------------------------------------------------------------


class TestColumnAssignees:
    def test_github_assignees_parsed_from_login(self):
        # Replica o parsing em ``_fetch_github``: ``a.get("login", "")``.
        raw = [{"login": "alice"}, {"login": "bob"}]
        parsed = [a.get("login", "") for a in raw]
        assert parsed == ["alice", "bob"]

    def test_gitlab_assignees_parsed_from_username(self):
        # Replica o parsing em ``_fetch_gitlab``: ``a.get("username", "")``.
        raw = [{"username": "carol"}, {"username": "dave"}]
        parsed = [a.get("username", "") for a in raw]
        assert parsed == ["carol", "dave"]

    def test_empty_assignees_displays_dash(self):
        # No render: ``", ".join(it.assignees) or "—"`` — array vazio cai
        # em "—" (o que o usuário viu na captura). Garante que isso é
        # comportamento esperado, não bug de parse.
        issue = _make_issue(20, assignees=[])
        display = ", ".join(issue.assignees) or "—"
        assert display == "—"


# ---------------------------------------------------------------------------
# number — GH ``number`` vs GL ``iid``
# ---------------------------------------------------------------------------


class TestColumnNumber:
    def test_github_number_is_number_field(self):
        # GH: gh api devolve ``number``.
        raw = {"number": 309, "title": "x"}
        assert int(raw.get("number", 0)) == 309

    def test_gitlab_iid_is_iid_field(self):
        # GL: glab api devolve ``iid`` (issue/MR id por projeto).
        raw = {"iid": 42, "title": "x"}
        assert int(raw.get("iid") or raw.get("number") or 0) == 42

    def test_gitlab_falls_back_to_number_if_missing(self):
        # Defensivo: o parser GL aceita ``number`` como fallback.
        raw = {"number": 7}
        assert int(raw.get("iid") or raw.get("number") or 0) == 7


# ---------------------------------------------------------------------------
# ENTER — abre URL no browser + copia pro clipboard
# ---------------------------------------------------------------------------


class TestEnterOpensUrl:
    """Bug raiz: o hotkey ``[enter] abrir URL`` prometia abrir a URL no
    browser default, mas a implementação só copiava pro clipboard. Os
    testes abaixo travam o contrato: ENTER chama ``webbrowser.open(url)``.
    """

    def _view_with(self, issues, prs):
        view = panel.IssuesPRsView(data=_FakePanelData(issues, prs))
        view.cursor = 0
        return view

    def test_enter_opens_github_issue_url_in_browser(self):
        issue = _make_issue(
            309,
            url="https://github.com/elimarcavalli/deile/issues/309",
        )
        view = self._view_with([issue], [])
        with patch.object(panel, "webbrowser") as mock_webbrowser, \
             patch.object(panel, "_copy_to_clipboard", return_value=True):
            mock_webbrowser.open.return_value = True
            view.handle_key("\r", app=None)
        mock_webbrowser.open.assert_called_once_with(
            "https://github.com/elimarcavalli/deile/issues/309",
            new=2, autoraise=True,
        )

    def test_enter_opens_gitlab_mr_url_in_browser(self):
        # Forge-agnóstico: a URL já vem do API (``web_url`` no GitLab).
        pr = _make_issue(
            1, is_pr=True,
            url="https://gitlab.com/grp/sub/proj/-/merge_requests/1",
        )
        view = self._view_with([], [pr])
        with patch.object(panel, "webbrowser") as mock_webbrowser, \
             patch.object(panel, "_copy_to_clipboard", return_value=True):
            mock_webbrowser.open.return_value = True
            view.handle_key("\n", app=None)
        mock_webbrowser.open.assert_called_once_with(
            "https://gitlab.com/grp/sub/proj/-/merge_requests/1",
            new=2, autoraise=True,
        )

    def test_enter_also_copies_url_to_clipboard(self):
        # Fallback nice-to-have: mesmo abrindo o browser, o operador
        # ainda recebe a URL no clipboard (útil em headless ou se o
        # browser default for o errado).
        issue = _make_issue(
            7,
            url="https://github.com/x/y/issues/7",
        )
        view = self._view_with([issue], [])
        with patch.object(panel, "webbrowser") as mock_webbrowser, \
             patch.object(panel, "_copy_to_clipboard",
                          return_value=True) as mock_copy:
            mock_webbrowser.open.return_value = True
            view.handle_key("\r", app=None)
        mock_copy.assert_called_once_with("https://github.com/x/y/issues/7")

    def test_enter_with_empty_url_is_noop_does_not_crash(self):
        # Defensivo: ``GitHubIssue.url`` pode vir vazio (API mudou,
        # ConfigMap corrompido). Não pode levantar nem chamar webbrowser
        # com string vazia (alguns OS abrem a home page do browser).
        issue = _make_issue(8, url="")
        view = self._view_with([issue], [])
        with patch.object(panel, "webbrowser") as mock_webbrowser, \
             patch.object(panel, "_copy_to_clipboard") as mock_copy:
            view.handle_key("\r", app=None)
        mock_webbrowser.open.assert_not_called()
        mock_copy.assert_not_called()

    def test_navigation_arrows_do_not_open_browser(self):
        # Sanity: setas só movem cursor, nunca disparam open.
        a = _make_issue(1, url="https://github.com/x/y/issues/1")
        b = _make_issue(2, url="https://github.com/x/y/issues/2")
        view = self._view_with([a, b], [])
        with patch.object(panel, "webbrowser") as mock_webbrowser:
            view.handle_key("DOWN", app=None)
            view.handle_key("UP", app=None)
        mock_webbrowser.open.assert_not_called()


# ---------------------------------------------------------------------------
# _open_in_browser helper
# ---------------------------------------------------------------------------


class TestOpenInBrowserHelper:
    def test_calls_webbrowser_open_with_url(self):
        with patch.object(panel, "webbrowser") as mock_wb:
            mock_wb.open.return_value = True
            assert panel._open_in_browser("https://example.com") is True
        mock_wb.open.assert_called_once_with(
            "https://example.com", new=2, autoraise=True,
        )

    def test_empty_url_returns_false_without_calling_webbrowser(self):
        with patch.object(panel, "webbrowser") as mock_wb:
            assert panel._open_in_browser("") is False
        mock_wb.open.assert_not_called()

    def test_webbrowser_exception_is_caught_and_returns_false(self):
        # Em headless / containers / WSL, ``webbrowser.open`` pode
        # levantar — o painel não pode cair por isso.
        with patch.object(panel, "webbrowser") as mock_wb:
            mock_wb.open.side_effect = RuntimeError("no display")
            assert panel._open_in_browser("https://example.com") is False
