"""Unit tests for the follow-up detector heuristic."""

from __future__ import annotations

import pytest

from deile.orchestration.pipeline.follow_up_detector import (FollowUp,
                                                             detect_follow_ups)


class TestDetectFollowUps:
    def test_empty_inputs_return_empty(self):
        assert detect_follow_ups("", []) == []

    def test_no_section_no_explicit_returns_empty(self):
        body = "This PR fixes a bug in the parser. All tests pass."
        assert detect_follow_ups(body, []) == []

    # ── Section-based detection ────────────────────────────────────────────

    def test_detects_english_followup_section(self):
        body = """\
## Summary
Fixed the thing.

## Follow-up
- Add unit tests for edge cases
- Update the docs
"""
        results = detect_follow_ups(body, [])
        assert len(results) == 2
        assert results[0].title == "Add unit tests for edge cases"
        assert results[1].title == "Update the docs"
        assert all(not r.is_breaking for r in results)

    def test_detects_followups_plural(self):
        body = "## Follow-ups\n- Open an issue for caching\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 1

    def test_detects_portuguese_proximos_passos(self):
        body = """\
## Próximos passos
- Adicionar suporte a JSON
- Criar issue para refactor do parser
"""
        results = detect_follow_ups(body, [])
        assert len(results) == 2

    def test_detects_trabalho_futuro(self):
        body = "## Trabalho futuro\n- Revisar a API\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 1

    def test_detects_future_work(self):
        body = "## Future work\n- Migrate to async IO\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 1

    def test_detects_next_steps(self):
        body = "## Next steps\n- Write migration guide\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 1

    def test_section_ends_at_next_header(self):
        body = """\
## Follow-up
- Item A
- Item B

## Unrelated section
- Item C (should not be picked up)
"""
        results = detect_follow_ups(body, [])
        titles = [r.title for r in results]
        assert "Item A" in titles
        assert "Item B" in titles
        assert "Item C (should not be picked up)" not in titles

    def test_nested_header_ends_section(self):
        body = """\
### Follow-up
- Do X

#### Details
Not a bullet to open

### Conclusion
"""
        results = detect_follow_ups(body, [])
        assert results[0].title == "Do X"
        assert len(results) == 1

    # ── Explicit bullet detection outside sections ─────────────────────────

    def test_detects_explicit_abrir_issue_bullet(self):
        body = "Some text.\n\n- Abrir issue para melhorar logging\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 1
        assert "logging" in results[0].title.lower()

    def test_detects_explicit_criar_issue_bullet(self):
        body = "- Criar issue sobre performance do banco\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 1

    def test_detects_open_issue_english(self):
        body = "- Open issue for retry logic\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 1

    def test_explicit_detection_ignores_plain_bullets(self):
        body = "- This is a normal bullet\n- Another normal bullet\n"
        assert detect_follow_ups(body, []) == []

    # ── Breaking-change classification ────────────────────────────────────

    def test_breaking_change_keyword_flags_item(self):
        body = "## Follow-up\n- Breaking change: remove legacy API endpoint\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 1
        assert results[0].is_breaking is True

    def test_mudanca_breaking_flags_item(self):
        body = "## Follow-up\n- Mudança breaking na interface do plugin\n"
        results = detect_follow_ups(body, [])
        assert results[0].is_breaking is True

    def test_non_breaking_item_is_false(self):
        body = "## Follow-up\n- Add caching layer\n"
        results = detect_follow_ups(body, [])
        assert results[0].is_breaking is False

    def test_incompatible_keyword_flags_item(self):
        body = "## Next steps\n- Remove incompatible fallback path\n"
        results = detect_follow_ups(body, [])
        assert results[0].is_breaking is True

    # ── Deduplication ─────────────────────────────────────────────────────

    def test_deduplicates_same_item_across_body_and_comment(self):
        body = "## Follow-up\n- Add retry logic\n"
        comment = "## Follow-up\n- Add retry logic\n"
        results = detect_follow_ups(body, [comment])
        assert len(results) == 1

    def test_deduplicates_case_insensitive(self):
        body = "## Follow-up\n- Add retry logic\n"
        comment = "## Follow-up\n- add retry logic\n"
        results = detect_follow_ups(body, [comment])
        assert len(results) == 1

    # ── Max items cap ─────────────────────────────────────────────────────

    def test_max_items_capped_at_five(self):
        items = "\n".join(f"- Item {i}" for i in range(10))
        body = f"## Follow-up\n{items}\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 5

    # ── Comment scanning ──────────────────────────────────────────────────

    def test_detects_follow_ups_in_comments(self):
        body = "Fixed the parser."
        comment = "## Next steps\n- Write integration tests\n"
        results = detect_follow_ups(body, [comment])
        assert len(results) == 1
        assert results[0].title == "Write integration tests"

    def test_scans_multiple_comments(self):
        body = ""
        comments = [
            "## Follow-up\n- Task A\n",
            "## Follow-up\n- Task B\n",
        ]
        results = detect_follow_ups(body, comments)
        titles = [r.title for r in results]
        assert "Task A" in titles
        assert "Task B" in titles

    # ── GFM checkbox items ────────────────────────────────────────────────

    def test_strips_checkbox_prefix(self):
        body = "## Follow-up\n- [ ] Write a migration guide\n"
        results = detect_follow_ups(body, [])
        assert results[0].title == "Write a migration guide"

    def test_checked_checkbox_still_captured(self):
        body = "## Follow-up\n- [x] Already done thing\n"
        results = detect_follow_ups(body, [])
        assert results[0].title == "Already done thing"

    # ── Edge cases ────────────────────────────────────────────────────────

    def test_empty_body_with_comments(self):
        results = detect_follow_ups("", ["## Follow-up\n- Do something\n"])
        assert len(results) == 1

    def test_title_truncated_at_120_chars(self):
        long_item = "A" * 200
        body = f"## Follow-up\n- {long_item}\n"
        results = detect_follow_ups(body, [])
        assert len(results[0].title) <= 120

    def test_numbered_list_items_captured(self):
        body = "## Follow-up\n1. First task\n2. Second task\n"
        results = detect_follow_ups(body, [])
        assert len(results) == 2

    def test_returns_frozen_dataclass(self):
        body = "## Follow-up\n- Add tests\n"
        result = detect_follow_ups(body, [])
        assert isinstance(result[0], FollowUp)
        with pytest.raises(Exception):
            result[0].title = "mutate"  # type: ignore[misc]

    def test_all_breaking_returns_only_breaking_items(self):
        body = (
            "## Follow-up\n"
            "- Breaking change: remove v1 API\n"
            "- Incompatible interface change\n"
        )
        results = detect_follow_ups(body, [])
        assert len(results) == 2
        assert all(r.is_breaking for r in results)
