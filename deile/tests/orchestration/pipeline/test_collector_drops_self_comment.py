"""Anti-eco no collector de menções (Decisão #45 — "PR é o quadro").

Comments cujo autor é o próprio handle do DEILE são DROPADOS antes de virarem
``MentionTrigger``. Sem esse drop, qualquer comentário que o próprio agente
postou citando seu handle viraria gatilho e dispararia trabalho redundante.

A identidade do agente vem do ``.user.login`` do comentário (campo
``CommentRef.author``) — NÃO do texto do corpo do comment (que poderia ser
escrito por qualquer pessoa).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.github_client import CommentRef
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.orchestration.pipeline.stages import _collect_mention_triggers


def _comment(comment_id: int, *, author: str, body: str = "@deile-one help", kind: str = "issue") -> CommentRef:
    return CommentRef(
        comment_id=comment_id,
        body=body,
        html_url=f"https://github.com/o/r/issues/1#issuecomment-{comment_id}",
        issue_url="https://api.github.com/repos/o/r/issues/1",
        author=author,
        kind=kind,
    )


def _make_monitor(*, issue_comments: list | None = None) -> PipelineMonitor:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        mention_handle="@deile-one",
    )
    github = MagicMock()
    github.list_issue_comments_since = AsyncMock(return_value=list(issue_comments or []))
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.list_issues_assigned_to = AsyncMock(return_value=[])
    github.list_prs_assigned_to = AsyncMock(return_value=[])
    github.list_prs_with_review_requests = AsyncMock(return_value=[])
    github.search_items_mentioning = AsyncMock(return_value=([], []))

    notifier = MagicMock()
    notifier.error = AsyncMock()
    claude = MagicMock()
    return PipelineMonitor(
        cfg, github=github, worktrees=MagicMock(), claude=claude, notifier=notifier,
    )


class TestCollectorDropsSelfComment:
    async def test_self_authored_comment_is_dropped(self):
        """Comment cujo ``author`` casa com ``gh_login`` é descartado mesmo se
        o corpo contiver ``@deile-one``. Anti-eco essencial — sem ele, o
        próprio DEILE postando um comment dispararia trigger no próximo tick."""
        c = _comment(1, author="deile-one", body="Olhei a PR e @deile-one decidiu...")
        monitor = _make_monitor(issue_comments=[c])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert triggers == []

    async def test_human_authored_comment_with_mention_arms_trigger(self):
        """Já comments de humanos com ``@deile-one`` no corpo viram triggers
        normalmente — é o caso de uso central do collector."""
        c = _comment(2, author="elimarcavalli", body="@deile-one Pode revisar?")
        monitor = _make_monitor(issue_comments=[c])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].trigger_type == "comment"
        assert triggers[0].comment is c

    async def test_trigger_author_carries_real_comment_author(self):
        """``MentionTrigger.trigger_author`` é populado com o autor real do
        comment (não com o handle do agente) — usado por testes/integradores
        que precisam distinguir quem disparou."""
        c = _comment(3, author="elimarcavalli", body="@deile-one help")
        monitor = _make_monitor(issue_comments=[c])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert triggers[0].trigger_author == "elimarcavalli"

    async def test_self_authored_comment_without_mention_is_not_picked_either(self):
        """Caso degenerado: comment do próprio DEILE sem ``@deile-one`` no
        corpo nem entra na branch de match — confirmamos que continua não
        virando trigger (não é regressão, só sanity-check)."""
        c = _comment(4, author="deile-one", body="implementação feita; merge no próximo tick")
        monitor = _make_monitor(issue_comments=[c])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert triggers == []
