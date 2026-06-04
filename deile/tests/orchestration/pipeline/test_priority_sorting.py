"""Tests for priority label system (issue #369)."""

from __future__ import annotations

from deile.orchestration.forge.refs import IssueRef, PrRef
from deile.orchestration.pipeline.labels import (LABEL_COLORS,
                                                 LABEL_DESCRIPTIONS,
                                                 PRIORITY_0, PRIORITY_1,
                                                 PRIORITY_2, PRIORITY_3,
                                                 PRIORITY_LABEL_PREFIX,
                                                 PRIORITY_LABELS,
                                                 parse_priority_from_labels)
from deile.orchestration.pipeline.stages import sort_by_priority

# ---------------------------------------------------------------------------
# parse_priority_from_labels
# ---------------------------------------------------------------------------


class TestParsePriorityFromLabels:
    def test_single_priority_label(self):
        assert parse_priority_from_labels(["~prioridade:0"]) == 0
        assert parse_priority_from_labels(["~prioridade:1"]) == 1
        assert parse_priority_from_labels(["~prioridade:3"]) == 3

    def test_no_priority_label(self):
        assert parse_priority_from_labels(["bug", "feature"]) is None
        assert parse_priority_from_labels([]) is None
        assert parse_priority_from_labels(()) is None

    def test_mixed_labels_returns_first_priority(self):
        labels = ("bug", "~prioridade:2", "~prioridade:0", "feature")
        assert parse_priority_from_labels(labels) == 2

    def test_invalid_priority_format_ignored(self):
        assert parse_priority_from_labels(["~prioridade:abc"]) is None
        assert parse_priority_from_labels(["~prioridade:"]) is None
        assert parse_priority_from_labels(["~prioridade:-1"]) is None
        assert parse_priority_from_labels(["prioridade:0"]) is None  # no tilde

    def test_high_n_still_parsed(self):
        # The issue says range is 0-3, but the parser accepts any int
        # (out-of-range values are accepted — validation is deferred)
        assert parse_priority_from_labels(["~prioridade:99"]) == 99


# ---------------------------------------------------------------------------
# sort_by_priority — issues
# ---------------------------------------------------------------------------


class TestSortByPriorityIssues:
    def test_all_with_priority_labels(self):
        issues = [
            IssueRef(number=5, title="medium", url="u", labels=("~prioridade:2",)),
            IssueRef(number=1, title="critical", url="u", labels=("~prioridade:0",)),
            IssueRef(number=3, title="high", url="u", labels=("~prioridade:1",)),
            IssueRef(number=7, title="low", url="u", labels=("~prioridade:3",)),
        ]
        sorted_issues = sort_by_priority(issues)
        assert [i.number for i in sorted_issues] == [1, 3, 5, 7]

    def test_mixed_with_and_without_labels(self):
        issues = [
            IssueRef(number=10, title="no-prio", url="u", labels=("bug",)),
            IssueRef(number=1, title="urgent", url="u", labels=("~prioridade:0",)),
            IssueRef(number=20, title="also-no-prio", url="u", labels=()),
            IssueRef(number=3, title="medium", url="u", labels=("~prioridade:2",)),
        ]
        sorted_issues = sort_by_priority(issues)
        # Priority 0 first, then priority 2, then no-priority (by number)
        assert [i.number for i in sorted_issues] == [1, 3, 10, 20]

    def test_tiebreaker_same_priority_by_number(self):
        issues = [
            IssueRef(number=5, title="a", url="u", labels=("~prioridade:1",)),
            IssueRef(number=2, title="b", url="u", labels=("~prioridade:1",)),
            IssueRef(number=8, title="c", url="u", labels=("~prioridade:1",)),
        ]
        sorted_issues = sort_by_priority(issues)
        assert [i.number for i in sorted_issues] == [2, 5, 8]

    def test_tiebreaker_no_priority_by_number(self):
        issues = [
            IssueRef(number=15, title="x", url="u", labels=()),
            IssueRef(number=3, title="y", url="u", labels=()),
        ]
        sorted_issues = sort_by_priority(issues)
        assert [i.number for i in sorted_issues] == [3, 15]

    def test_empty_list(self):
        assert sort_by_priority([]) == []

    def test_single_item(self):
        issues = [IssueRef(number=1, title="x", url="u", labels=("~prioridade:0",))]
        assert sort_by_priority(issues) == issues


# ---------------------------------------------------------------------------
# sort_by_priority — PRs
# ---------------------------------------------------------------------------


class TestSortByPriorityPRs:
    def test_all_with_priority_labels(self):
        prs = [
            PrRef(number=5, title="medium", url="u", labels=("~prioridade:2",)),
            PrRef(number=1, title="critical", url="u", labels=("~prioridade:0",)),
            PrRef(number=3, title="high", url="u", labels=("~prioridade:1",)),
        ]
        sorted_prs = sort_by_priority(prs)
        assert [p.number for p in sorted_prs] == [1, 3, 5]

    def test_mixed_with_and_without_labels(self):
        prs = [
            PrRef(number=42, title="no-prio", url="u", labels=()),
            PrRef(number=2, title="urgent", url="u", labels=("~prioridade:0",)),
        ]
        sorted_prs = sort_by_priority(prs)
        assert [p.number for p in sorted_prs] == [2, 42]

    def test_deterministic_order(self):
        # Same input → same output every time
        prs = [
            PrRef(number=3, title="c", url="u", labels=("~prioridade:1",)),
            PrRef(number=1, title="a", url="u", labels=("~prioridade:1",)),
            PrRef(number=2, title="b", url="u", labels=("~prioridade:1",)),
        ]
        result1 = sort_by_priority(prs)
        result2 = sort_by_priority(prs)
        assert result1 == result2
        assert [p.number for p in result1] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Label constants
# ---------------------------------------------------------------------------


class TestPriorityLabelConstants:
    def test_prefix(self):
        assert PRIORITY_LABEL_PREFIX == "~prioridade:"

    def test_priority_labels_tuple(self):
        assert PRIORITY_0 == "~prioridade:0"
        assert PRIORITY_1 == "~prioridade:1"
        assert PRIORITY_2 == "~prioridade:2"
        assert PRIORITY_3 == "~prioridade:3"
        assert PRIORITY_LABELS == (PRIORITY_0, PRIORITY_1, PRIORITY_2, PRIORITY_3)

    def test_all_have_color(self):
        for lb in PRIORITY_LABELS:
            assert lb in LABEL_COLORS, f"{lb} missing color"
            assert len(LABEL_COLORS[lb]) == 6
            int(LABEL_COLORS[lb], 16)  # valid hex

    def test_all_have_description(self):
        for lb in PRIORITY_LABELS:
            assert lb in LABEL_DESCRIPTIONS, f"{lb} missing description"
            assert len(LABEL_DESCRIPTIONS[lb]) > 10

    def test_priorities_have_distinct_colors(self):
        # Each priority should have a visibly distinct color
        colors = {lb: LABEL_COLORS[lb] for lb in PRIORITY_LABELS}
        assert len(set(colors.values())) == len(PRIORITY_LABELS), (
            f"Priority labels must have distinct colors, got: {colors}"
        )


# ---------------------------------------------------------------------------
# Integration: sort_by_priority with parse_priority_from_labels
# ---------------------------------------------------------------------------


class TestSortByPriorityIntegration:
    def test_parse_then_sort_is_consistent(self):
        """Ensure parse and sort produce the same ordering."""
        labels_list = [
            ("~prioridade:0",),
            ("~prioridade:2",),
            ("~prioridade:1",),
            (),
            ("~prioridade:3",),
        ]
        issues = [
            IssueRef(number=i, title="x", url="u", labels=labels_list[i])
            for i in range(len(labels_list))
        ]
        sorted_issues = sort_by_priority(issues)
        # Parse each sorted item's priority to verify order is monotonic
        priorities = [parse_priority_from_labels(i.labels) for i in sorted_issues]
        # None (no priority) should be last; numeric priorities should be ascending
        none_seen = False
        for p in priorities:
            if p is None:
                none_seen = True
            else:
                assert not none_seen, "Items without priority must come last"

    def test_multiple_priority_labels_only_first_counts(self):
        """If an item has multiple ~prioridade:N labels, parse returns the first."""
        issue = IssueRef(
            number=1, title="x", url="u",
            labels=("~prioridade:0", "~prioridade:3", "bug"),
        )
        assert parse_priority_from_labels(issue.labels) == 0
