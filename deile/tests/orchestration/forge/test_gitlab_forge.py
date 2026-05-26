"""Tests for :class:`deile.orchestration.forge.gitlab_forge.GitLabForge`.

Adapter behaviour is exercised against a fake ``glab`` subprocess: every
call to :meth:`GitLabForge._run` / :meth:`_run_checked` is intercepted via
``monkeypatch`` so no real binary is ever invoked. The fixtures expose
helpers to script "next N responses" so each test reads as a small story
("given these responses, expect this call sequence").
"""

from __future__ import annotations

import json
from collections import deque
from typing import Tuple

import pytest

from deile.orchestration.forge import GitLabForge
from deile.orchestration.forge.base import (ForgeConfig, ForgeKind,
                                            MergeBlocked,
                                            MergeBlockedByPipeline)


@pytest.fixture
def fake_glab(monkeypatch):
    """Returns ``(forge, responses)``. Append (rc, out, err) tuples to
    ``responses`` in the order the test expects them to be consumed.
    Each ``_run`` call pops the leftmost response.

    ``forge`` is built with a known ``ForgeConfig`` so no real ``glab``
    binary is needed.
    """
    responses: "deque[Tuple[int, str, str]]" = deque()
    calls: list[tuple] = []

    cfg = ForgeConfig(
        kind=ForgeKind.GITLAB,
        host="gitlab.com",
        project_path="group/project",
        cli_path="/usr/bin/glab",
    )
    forge = GitLabForge(cfg)

    async def fake_run(self, *args):
        calls.append(args)
        if not responses:
            return (0, "[]", "")
        return responses.popleft()

    monkeypatch.setattr(GitLabForge, "_run", fake_run)
    return forge, responses, calls


async def test_get_issue_uses_iid_endpoint(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, json.dumps({
        "iid": 7, "title": "x", "web_url": "u", "labels": [],
        "description": "d", "state": "opened", "author": {"username": "alice"},
    }), ""))
    issue = await forge.get_issue(7)
    assert issue.number == 7
    assert calls[0] == ("api", "projects/group%2Fproject/issues/7")


async def test_get_pr_filters_out_closed_mrs(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({
        "iid": 5, "title": "x", "web_url": "u", "labels": [],
        "source_branch": "b", "target_branch": "main", "state": "closed",
    }), ""))
    assert await forge.get_pr(5) is None


async def test_get_pr_open_returns_mr(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({
        "iid": 5, "title": "x", "web_url": "u", "labels": [],
        "source_branch": "b", "target_branch": "main", "state": "opened",
    }), ""))
    pr = await forge.get_pr(5)
    assert pr is not None
    assert pr.state == "open"
    assert pr.head_ref == "b"


async def test_list_open_prs_paginated(fake_glab):
    forge, responses, _ = fake_glab
    # First page: 100 items (full page) → triggers pagination.
    full_page = [
        {"iid": i, "title": f"mr{i}", "web_url": "u", "labels": [],
         "source_branch": "b", "target_branch": "main", "state": "opened"}
        for i in range(100)
    ]
    short_page = [
        {"iid": 100, "title": "mr100", "web_url": "u", "labels": [],
         "source_branch": "b", "target_branch": "main", "state": "opened"},
    ]
    responses.append((0, json.dumps(full_page), ""))
    responses.append((0, json.dumps(short_page), ""))
    prs = await forge.list_open_prs(limit=150)
    assert len(prs) == 101


async def test_add_labels_uses_put_endpoint(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, "{}", ""))
    await forge.add_labels("issue", 42, ["bug", "~workflow:nova"])
    assert calls[0][:3] == ("api", "-X", "PUT")
    assert calls[0][3] == "projects/group%2Fproject/issues/42"
    # GitLab takes comma-separated label names.
    assert "add_labels=bug,~workflow:nova" in calls[0]


async def test_remove_labels_uses_put(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, "{}", ""))
    await forge.remove_labels("pr", 1, ["foo"])
    assert calls[0][3] == "projects/group%2Fproject/merge_requests/1"
    assert "remove_labels=foo" in calls[0]


async def test_remove_labels_404_is_idempotent(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((1, "", "404 Not Found"))
    # Must not raise.
    await forge.remove_labels("issue", 42, ["nope"])


async def test_assign_issue_resolves_username_to_user_id(fake_glab):
    """Verifica o flow end-to-end e o fix do HTTP 400 (assignee_ids[] em query string).

    Bug empírico descoberto: ``glab api -X PUT ... -f assignee_ids[]=N`` retorna
    HTTP 400 ("assignee_ids ... are missing") porque o GitLab REST PUT espera o
    array na query string, não no body form-encoded. O fix encoda ``[]`` como
    ``%5B%5D`` na URL diretamente.
    """
    forge, responses, calls = fake_glab
    # 1st: user lookup. 2nd: PUT com assignee_ids[] na query string.
    responses.append((0, json.dumps([{"id": 123, "username": "alice"}]), ""))
    responses.append((0, "{}", ""))
    await forge.assign_issue(42, "alice")
    assert "username=alice" in calls[0]
    # PUT call: URL deve carregar ``assignee_ids%5B%5D=123`` na query string —
    # NÃO ``-f assignee_ids[]=123`` (que o GitLab REST rejeita com HTTP 400).
    put_call = calls[1]
    assert put_call[:3] == ("api", "-X", "PUT")
    url_segment = put_call[3]
    assert "assignee_ids%5B%5D=123" in url_segment, (
        f"esperado 'assignee_ids%5B%5D=123' na URL, got {url_segment!r}"
    )
    # Garante que NÃO usamos -f para esse campo (esse é o bug que estamos prevenindo).
    assert "-f" not in put_call, "PUT assignee_ids deve usar query string, não -f"


async def test_assign_issue_logs_replace_at_debug(fake_glab):
    """assign_issue documenta a semântica REPLACE em log DEBUG (não WARNING).

    Pre-PR-review esta mensagem era ``logger.warning(...)`` em toda chamada,
    gerando ruído sob auto-routing. Rebaixada a ``logger.debug`` (a contratação
    REPLACE continua documentada no docstring + CLAUDE.md). O teste passa a
    capturar em DEBUG para garantir que o sinal não sumiu por completo.
    """
    import logging

    forge, responses, _ = fake_glab
    responses.append((0, json.dumps([{"id": 99, "username": "bob"}]), ""))
    responses.append((0, "{}", ""))

    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    _logger = logging.getLogger("deile.orchestration.forge.gitlab_forge")
    handler = _Capture(level=logging.DEBUG)
    _logger.addHandler(handler)
    original_level = _logger.level
    _logger.setLevel(logging.DEBUG)
    # Restaura o estado global do logging caso outro teste tenha chamado
    # logging.disable() e esquecido de reverter (padrão conhecido na suíte).
    previous_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    try:
        await forge.assign_issue(10, "bob")
    finally:
        _logger.removeHandler(handler)
        _logger.setLevel(original_level)
        logging.disable(previous_disable)

    assert any("REPLACE" in msg for msg in captured)


async def test_assign_issue_handles_missing_user_gracefully(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, "[]", ""))  # user not found
    # Must not raise — courtesy signal.
    await forge.assign_issue(42, "ghost")


async def test_merge_pr_blocked_by_unmergeable_status(fake_glab):
    """detailed_merge_status=conflict deve levantar MergeBlocked."""
    forge, responses, _ = fake_glab
    # Payload com detailed_merge_status preenchido (GitLab >= 15.6).
    responses.append((0, json.dumps({
        "iid": 5,
        "detailed_merge_status": "conflict",
        "merge_status": "cannot_be_merged",  # legado presente mas não deve ser lido
    }), ""))
    with pytest.raises(MergeBlocked) as exc_info:
        await forge.merge_pr(5)
    assert "detailed_merge_status=conflict" in str(exc_info.value)


async def test_merge_pr_blocked_fallback_to_merge_status(fake_glab):
    """Instâncias GitLab antigas sem detailed_merge_status usam merge_status como fallback."""
    forge, responses, _ = fake_glab
    # Sem detailed_merge_status — somente campo legado.
    responses.append((0, json.dumps({
        "iid": 5,
        "merge_status": "cannot_be_merged",
    }), ""))
    with pytest.raises(MergeBlocked) as exc_info:
        await forge.merge_pr(5)
    assert "merge_status=cannot_be_merged" in str(exc_info.value)


async def test_merge_pr_unchecked_does_NOT_block(fake_glab):
    """detailed_merge_status=unchecked é neutro — não bloqueia no pre-check."""
    forge, responses, _ = fake_glab
    # Pre-check: unchecked (GitLab ainda está computando).
    responses.append((0, json.dumps({
        "iid": 5,
        "detailed_merge_status": "unchecked",
    }), ""))
    # Merge PUT: sucesso.
    responses.append((0, "{}", ""))
    # Não deve levantar MergeBlocked.
    await forge.merge_pr(5)


async def test_merge_pr_unchecked_legacy_does_NOT_block(fake_glab):
    """merge_status=unchecked no campo legado também não bloqueia."""
    forge, responses, _ = fake_glab
    # Sem detailed_merge_status; merge_status=unchecked no campo legado.
    responses.append((0, json.dumps({
        "iid": 5,
        "merge_status": "unchecked",
    }), ""))
    # Merge PUT: sucesso.
    responses.append((0, "{}", ""))
    await forge.merge_pr(5)


@pytest.mark.parametrize("dms,exc_type,fragment", [
    ("conflict",                   MergeBlocked,           "conflict"),
    ("not_approved",               MergeBlocked,           "not_approved"),
    ("requested_changes",          MergeBlocked,           "requested_changes"),
    ("discussions_not_resolved",   MergeBlocked,           "discussions_not_resolved"),
    ("need_rebase",                MergeBlocked,           "need_rebase"),
    ("not_open",                   MergeBlocked,           "not_open"),
    ("cannot_be_merged",           MergeBlocked,           "cannot_be_merged"),
    # ci_must_pass levanta MergeBlockedByPipeline — requer respostas extras para get_ci_status
    # (testado em test_merge_pr_blocked_by_pipeline_succeed_rule acima)
    # Valores neutros — não devem levantar (testados individualmente):
    # unchecked, checking, mergeable, preparing, approvals_syncing
])
async def test_merge_pr_detailed_status_variants(fake_glab, dms, exc_type, fragment):
    """Cada detailed_merge_status bloqueante leva ao tipo de exceção correto."""
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({
        "iid": 5,
        "detailed_merge_status": dms,
    }), ""))
    with pytest.raises(exc_type) as exc_info:
        await forge.merge_pr(5)
    assert fragment in str(exc_info.value)


@pytest.mark.parametrize("dms", ["unchecked", "checking", "mergeable", "preparing", "approvals_syncing"])
async def test_merge_pr_neutral_detailed_status_does_not_block(fake_glab, dms):
    """Valores neutros de detailed_merge_status não devem levantar no pre-check."""
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({
        "iid": 5,
        "detailed_merge_status": dms,
    }), ""))
    # PUT merge bem-sucedido.
    responses.append((0, "{}", ""))
    await forge.merge_pr(5)


async def test_merge_pr_blocked_by_pipeline_succeed_rule(fake_glab):
    forge, responses, _ = fake_glab
    # 1) precheck: OK.
    responses.append((0, json.dumps({"iid": 5, "merge_status": "can_be_merged"}), ""))
    # 2) actual merge: 405 with pipeline-related message.
    responses.append((
        1, "", "405 Method Not Allowed: Pipeline must succeed.",
    ))
    # 3) get_ci_status: MR with pipeline id.
    responses.append((0, json.dumps({"iid": 5, "head_pipeline": {"id": 99}}), ""))
    # 4) get_ci_status: pipeline running.
    responses.append((0, json.dumps({"status": "running"}), ""))
    with pytest.raises(MergeBlockedByPipeline) as exc_info:
        await forge.merge_pr(5)
    assert "pending" in str(exc_info.value).lower() or "running" in str(exc_info.value).lower()


async def test_merge_pr_success(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, json.dumps({"iid": 5, "merge_status": "can_be_merged"}), ""))
    responses.append((0, "{}", ""))
    await forge.merge_pr(5)
    # 1st call = precheck GET MR; 2nd call = PUT /merge.
    assert calls[1][:3] == ("api", "-X", "PUT")
    assert "merge_requests/5/merge" in calls[1][3]


async def test_get_ci_status_returns_none_when_no_pipeline(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({"iid": 5, "head_pipeline": None}), ""))
    assert await forge.get_ci_status(5) == "none"


@pytest.mark.parametrize("status, expected", [
    ("success", "passing"),
    ("failed", "failing"),
    ("canceled", "failing"),
    ("running", "pending"),
    ("pending", "pending"),
    ("manual", "pending"),
    ("skipped", "none"),
])
async def test_get_ci_status_normalises_gitlab_statuses(fake_glab, status, expected):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({"iid": 5, "head_pipeline": {"id": 1}}), ""))
    responses.append((0, json.dumps({"status": status}), ""))
    assert await forge.get_ci_status(5) == expected


async def test_resolve_project_id_caches_value(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, json.dumps({"id": 9876, "default_branch": "trunk"}), ""))
    pid = await forge._resolve_project_id()
    assert pid == "9876"
    assert forge.config.project_id == "9876"
    # default_branch is captured as a side effect.
    assert forge.config.default_branch == "trunk"
    # Second call must NOT hit the API.
    pid2 = await forge._resolve_project_id()
    assert pid2 == "9876"
    assert len(calls) == 1


async def test_ensure_label_normalises_color_with_hash(fake_glab):
    """GitLab labels colors MUST be prefixed with '#' — adapter normalises."""
    forge, responses, calls = fake_glab
    responses.append((0, "{}", ""))
    await forge._ensure_label("~workflow:nova", color="0e8a16", description="x")
    flat = " ".join(calls[0])
    assert "color=#0e8a16" in flat


async def test_pr_reviewer_still_requested_reads_reviewers_array(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({
        "iid": 5, "reviewers": [{"username": "alice"}, {"username": "deile-one"}],
    }), ""))
    assert await forge.pr_reviewer_still_requested(5, "deile-one") is True


async def test_pr_reviewer_still_requested_fails_open(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((1, "", "boom"))  # API error
    # Must return False — never block work on a transient hiccup.
    assert await forge.pr_reviewer_still_requested(5, "deile-one") is False


async def test_invalid_project_path_raises():
    from deile.orchestration.forge.base import ForgeConfigError

    # A 1-segment path is not valid for GitLab (needs at least 2).
    with pytest.raises(ForgeConfigError):
        ForgeConfig(
            kind=ForgeKind.GITLAB,
            host="gitlab.com",
            project_path="single",
            cli_path="/usr/bin/glab",
        )


async def test_path_traversal_rejected():
    from deile.orchestration.forge.base import ForgeConfigError
    with pytest.raises(ForgeConfigError):
        ForgeConfig(
            kind=ForgeKind.GITLAB,
            host="gitlab.com",
            project_path="group/../etc",
            cli_path="/usr/bin/glab",
        )


async def test_comment_on_issue_uses_raw_field(fake_glab):
    """comment_on_issue deve usar --raw-field (não -f) para o corpo do comentário."""
    forge, responses, calls = fake_glab
    responses.append((0, "{}", ""))
    await forge.comment_on_issue(7, "texto: null, true, :repo, $HOME")
    flat_call = calls[0]
    # --raw-field deve estar presente
    assert "--raw-field" in flat_call
    # -f NÃO deve aparecer para o body (poderia estar em outros params, mas body é raw)
    # Verifica que o par "--raw-field", "body=..." está na sequência
    idx = list(flat_call).index("--raw-field")
    assert flat_call[idx + 1].startswith("body=")
    # Garante que -f NÃO precede o body
    assert "-f" not in flat_call[:idx]


async def test_comment_on_pr_uses_raw_field(fake_glab):
    """comment_on_pr deve usar --raw-field para evitar magic type conversion."""
    forge, responses, calls = fake_glab
    responses.append((0, "{}", ""))
    await forge.comment_on_pr(3, "resposta: null or :branch override?")
    flat_call = calls[0]
    assert "--raw-field" in flat_call
    idx = list(flat_call).index("--raw-field")
    assert flat_call[idx + 1].startswith("body=")


async def test_event_to_comment_issue_uses_issues_url(fake_glab):
    """_event_to_comment com kind=issue deve usar /-/issues/<iid> na URL."""
    forge, _, _ = fake_glab
    note = {"id": 10, "body": "msg", "noteable_iid": 5, "noteable_url": None}
    event = {"author": {"username": "alice"}}
    ref = forge._event_to_comment(event, note, kind="issue")
    assert "/-/issues/5" in ref.issue_url
    assert "/-/merge_requests/" not in ref.issue_url


async def test_event_to_comment_pr_review_uses_merge_requests_url(fake_glab):
    """_event_to_comment com kind=pr_review deve usar /-/merge_requests/<iid> na URL."""
    forge, _, _ = fake_glab
    note = {"id": 20, "body": "review", "noteable_iid": 8, "noteable_url": None}
    event = {"author": {"username": "bob"}}
    ref = forge._event_to_comment(event, note, kind="pr_review")
    assert "/-/merge_requests/8" in ref.issue_url
    assert "/-/issues/" not in ref.issue_url


async def test_event_to_comment_prefers_noteable_url(fake_glab):
    """Quando noteable_url está presente, deve ser usado sem reconstrução."""
    forge, _, _ = fake_glab
    canonical = "https://gitlab.example.com/grp/prj/-/issues/42"
    note = {
        "id": 30, "body": "x",
        "noteable_iid": 42, "noteable_url": canonical,
    }
    event = {"author": {"username": "carol"}}
    ref = forge._event_to_comment(event, note, kind="issue")
    assert ref.issue_url == canonical


# ---------------------------------------------------------------------------
# Regressões de bugs descobertos no E2E real contra gitlab.com (2026-05-26)
# ---------------------------------------------------------------------------


async def test_api_get_json_forces_X_GET_when_params_present(fake_glab):
    """Regressão: ``glab api`` muda para POST por default quando há ``-f``.

    Bug observado contra gitlab.com real:
    ``glab api projects/.../issues -f state=opened`` virou POST → HTTP 400
    ("title is missing") porque GitLab tratou como criação de issue.
    O fix adiciona ``-X GET`` explícito sempre que há parâmetros.
    """
    forge, responses, calls = fake_glab
    responses.append((0, "[]", ""))
    # Chamada com parâmetros deve carregar -X GET antes do endpoint.
    await forge._api_get_json("projects/1/issues", "-f", "state=opened")
    call = calls[0]
    # Sequência esperada: ("api", "-X", "GET", "projects/1/issues", "-f", "state=opened")
    assert call[0] == "api"
    assert call[1:3] == ("-X", "GET"), (
        f"esperado ('-X', 'GET') antes do endpoint, got {call[1:3]}"
    )
    assert call[3] == "projects/1/issues"


async def test_api_get_json_no_method_flag_when_no_params(fake_glab):
    """Sem parâmetros, ``glab api <endpoint>`` é GET implícito — não força ``-X GET``."""
    forge, responses, calls = fake_glab
    responses.append((0, "{}", ""))
    await forge._api_get_json("projects/1")
    call = calls[0]
    assert call == ("api", "projects/1"), (
        f"sem params não deve injetar -X GET, got {call}"
    )


async def test_list_open_prs_uses_X_GET(fake_glab):
    """Verifica que list_open_prs (via _api_paginated) carrega -X GET no glab call."""
    forge, responses, calls = fake_glab
    responses.append((0, "[]", ""))
    await forge.list_open_prs(limit=10)
    call = calls[0]
    assert call[1:3] == ("-X", "GET"), (
        f"list_open_prs deve usar -X GET, got {call[1:3]}"
    )


async def test_create_issue_parses_work_items_url(fake_glab):
    """Regressão: GitLab >= 17 retorna URL ``/-/work_items/<iid>`` no output do create.

    Bug observado: ``glab issue create`` agora retorna
    ``https://gitlab.com/owner/repo/-/work_items/2`` em vez do antigo
    ``/-/issues/2``. O regex deve tolerar ambos os formatos.
    """
    forge, responses, _ = fake_glab
    # Output novo do glab (work_items).
    work_items_out = (
        "- Creating issue in elimarcavalli/test\n"
        "https://gitlab.com/elimarcavalli/test/-/work_items/42\n"
    )
    responses.append((0, work_items_out, ""))
    iid = await forge.create_issue("test", "body")
    assert iid == 42, f"deveria parsear iid=42 do path /-/work_items/, got {iid}"


async def test_create_issue_still_parses_legacy_issues_url(fake_glab):
    """Output legado (``/-/issues/N``) continua sendo parseado corretamente."""
    forge, responses, _ = fake_glab
    legacy_out = (
        "- Creating issue in owner/repo\n"
        "https://gitlab.com/owner/repo/-/issues/17\n"
    )
    responses.append((0, legacy_out, ""))
    iid = await forge.create_issue("title", "body")
    assert iid == 17
