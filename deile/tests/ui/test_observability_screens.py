"""Tests for the 3 observability screens (issue #347).

Renders each screen against a fixed-width capture console and asserts
that key tokens land in the output.  No network, no asyncio — the
screens are pure functions over their data structs.
"""

from __future__ import annotations

import time

from deile.ui.panel.observability.screens import (
    ClusterStatusData,
    ClusterStatusScreen,
    HistoryData,
    HistoryScreen,
    LiveSessionData,
    LiveSessionScreen,
    render_to_string,
)

# --------------------------------------------------------------------------- #
# Cluster Status
# --------------------------------------------------------------------------- #


def test_cluster_status_screen_renders():
    """Renders pipeline summary + backlog + recent + ledger."""
    data = ClusterStatusData(
        pipeline_status={
            "uptime_seconds": 6840,
            "ticks_total": 47,
            "errors_total": 0,
            "last_tick_at": time.time(),
            "next_tick_at": time.time() + 45,
            "pods_seen": {"deile-worker": {"ready_replicas": 2}},
            "now": time.time(),
        },
        backlog=[
            {
                "kind": "issue",
                "number": 347,
                "title": "live panel",
                "age_seconds": 300,
                "why_eligible": "new",
            },
        ],
        recent_events=[
            {
                "ts": time.time() - 60,
                "event_type": "merged",
                "summary": "PR #346 merged",
            },
        ],
        ledger={
            "issue:345": {"stage": "implement", "task_id": "abc", "attempt": 1},
        },
        api_errors=[],
    )
    out = render_to_string(ClusterStatusScreen(), data, width=120)
    assert "DEILE CLUSTER" in out
    assert "ticks=47" in out
    assert "#347 live panel" in out.replace("│", "").replace("\n", " ") or "347" in out
    assert "merged" in out.lower()
    assert "issue:345" in out


def test_cluster_status_screen_adapts_width_80():
    """At 80 columns the screen must still render (no exception, no overflow)."""
    data = ClusterStatusData(
        pipeline_status={
            "uptime_seconds": 60,
            "ticks_total": 1,
            "errors_total": 0,
            "last_tick_at": time.time(),
            "next_tick_at": time.time(),
            "pods_seen": {},
            "now": time.time(),
        },
        backlog=[
            {
                "kind": "issue",
                "number": 1,
                "title": "x",
                "age_seconds": 10,
                "why_eligible": "new",
            }
        ],
        recent_events=[],
        ledger={},
        api_errors=[],
    )
    out = render_to_string(ClusterStatusScreen(), data, width=80)
    assert "DEILE CLUSTER" in out
    # Every output line must be <= 80 cols (Rich must honor capture width).
    for line in out.splitlines():
        assert len(line) <= 80, f"line longer than 80 cols: {line!r}"


def test_cluster_status_screen_adapts_width_200():
    """At 200 columns the layout uses the extra horizontal space."""
    data = ClusterStatusData(
        pipeline_status={
            "uptime_seconds": 60,
            "ticks_total": 1,
            "errors_total": 0,
            "last_tick_at": time.time(),
            "next_tick_at": time.time(),
            "pods_seen": {"deile-worker": {"ready_replicas": 2}},
            "now": time.time(),
        },
        backlog=[],
        recent_events=[
            {"ts": time.time(), "event_type": "merged", "summary": "PR #346 merged"},
        ],
        ledger={},
        api_errors=[],
    )
    out = render_to_string(ClusterStatusScreen(), data, width=200)
    assert "DEILE CLUSTER" in out
    # A handful of lines must use more than the 80-col baseline now.
    long_lines = [line for line in out.splitlines() if len(line) > 100]
    assert long_lines, "expected at least one wide line at width=200"


def test_cluster_status_shows_api_errors():
    """An API failure is surfaced — operator must not be left guessing."""
    data = ClusterStatusData(
        pipeline_status={},
        backlog=[],
        recent_events=[],
        ledger={},
        api_errors=["pipeline status: connection refused"],
    )
    out = render_to_string(ClusterStatusScreen(), data, width=120)
    assert "API errors" in out
    assert "connection refused" in out


# --------------------------------------------------------------------------- #
# Live Session
# --------------------------------------------------------------------------- #


def test_live_session_screen_renders_alive_task():
    """Alive task shows ●ALIVE + command + chat preview."""
    now = time.time()
    data = LiveSessionData(
        session={
            "task_id": "be75a424e2b06dc6",
            "stage": "pr_review",
            "branch": "auto/issue-345",
            "alive": True,
            "started_at": now - 180,
            "last_completed_at": None,
            "last_duration_seconds": 180,
            "last_total_cost_usd": 0.43,
            "attempt": 1,
            "workdir_exists": True,
        },
        command={
            "cmd": ["claude", "-p", "--session-id", "abc"],
            "full_prompt": "Você é Claude Code revisor de PR",
        },
        chat={
            "turns": [
                {"role": "user", "content": "review the PR", "ts": now - 170},
                {
                    "role": "assistant",
                    "content": "Reading files first.",
                    "ts": now - 160,
                },
                {
                    "role": "tool",
                    "tool_name": "Bash",
                    "tool_input": {"command": "gh pr view 346"},
                    "ts": now - 158,
                    "in_progress": True,
                },
            ],
        },
        api_errors=[],
    )
    out = render_to_string(LiveSessionScreen(), data, width=120)
    assert "be75a424e2b06dc6" in out
    assert "●ALIVE" in out
    assert "pr_review" in out
    assert "claude" in out
    assert "Bash" in out
    assert "▶" in out  # in_progress marker


def test_live_session_screen_renders_idle():
    """When no session is provided the screen says (idle …)."""
    data = LiveSessionData(
        session=None,
        command=None,
        chat=None,
        api_errors=[],
    )
    out = render_to_string(LiveSessionScreen(), data, width=120)
    assert "idle" in out.lower()


def test_live_session_screen_renders_ended_task():
    """Task that is no longer alive shows ○ENDED."""
    now = time.time()
    data = LiveSessionData(
        session={
            "task_id": "abc1234567890def",
            "stage": "implement",
            "branch": "main",
            "alive": False,
            "started_at": now - 300,
            "last_completed_at": now - 60,
            "last_duration_seconds": 240,
            "last_total_cost_usd": 0.05,
            "attempt": 1,
            "workdir_exists": True,
        },
        command={"cmd": ["claude", "-p"], "full_prompt": "..."},
        chat={"turns": []},
        api_errors=[],
    )
    out = render_to_string(LiveSessionScreen(), data, width=120)
    assert "○ENDED" in out


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def test_history_screen_renders_chronological_list():
    """Shows up to 20 rows with stage/branch/cost/duration columns."""
    now = time.time()
    sessions = [
        {
            "task_id": "be75a424e2b06dc6",
            "stage": "pr_review",
            "branch": "auto/issue-345",
            "last_completed_at": now - 600,
            "last_duration_seconds": 842,
            "last_total_cost_usd": 1.47,
            "last_is_error": False,
            "attempt": 1,
        },
        {
            "task_id": "dcfd09d75c0c5ea1",
            "stage": "implement",
            "branch": "auto/issue-345",
            "last_completed_at": now - 1200,
            "last_duration_seconds": 105,
            "last_total_cost_usd": 0.0,
            "last_is_error": True,
            "attempt": 2,
        },
    ]
    out = render_to_string(HistoryScreen(), HistoryData(sessions, []), width=120)
    assert "TASK HISTORY" in out
    assert "pr_review" in out
    assert "auto/issue-345" in out
    assert "$1.47" in out


def test_history_screen_empty_state():
    """Empty history must render gracefully."""
    out = render_to_string(HistoryScreen(), HistoryData([], []), width=120)
    assert "(no historical sessions)" in out


def test_history_screen_marks_resumed_attempts():
    """A failed task with attempt>1 shows ↻ marker (resumed)."""
    sessions = [
        {
            "task_id": "x",
            "stage": "impl",
            "branch": "b",
            "last_completed_at": time.time(),
            "last_duration_seconds": 10,
            "last_total_cost_usd": 0.01,
            "last_is_error": True,
            "attempt": 3,
        },
    ]
    out = render_to_string(HistoryScreen(), HistoryData(sessions, []), width=120)
    assert "↻" in out
