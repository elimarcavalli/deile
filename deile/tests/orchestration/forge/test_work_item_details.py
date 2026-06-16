"""Tests for :class:`WorkItemDetails` and :meth:`ForgeClient.get_work_item_details`.

Covers:
- ``WorkItemDetails`` dataclass defaults and construction
- ``GitHubForge.get_work_item_details`` via mocked ``_run``
- ``GitLabForge.get_work_item_details`` via mocked ``_run``
- ``_parse_linked_items`` logic (regex inline — avoids loading the infra module)
"""

from __future__ import annotations

import json
import re
from collections import deque
from typing import Tuple

import pytest

from deile.orchestration.forge.base import ForgeConfig, ForgeKind, WorkItemDetails
from deile.orchestration.forge.github_forge import GitHubForge
from deile.orchestration.forge.gitlab_forge import GitLabForge

# ---------------------------------------------------------------------------
# Reference implementation of _parse_linked_items for isolated testing
# ---------------------------------------------------------------------------

_LINKED_ITEM_RE = re.compile(
    r"\b(?P<kw>clos(?:e[sd]?|ing)|fix(?:e[sd]|ing)?|resolv(?:e[sd]?|ing)|ref(?:erence(?:d|s)?)?s?)"
    r"\s+(?:(?P<mr>![0-9]+)|#(?P<issue>[0-9]+))",
    re.IGNORECASE,
)


def _parse_linked_items(body: str):
    """Mirror of infra/k8s/_panel_data._parse_linked_items for isolated tests."""
    if not body:
        return []
    results = []
    for m in _LINKED_ITEM_RE.finditer(body):
        kw = m.group("kw").lower()
        closing = kw.startswith(("clos", "fix", "resolv"))
        raw = m.group("mr") or m.group("issue") or ""
        try:
            from types import SimpleNamespace

            results.append(
                SimpleNamespace(
                    kind="closes" if closing else "refs",
                    number=int(raw.lstrip("!")),
                )
            )
        except ValueError:
            pass
    return results


# ---------------------------------------------------------------------------
# WorkItemDetails dataclass
# ---------------------------------------------------------------------------


def test_work_item_details_defaults():
    wd = WorkItemDetails(number=1, kind="issue")
    assert wd.number == 1
    assert wd.kind == "issue"
    assert wd.author == ""
    assert wd.ci_status == "none"
    assert wd.ci_checks_summary == (0, 0)
    assert wd.mergeability == "unknown"
    assert wd.requested_reviewers == []
    assert wd.comments_count == 0
    assert wd.linked_items == []


def test_work_item_details_full():
    wd = WorkItemDetails(
        number=42,
        kind="pr",
        author="alice",
        ci_status="passing",
        ci_checks_summary=(5, 5),
        mergeability="clean",
        requested_reviewers=[("bob", "pending")],
        comments_count=3,
        linked_items=[("closes", 10)],
    )
    assert wd.author == "alice"
    assert wd.ci_status == "passing"
    assert wd.ci_checks_summary == (5, 5)
    assert wd.mergeability == "clean"
    assert wd.requested_reviewers == [("bob", "pending")]
    assert wd.comments_count == 3
    assert wd.linked_items == [("closes", 10)]


# ---------------------------------------------------------------------------
# _parse_linked_items logic tests
# ---------------------------------------------------------------------------


def test_parse_closes_hash():
    items = _parse_linked_items("Closes #42")
    assert len(items) == 1
    assert items[0].kind == "closes"
    assert items[0].number == 42


def test_parse_fixes_hash():
    items = _parse_linked_items("Fixes #7")
    assert items[0].kind == "closes"
    assert items[0].number == 7


def test_parse_resolves_hash():
    items = _parse_linked_items("Resolves #100")
    assert items[0].kind == "closes"
    assert items[0].number == 100


def test_parse_refs_hash():
    items = _parse_linked_items("Refs #5")
    assert items[0].kind == "refs"
    assert items[0].number == 5


def test_parse_multiple_links():
    body = "Closes #1\nAlso fixes #2 and references #3"
    items = _parse_linked_items(body)
    numbers = [it.number for it in items]
    assert 1 in numbers
    assert 2 in numbers
    assert 3 in numbers


def test_parse_mr_ref_extracts_number():
    # "Closes !99" = closing MR #99 (GitLab-style !N ref); number is extracted.
    items = _parse_linked_items("Closes !99")
    assert len(items) == 1
    assert items[0].kind == "closes"
    assert items[0].number == 99


def test_parse_empty_body():
    assert _parse_linked_items("") == []
    assert _parse_linked_items(None) == []


def test_parse_case_insensitive():
    items = _parse_linked_items("CLOSES #55")
    assert items[0].number == 55


# ---------------------------------------------------------------------------
# GitHubForge.get_work_item_details
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_gh(monkeypatch):
    responses: "deque[Tuple[int, str, str]]" = deque()
    calls: list[tuple] = []

    cfg = ForgeConfig(
        kind=ForgeKind.GITHUB,
        host="github.com",
        project_path="owner/repo",
        cli_path="/usr/bin/gh",
    )
    forge = GitHubForge(cfg)

    async def fake_run(self, *args):
        calls.append(args)
        if not responses:
            return (0, "[]", "")
        return responses.popleft()

    monkeypatch.setattr(GitHubForge, "_run", fake_run)
    return forge, responses, calls


async def test_gh_get_work_item_details_issue(fake_gh):
    forge, responses, calls = fake_gh
    responses.append(
        (
            0,
            json.dumps(
                {
                    "number": 10,
                    "user": {"login": "alice"},
                    "comments": 2,
                    "body": "Closes #3",
                }
            ),
            "",
        )
    )
    wd = await forge.get_work_item_details("issue", 10)

    assert wd.number == 10
    assert wd.kind == "issue"
    assert wd.author == "alice"
    assert wd.comments_count == 2
    assert any(n == 3 for _, n in wd.linked_items)
    # Issue should not trigger PR endpoint
    assert len(calls) == 1


async def test_gh_links_refs_not_classified_as_closes(fake_gh):
    """refs/references must produce kind='refs', not 'closes' (r is in 'cfr' — old bug)."""
    forge, responses, _ = fake_gh
    responses.append(
        (
            0,
            json.dumps(
                {
                    "number": 11,
                    "user": {"login": "alice"},
                    "comments": 0,
                    "body": "Refs #7\nReferences #8",
                }
            ),
            "",
        )
    )
    wd = await forge.get_work_item_details("issue", 11)

    assert len(wd.linked_items) == 2
    assert all(kind == "refs" for kind, _ in wd.linked_items)
    assert {n for _, n in wd.linked_items} == {7, 8}


async def test_gh_get_work_item_details_pr_clean(fake_gh):
    forge, responses, calls = fake_gh
    # Issue detail
    responses.append(
        (
            0,
            json.dumps(
                {
                    "number": 20,
                    "user": {"login": "bob"},
                    "comments": 0,
                    "body": "",
                }
            ),
            "",
        )
    )
    # PR detail
    responses.append(
        (
            0,
            json.dumps(
                {
                    "number": 20,
                    "draft": False,
                    "mergeable_state": "clean",
                    "requested_reviewers": [{"login": "carol"}],
                }
            ),
            "",
        )
    )
    # PR checks
    responses.append(
        (
            0,
            json.dumps(
                [
                    {"bucket": "pass", "state": "completed", "conclusion": "success"},
                    {"bucket": "pass", "state": "completed", "conclusion": "success"},
                ]
            ),
            "",
        )
    )

    wd = await forge.get_work_item_details("pr", 20)

    assert wd.kind == "pr"
    assert wd.author == "bob"
    assert wd.mergeability == "clean"
    assert wd.ci_status == "passing"
    assert wd.ci_checks_summary == (2, 2)
    assert any(login == "carol" for login, _ in wd.requested_reviewers)


async def test_gh_get_work_item_details_pr_draft(fake_gh):
    forge, responses, _ = fake_gh
    responses.append(
        (
            0,
            json.dumps(
                {"number": 5, "user": {"login": "x"}, "comments": 0, "body": ""}
            ),
            "",
        )
    )
    responses.append(
        (0, json.dumps({"number": 5, "draft": True, "mergeable_state": "clean"}), "")
    )
    responses.append((0, "[]", ""))

    wd = await forge.get_work_item_details("pr", 5)
    assert wd.mergeability == "draft"


async def test_gh_get_work_item_details_pr_conflict(fake_gh):
    forge, responses, _ = fake_gh
    responses.append(
        (
            0,
            json.dumps(
                {"number": 6, "user": {"login": "x"}, "comments": 0, "body": ""}
            ),
            "",
        )
    )
    responses.append(
        (0, json.dumps({"number": 6, "draft": False, "mergeable_state": "dirty"}), "")
    )
    responses.append((0, "[]", ""))

    wd = await forge.get_work_item_details("pr", 6)
    assert wd.mergeability == "conflict"


async def test_gh_get_work_item_details_api_failure_returns_minimal(fake_gh):
    forge, responses, _ = fake_gh
    responses.append((1, "", "error"))  # item call fails
    responses.append((1, "", "error"))  # pr detail fails
    responses.append((1, "", "error"))  # checks fails

    wd = await forge.get_work_item_details("pr", 99)
    # Should return a minimal struct without raising
    assert wd.number == 99
    assert wd.kind == "pr"
    assert wd.author == ""


# ---------------------------------------------------------------------------
# GitLabForge.get_work_item_details
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_gl(monkeypatch):
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
            return (0, "{}", "")
        return responses.popleft()

    monkeypatch.setattr(GitLabForge, "_run", fake_run)
    return forge, responses, calls


async def test_gl_get_work_item_details_issue(fake_gl):
    forge, responses, _ = fake_gl
    responses.append(
        (
            0,
            json.dumps(
                {
                    "iid": 8,
                    "author": {"username": "alice"},
                    "user_notes_count": 4,
                    "description": "Refs #2",
                }
            ),
            "",
        )
    )

    wd = await forge.get_work_item_details("issue", 8)
    assert wd.number == 8
    assert wd.author == "alice"
    assert wd.comments_count == 4
    assert any(n == 2 for _, n in wd.linked_items)


async def test_gl_get_work_item_details_mr_clean(fake_gl):
    forge, responses, _ = fake_gl
    responses.append(
        (
            0,
            json.dumps(
                {
                    "iid": 15,
                    "author": {"username": "bob"},
                    "user_notes_count": 1,
                    "description": "",
                    "work_in_progress": False,
                    "has_conflicts": False,
                    "merge_status": "can_be_merged",
                    "reviewers": [{"username": "carol"}],
                    "head_pipeline": {"status": "success", "id": 100},
                }
            ),
            "",
        )
    )

    wd = await forge.get_work_item_details("pr", 15)
    assert wd.mergeability == "clean"
    assert wd.ci_status == "passing"
    assert any(login == "carol" for login, _ in wd.requested_reviewers)


async def test_gl_get_work_item_details_mr_conflict(fake_gl):
    forge, responses, _ = fake_gl
    responses.append(
        (
            0,
            json.dumps(
                {
                    "iid": 16,
                    "author": {"username": "x"},
                    "user_notes_count": 0,
                    "description": "",
                    "work_in_progress": False,
                    "has_conflicts": True,
                    "merge_status": "cannot_be_merged",
                    "head_pipeline": None,
                }
            ),
            "",
        )
    )

    wd = await forge.get_work_item_details("pr", 16)
    assert wd.mergeability == "conflict"
    assert wd.ci_status == "none"


async def test_gl_get_work_item_details_mr_draft(fake_gl):
    forge, responses, _ = fake_gl
    responses.append(
        (
            0,
            json.dumps(
                {
                    "iid": 17,
                    "author": {"username": "y"},
                    "user_notes_count": 0,
                    "description": "",
                    "work_in_progress": True,
                    "has_conflicts": False,
                    "merge_status": "can_be_merged",
                    "head_pipeline": None,
                }
            ),
            "",
        )
    )

    wd = await forge.get_work_item_details("pr", 17)
    assert wd.mergeability == "draft"


async def test_gl_get_work_item_details_api_failure(fake_gl):
    forge, responses, _ = fake_gl
    responses.append((1, "", "server error"))

    wd = await forge.get_work_item_details("pr", 999)
    assert wd.number == 999
    assert wd.author == ""
