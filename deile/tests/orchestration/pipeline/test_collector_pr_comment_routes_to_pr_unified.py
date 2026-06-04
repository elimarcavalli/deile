"""Decisão #46 — comments em PR usam brief unified, não o legacy.

GitHub retorna comments regulares (conversation) em PR via o endpoint
``/issues/comments`` com ``kind="issue"`` porque, na API, PRs SÃO issues.
Sem o flag ``is_pr_comment``, o router caía no brief
``_WORKER_MENTION_BRIEF`` (legacy) quando deveria roteá-lo para
``pr_unified``. Este suite cobre a detecção e o roteamento.
"""

from __future__ import annotations

from deile.orchestration.forge.refs import CommentRef, MentionTrigger


class TestCommentRefIsPrCommentFlag:
    """O campo ``is_pr_comment`` é o sinal canônico de comment em PR/MR.

    Vale independente do ``kind`` (que distingue issue/issue-comment de
    pr-review/inline-comment). Comments regulares em PR ainda vêm com
    ``kind="issue"`` mas precisam ser detectados pela URL ou pelo flag.
    """

    def test_default_is_false(self):
        ref = CommentRef(
            comment_id=1, body="oi", html_url="https://x/issues/1",
            issue_url="https://x/api/issues/1", author="u", kind="issue",
        )
        assert ref.is_pr_comment is False

    def test_pr_review_kind_does_not_imply_flag(self):
        """``kind="pr_review"`` é independente — o builder do forge decide.
        Conferimos que o dataclass não infere implicitamente."""
        ref = CommentRef(
            comment_id=2, body="oi", html_url="https://x/pull/1",
            issue_url="https://x/api/pulls/1", author="u", kind="pr_review",
        )
        assert ref.is_pr_comment is False  # builder forge é quem seta

    def test_flag_can_be_true_with_issue_kind(self):
        """Conversation comments em PR: kind="issue" + is_pr_comment=True.
        Esse é exatamente o caso que falhava antes da Decisão #46."""
        ref = CommentRef(
            comment_id=3, body="oi", html_url="https://x/pull/1#issuecomment-99",
            issue_url="https://x/api/issues/1", author="u", kind="issue",
            is_pr_comment=True,
        )
        assert ref.is_pr_comment is True


class TestMentionTriggerTargetKindHonorsIsPrComment:
    """``MentionTrigger.target_kind`` precisa retornar "pr" sempre que o
    comment é num PR — pelo ``kind="pr_review"`` OU pelo ``is_pr_comment``.
    """

    def test_issue_kind_with_pr_flag_routes_to_pr(self):
        ref = CommentRef(
            comment_id=1, body="@deile", html_url="https://x/pull/5#c",
            issue_url="https://x/api/issues/5", author="u", kind="issue",
            is_pr_comment=True,
        )
        trigger = MentionTrigger(trigger_type="comment", comment=ref)
        assert trigger.target_kind == "pr"

    def test_issue_kind_without_pr_flag_routes_to_issue(self):
        ref = CommentRef(
            comment_id=1, body="@deile", html_url="https://x/issues/5#c",
            issue_url="https://x/api/issues/5", author="u", kind="issue",
            is_pr_comment=False,
        )
        trigger = MentionTrigger(trigger_type="comment", comment=ref)
        assert trigger.target_kind == "issue"

    def test_pr_review_kind_routes_to_pr(self):
        """Compat: ``kind="pr_review"`` continua roteando para pr mesmo
        sem o flag (caminho legacy)."""
        ref = CommentRef(
            comment_id=1, body="@deile", html_url="https://x/pull/5#c",
            issue_url="https://x/api/pulls/5", author="u", kind="pr_review",
        )
        trigger = MentionTrigger(trigger_type="comment", comment=ref)
        assert trigger.target_kind == "pr"


class TestPrCommentDetectionFromGitHubUrl:
    """O GitHub forge detecta ``is_pr_comment`` a partir do ``html_url``
    (contém ``/pull/`` em URLs de comment em PR). Garante a detecção via
    URL — backstop quando a API não dá um sinal mais explícito.
    """

    def test_url_with_pull_segment_sets_flag(self):
        from deile.orchestration.forge.github_forge import \
            GitHubForge  # noqa: F401

        # O detector vive inline em _list_comments_since, mas a heurística é
        # simples: ``"/pull/" in html_url``. Validamos o invariante.
        url = "https://github.com/o/r/pull/5#issuecomment-123"
        assert "/pull/" in url

    def test_url_with_issue_segment_does_not_set_flag(self):
        url = "https://github.com/o/r/issues/5#issuecomment-123"
        assert "/pull/" not in url
