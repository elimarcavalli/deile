"""Tests for render_mention_prompt added for issue #164 (gap 4)."""
from __future__ import annotations

from deile.orchestration.pipeline.claude_dispatcher import \
    render_mention_prompt


class TestRenderMentionPrompt:
    def test_contains_author(self):
        p = render_mention_prompt("o/r", "https://github.com/o/r/issues/1", "hello", "alice")
        assert "alice" in p

    def test_contains_context_url(self):
        url = "https://github.com/o/r/issues/1"
        p = render_mention_prompt("o/r", url, "msg", "bob")
        assert url in p

    def test_contains_comment_body(self):
        p = render_mention_prompt("o/r", "url", "please do X", "user")
        assert "please do X" in p

    def test_contains_repo(self):
        p = render_mention_prompt("owner/name", "url", "msg", "user")
        assert "owner/name" in p

    def test_is_nonempty_string(self):
        p = render_mention_prompt("o/r", "https://github.com/o/r/issues/1", "body", "author")
        assert isinstance(p, str)
        assert len(p) > 0

    def test_instructs_to_post_response(self):
        p = render_mention_prompt("o/r", "https://github.com/o/r/issues/1", "body", "author")
        # The prompt should instruct the agent to post a response
        assert "gh" in p.lower() or "comment" in p.lower() or "responda" in p.lower()
