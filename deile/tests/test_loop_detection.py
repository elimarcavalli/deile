"""Tests for ToolLoopGuard and its integration with the tool-loop drivers.

Covers the four detection rules in ``deile.core.loop_guard``:
  1. Hard ceiling on calls per turn.
  2. Identical-call repetition.
  3. Sliding-window repetition (allows one different call sandwiched in).
  4. No-progress streak (consecutive empty/error results).

Plus the integration tests against ``ToolLoopExecutor``: the bash-fail-then-loop
case from the user's screenshot, the "30 different calls don't trigger" case,
and the assertion that the audit logger sees a SUSPICIOUS_ACTIVITY event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from deile.core.loop_guard import (AbortKind, ToolLoopGuard, args_hash_for,
                                   format_loop_break_message, make_guard,
                                   tool_result_made_progress)
from deile.core.models.base import ModelMessage
from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent
from deile.core.tool_loop_executor import ToolLoopExecutor
from deile.security.audit_logger import (AuditEventType, SeverityLevel,
                                         get_audit_logger)
from deile.tools.base import ToolResult, ToolStatus

# ---------------------------------------------------------------------------
# Fakes (shared with the executor's own test file in shape, but kept local
# so this file is self-contained)
# ---------------------------------------------------------------------------


@dataclass
class FakeProvider:
    """Provider stub: replays a queue of event-lists per iteration."""

    provider_id: str = "fake"
    iterations: List[List[UnifiedStreamEvent]] = field(default_factory=list)
    seen_messages: List[List[ModelMessage]] = field(default_factory=list)
    _idx: int = 0

    async def generate_stream(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        tools: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        self.seen_messages.append(list(messages))
        if self._idx >= len(self.iterations):
            return
        events = self.iterations[self._idx]
        self._idx += 1
        for event in events:
            yield event

    def format_assistant_tool_use_message(
        self,
        pending_tool_calls,
        text_so_far: str = "",
        reasoning_content: Optional[str] = None,
    ) -> ModelMessage:
        return ModelMessage(role="assistant", content=text_so_far)

    def format_tool_result_message(
        self, tool_call_id: str, tool_name: str, payload: Any
    ) -> ModelMessage:
        return ModelMessage(role="tool", content=str(payload))


@dataclass
class FakeRegistry:
    results: Dict[str, ToolResult] = field(default_factory=dict)
    seen: List[str] = field(default_factory=list)
    default: Optional[ToolResult] = None

    async def execute_tool(self, name: str, ctx) -> ToolResult:
        self.seen.append(name)
        if name in self.results:
            return self.results[name]
        if self.default is not None:
            return self.default
        return ToolResult(status=ToolStatus.SUCCESS, data={"echo": name}, message="ok")


def _tool_use_round(call_id: str, name: str, args: Dict[str, Any]):
    """Build the per-iteration event list a provider would emit for ONE call."""
    return [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id=call_id,
            tool_name=name,
            arguments=args,
        ),
    ]


# ---------------------------------------------------------------------------
# Unit tests — ToolLoopGuard rules in isolation
# ---------------------------------------------------------------------------


def test_guard_allows_distinct_calls():
    guard = ToolLoopGuard()
    for i in range(15):
        assert (
            guard.check(f"tool_{i}", {"i": i}) is None
        ), f"call #{i} unexpectedly aborted"
    assert guard.aborted_reason is None
    assert len(guard.history) == 15


def test_guard_trips_on_three_identical_consecutive_calls():
    guard = ToolLoopGuard()
    assert guard.check("list_files", {"path": "."}) is None
    assert guard.check("list_files", {"path": "."}) is None
    abort = guard.check("list_files", {"path": "."})
    assert abort is not None
    assert abort.kind is AbortKind.IDENTICAL_REPEAT
    assert abort.tool_name == "list_files"
    assert abort.repeat_count >= 3


def test_guard_does_not_trip_on_two_identical_calls():
    """Two identical calls is fine — sometimes the model wants to retry once.
    Threshold is 3 by default."""
    guard = ToolLoopGuard()
    assert guard.check("list_files", {"path": "."}) is None
    assert guard.check("list_files", {"path": "."}) is None
    # A third call with DIFFERENT args breaks the consecutive streak.
    assert guard.check("list_files", {"path": "deile/"}) is None


def test_guard_args_hash_is_order_insensitive():
    """A call with the same kwargs in different dict order should be the same hash."""
    h1 = args_hash_for("write_file", {"path": "/tmp/x", "content": "abc"})
    h2 = args_hash_for("write_file", {"content": "abc", "path": "/tmp/x"})
    assert h1 == h2

    # A different value MUST give a different hash.
    h3 = args_hash_for("write_file", {"path": "/tmp/x", "content": "xyz"})
    assert h1 != h3


def test_guard_sliding_window_catches_interleaved_repeats():
    """The pattern 'A, A, B, A' has 3 As inside a window of 4 — should trip."""
    guard = ToolLoopGuard()
    assert guard.check("list_files", {"path": "."}) is None
    assert guard.check("list_files", {"path": "."}) is None
    # B breaks the consecutive streak so rule 2 should NOT fire.
    assert guard.check("bash_execute", {"cmd": "ls"}) is None
    # The next A makes 3-of-4 in the window — sliding window trips.
    abort = guard.check("list_files", {"path": "."})
    assert abort is not None
    assert abort.kind is AbortKind.SLIDING_WINDOW
    assert abort.repeat_count >= 3


def test_guard_no_progress_trips_after_streak():
    """If the executor reports N consecutive empty/error results, the next call
    must be aborted — without needing identical args. Default threshold is 6."""
    guard = ToolLoopGuard()
    # Six different no-progress results from six different calls.
    for i in range(6):
        assert guard.check(f"tool_{i}", {"i": i}) is None
        guard.record_result(made_progress=False)
    # The seventh attempt — even with new args — must abort because the model
    # has been making no progress for 6 calls in a row.
    abort = guard.check("yet_another_tool", {"i": 6})
    assert abort is not None
    assert abort.kind is AbortKind.NO_PROGRESS
    assert abort.repeat_count == 6


def test_guard_no_progress_resets_on_progress():
    """A single progress result resets the no-progress counter."""
    guard = ToolLoopGuard()
    for i in range(5):
        guard.check(f"t_{i}", {})
        guard.record_result(made_progress=False)
    # Reset
    guard.check("t_progress", {})
    guard.record_result(made_progress=True)
    # Three more no-progress results — well below the threshold of 6.
    for i in range(3):
        guard.check(f"u_{i}", {})
        guard.record_result(made_progress=False)
    # Should still be allowed.
    assert guard.check("u_final", {}) is None


def test_guard_hard_ceiling():
    """The hard call ceiling defaults to 50 — set it lower for the test."""
    guard = ToolLoopGuard(max_calls=4)
    assert guard.check("a", {"i": 1}) is None
    assert guard.check("b", {"i": 2}) is None
    assert guard.check("c", {"i": 3}) is None
    assert guard.check("d", {"i": 4}) is None
    abort = guard.check("e", {"i": 5})
    assert abort is not None
    assert abort.kind is AbortKind.MAX_CALLS


def test_guard_disabled_via_env(monkeypatch):
    """The escape hatch must let every call through, no matter how repetitive."""
    monkeypatch.setenv("DEILE_LOOP_GUARD_DISABLE", "1")
    guard = ToolLoopGuard()
    for _ in range(100):
        assert guard.check("list_files", {"path": "."}) is None
    assert guard.aborted_reason is None


def test_guard_thresholds_via_env(monkeypatch):
    """When repeat threshold is bumped, the rule fires later. We also bump
    the window threshold/size so it doesn't fire first and steal the abort."""
    monkeypatch.setenv("DEILE_LOOP_GUARD_REPEAT_THRESHOLD", "5")
    monkeypatch.setenv("DEILE_LOOP_GUARD_WINDOW_SIZE", "10")
    monkeypatch.setenv("DEILE_LOOP_GUARD_WINDOW_THRESHOLD", "8")
    guard = ToolLoopGuard()
    assert guard.repeat_threshold == 5
    # Four identical calls must NOT trip when threshold is 5.
    for _ in range(4):
        assert guard.check("x", {"y": 1}) is None
    abort = guard.check("x", {"y": 1})
    assert abort is not None
    assert abort.kind is AbortKind.IDENTICAL_REPEAT
    assert abort.repeat_count == 5


def test_guard_window_threshold_clamped_to_window_size(monkeypatch):
    """If the user makes the window threshold larger than the window itself,
    the constructor must clamp it so the rule remains triggerable."""
    monkeypatch.setenv("DEILE_LOOP_GUARD_WINDOW_SIZE", "3")
    monkeypatch.setenv("DEILE_LOOP_GUARD_WINDOW_THRESHOLD", "10")
    guard = ToolLoopGuard()
    assert guard.window_size == 3
    assert guard.window_threshold == 3


def test_user_message_mentions_kind_and_tool():
    """The user-facing string must name the tool and the kind so the operator
    can figure out where to look."""
    guard = ToolLoopGuard()
    guard.check("list_files", {"path": "."})
    guard.check("list_files", {"path": "."})
    abort = guard.check("list_files", {"path": "."})
    assert abort is not None
    msg = abort.user_message()
    assert "list_files" in msg
    formatted = format_loop_break_message(abort)
    assert AbortKind.IDENTICAL_REPEAT.value in formatted


def test_user_message_hints_bash_when_path_tool_loops_outside_cwd():
    """Regression guard for the second-/EVOLVE-run trace.

    When the model loops on a path-tool with arguments that look like they
    target OUTSIDE the working directory (leading slash, ``..``, ``~``),
    the loop-break message must point at ``bash_execute`` — that's the only
    DEILE tool with no working_directory sandbox.

    Without this hint, the model receives the loop-break, "rephrases", and
    keeps trying the same family of broken paths because nothing told it
    the sandbox is the actual blocker. With the hint, the next iteration
    has explicit guidance to switch tools.
    """
    guard = ToolLoopGuard()
    guard.check("list_files", {"path": "/Users/x/parent_repo/.github"})
    guard.check("list_files", {"path": "/Users/x/parent_repo/.github"})
    abort = guard.check("list_files", {"path": "/Users/x/parent_repo/.github"})
    assert abort is not None
    msg = abort.user_message()
    assert "bash_execute" in msg
    assert "OUTSIDE the project working" in msg


def test_user_message_hints_bash_for_parent_relative_path_loops():
    """Same as above but for ``../parent/...`` paths (parent-relative form
    the user typically uses when redirecting from a subproject)."""
    guard = ToolLoopGuard()
    guard.check("read_file", {"file_path": "../parent_repo/README.md"})
    guard.check("read_file", {"file_path": "../parent_repo/README.md"})
    abort = guard.check("read_file", {"file_path": "../parent_repo/README.md"})
    assert abort is not None
    assert "bash_execute" in abort.user_message()


def test_user_message_no_bash_hint_for_clean_relative_path_loops():
    """When the loop is on a project-relative path (no leading /, no ..,
    no ~), the bash hint would mislead the model — clean-relative paths
    that loop usually mean "the file you keep listing doesn't exist", not
    "use a different tool". Stay quiet on the bash hint there."""
    guard = ToolLoopGuard()
    guard.check("list_files", {"path": "src/components"})
    guard.check("list_files", {"path": "src/components"})
    abort = guard.check("list_files", {"path": "src/components"})
    assert abort is not None
    assert "bash_execute" not in abort.user_message()


def test_user_message_no_bash_hint_for_non_path_tool_loops():
    """The bash failover hint applies only to file/path tools. A loop on
    e.g. ``http_get`` shouldn't suggest bash — that's a different family
    of failures (network, auth) where bash isn't the answer."""
    guard = ToolLoopGuard()
    guard.check("http_get", {"url": "https://example.com/api"})
    guard.check("http_get", {"url": "https://example.com/api"})
    abort = guard.check("http_get", {"url": "https://example.com/api"})
    assert abort is not None
    assert "bash_execute" not in abort.user_message()


def test_tool_result_made_progress_helper():
    assert tool_result_made_progress(
        ToolResult(status=ToolStatus.SUCCESS, data={"x": 1})
    ) is True
    assert tool_result_made_progress(
        ToolResult(status=ToolStatus.SUCCESS, data=[])
    ) is False
    assert tool_result_made_progress(
        ToolResult(status=ToolStatus.SUCCESS, data="")
    ) is False
    assert tool_result_made_progress(
        ToolResult(status=ToolStatus.ERROR, message="boom")
    ) is False
    # SUCCESS with non-empty message but empty data still counts as progress.
    assert tool_result_made_progress(
        ToolResult(status=ToolStatus.SUCCESS, data=None, message="ok")
    ) is True


def test_guard_repeat_idempotent_on_same_hash():
    """If we hit the same loop signature twice in a row (e.g. the caller
    forgot to break), the guard must keep returning the same reason — but
    the audit log must NOT re-fire. This is checked indirectly: we just
    verify .check returns abort consistently."""
    guard = ToolLoopGuard()
    for _ in range(2):
        guard.check("x", {})
    abort1 = guard.check("x", {})
    abort2 = guard.check("x", {})
    assert abort1 is not None
    assert abort2 is not None
    assert abort1.args_hash == abort2.args_hash


# ---------------------------------------------------------------------------
# Integration tests — guard inside ToolLoopExecutor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_breaks_on_identical_repeat_under_six_iterations():
    """A provider that always asks for list_files(path='.') with identical args
    must be stopped well before the 30-iteration cap — 3rd call should abort."""
    one_round = _tool_use_round("t1", "list_files", {"path": "."})
    provider = FakeProvider(iterations=[list(one_round) for _ in range(30)])
    executor = ToolLoopExecutor(
        tool_registry=FakeRegistry(
            default=ToolResult(status=ToolStatus.SUCCESS, data=["a", "b"], message="ok"),
        ),
        max_iterations=30,
    )
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="x")], tools=[]
        )
    ]
    # Provider was called fewer than 6 times — guard tripped early.
    assert len(provider.seen_messages) <= 6, (
        f"executor invoked provider {len(provider.seen_messages)} times — "
        "guard should have aborted earlier"
    )
    # Loop-break TOOL_RESULT and a TEXT_DELTA explaining the abort were emitted.
    loop_break_results = [
        e
        for e in events
        if e.type is StreamEventType.TOOL_RESULT
        and e.tool_metadata
        and e.tool_metadata.get("loop_break")
    ]
    assert loop_break_results, "no loop-break TOOL_RESULT emitted"
    loop_break_text = [
        e
        for e in events
        if e.type is StreamEventType.TEXT_DELTA and e.source == "loop_guard"
    ]
    assert loop_break_text, "no loop_guard TEXT_DELTA emitted"
    assert "list_files" in loop_break_text[0].text


@pytest.mark.asyncio
async def test_executor_does_not_break_on_distinct_calls():
    """30 different tool calls in a row must NOT trigger the guard."""
    iterations = [
        _tool_use_round(f"t{i}", f"distinct_tool_{i}", {"i": i}) for i in range(20)
    ]
    # A final iteration with no tool calls so the loop terminates cleanly.
    iterations.append(
        [UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="done")]
    )
    provider = FakeProvider(iterations=iterations)
    executor = ToolLoopExecutor(
        tool_registry=FakeRegistry(
            default=ToolResult(
                status=ToolStatus.SUCCESS,
                data={"unique": True},
                message="ok",
            )
        ),
        max_iterations=30,
    )
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="x")], tools=[]
        )
    ]
    loop_break_results = [
        e
        for e in events
        if e.type is StreamEventType.TOOL_RESULT
        and e.tool_metadata
        and e.tool_metadata.get("loop_break")
    ]
    assert loop_break_results == [], "guard tripped on legitimately-distinct calls"
    # 21 provider invocations: 20 tool rounds + 1 final no-op.
    assert len(provider.seen_messages) == 21


@pytest.mark.asyncio
async def test_executor_breaks_on_sliding_window():
    """list_files, list_files, bash_execute, list_files → sliding window trips."""
    # Distinct call ids per round so the events look authentic.
    iterations = [
        _tool_use_round("a1", "list_files", {"path": "."}),
        _tool_use_round("a2", "list_files", {"path": "."}),
        _tool_use_round("b1", "bash_execute", {"command": "ls"}),
        _tool_use_round("a3", "list_files", {"path": "."}),
        _tool_use_round("a4", "list_files", {"path": "."}),
        # Padding rounds in case the guard somehow doesn't trip.
        *[_tool_use_round(f"z{i}", "noop", {"i": i}) for i in range(10)],
    ]
    provider = FakeProvider(iterations=iterations)
    executor = ToolLoopExecutor(
        tool_registry=FakeRegistry(
            default=ToolResult(status=ToolStatus.SUCCESS, data="ok", message="ok"),
        ),
        max_iterations=30,
    )
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="x")], tools=[]
        )
    ]
    loop_break_results = [
        e
        for e in events
        if e.type is StreamEventType.TOOL_RESULT
        and e.tool_metadata
        and e.tool_metadata.get("loop_break")
    ]
    assert loop_break_results, "sliding window did not trip"
    # Either kind is acceptable here — the ordering of rule checks decides
    # which one fires first (identical-repeat may catch the 4th in a row
    # before sliding-window does). Both prove the loop was broken.
    assert loop_break_results[0].tool_metadata["loop_break_kind"] in (
        AbortKind.IDENTICAL_REPEAT.value,
        AbortKind.SLIDING_WINDOW.value,
    )


@pytest.mark.asyncio
async def test_executor_bash_fail_then_listfiles_loop_user_screenshot_case():
    """The exact symptom from the user's screenshot:
    bash_execute(pkill -f deile) fails, then list_files(path='.') is called
    repeatedly with identical args. The guard MUST break the loop."""
    iterations = [
        _tool_use_round("b1", "bash_execute", {"command": "pkill -f deile"}),
        _tool_use_round("l1", "list_files", {"path": "."}),
        _tool_use_round("l2", "list_files", {"path": "."}),
        _tool_use_round("l3", "list_files", {"path": "."}),
        # Many more identical rounds — the guard MUST stop us before reaching them.
        *[_tool_use_round(f"l{i}", "list_files", {"path": "."}) for i in range(4, 30)],
    ]
    provider = FakeProvider(iterations=iterations)

    registry = FakeRegistry(
        results={
            "bash_execute": ToolResult(
                status=ToolStatus.ERROR,
                message="exit 1",
                error=RuntimeError("pkill: no matching processes"),
            ),
            "list_files": ToolResult(
                status=ToolStatus.SUCCESS,
                data=["deile.py", "README.md"],
                message="ok",
            ),
        },
    )
    executor = ToolLoopExecutor(tool_registry=registry, max_iterations=30)
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="kill all deile processes")],
            tools=[],
        )
    ]
    # Exactly: bash_execute (fail) + list_files x2 succeeded + 3rd list_files
    # tripped the guard before execution. Total registry runs = 3 (bash + 2 list).
    assert "bash_execute" in registry.seen
    assert registry.seen.count("list_files") == 2, (
        f"registry should have run list_files twice, got: {registry.seen}"
    )
    loop_break_results = [
        e
        for e in events
        if e.type is StreamEventType.TOOL_RESULT
        and e.tool_metadata
        and e.tool_metadata.get("loop_break")
    ]
    assert loop_break_results, "guard did not abort the bash-fail-then-loop case"
    assert loop_break_results[0].tool_name == "list_files"


@pytest.mark.asyncio
async def test_executor_emits_audit_event_on_loop_break():
    """The guard MUST log a SUSPICIOUS_ACTIVITY audit event when it breaks."""
    audit = get_audit_logger()
    # Snapshot the count BEFORE the test so concurrent prior events don't pollute.
    before = sum(
        1
        for e in audit.recent_events
        if e.event_type is AuditEventType.SUSPICIOUS_ACTIVITY
    )

    one_round = _tool_use_round("t1", "list_files", {"path": "."})
    provider = FakeProvider(iterations=[list(one_round) for _ in range(10)])
    executor = ToolLoopExecutor(
        tool_registry=FakeRegistry(
            default=ToolResult(
                status=ToolStatus.SUCCESS, data=["a"], message="ok"
            )
        ),
        max_iterations=10,
    )
    async for _ in executor.run(
        provider, [ModelMessage(role="user", content="x")], tools=[]
    ):
        pass

    after = sum(
        1
        for e in audit.recent_events
        if e.event_type is AuditEventType.SUSPICIOUS_ACTIVITY
    )
    assert after > before, "no SUSPICIOUS_ACTIVITY audit event recorded"

    matching = [
        e
        for e in audit.recent_events
        if e.event_type is AuditEventType.SUSPICIOUS_ACTIVITY
        and e.tool_name == "list_files"
        and e.severity is SeverityLevel.WARNING
    ]
    assert matching, "SUSPICIOUS_ACTIVITY entry missing tool_name/severity context"
    # Verify the structured details payload carries the loop signature.
    last = matching[-1]
    assert "args_hash" in last.details
    assert last.details["kind"] == AbortKind.IDENTICAL_REPEAT.value


@pytest.mark.asyncio
async def test_executor_breaks_on_no_progress_streak():
    """If every tool keeps returning errors and the model keeps calling, the
    guard's no-progress rule must fire even when args are all distinct."""
    # Six distinct tool calls in a row — each returns an error.
    iterations = [_tool_use_round(f"t{i}", f"tool_{i}", {"i": i}) for i in range(20)]
    provider = FakeProvider(iterations=iterations)

    class AlwaysErroring:
        def __init__(self):
            self.seen: List[str] = []

        async def execute_tool(self, name, ctx):
            self.seen.append(name)
            return ToolResult(status=ToolStatus.ERROR, message=f"{name} failed")

    registry = AlwaysErroring()
    executor = ToolLoopExecutor(tool_registry=registry, max_iterations=20)
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="x")], tools=[]
        )
    ]
    # No-progress streak default is 6 → after the 6th call returning an error,
    # the 7th call MUST be aborted before execution.
    assert len(registry.seen) <= 7, (
        f"registry executed {len(registry.seen)} tools — guard should have aborted "
        "after the no-progress threshold (6)"
    )
    loop_break_results = [
        e
        for e in events
        if e.type is StreamEventType.TOOL_RESULT
        and e.tool_metadata
        and e.tool_metadata.get("loop_break")
    ]
    assert loop_break_results, "no-progress streak did not trigger an abort"


@pytest.mark.asyncio
async def test_executor_max_calls_ceiling(monkeypatch):
    """When the hard call ceiling is set very low via env, the executor stops."""
    monkeypatch.setenv("DEILE_LOOP_GUARD_MAX_CALLS", "5")
    # The guard must override the executor's iteration cap (which counts
    # iterations, not individual calls). Use distinct args so identical-repeat
    # doesn't fire — only MAX_CALLS should.
    iterations = [
        _tool_use_round(f"t{i}", f"tool_{i}", {"i": i}) for i in range(20)
    ]
    provider = FakeProvider(iterations=iterations)
    executor = ToolLoopExecutor(
        tool_registry=FakeRegistry(
            default=ToolResult(
                status=ToolStatus.SUCCESS, data={"unique": True}, message="ok"
            )
        ),
        max_iterations=20,
    )
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="x")], tools=[]
        )
    ]
    loop_break_results = [
        e
        for e in events
        if e.type is StreamEventType.TOOL_RESULT
        and e.tool_metadata
        and e.tool_metadata.get("loop_break")
    ]
    assert loop_break_results, "MAX_CALLS ceiling did not trigger"
    assert loop_break_results[0].tool_metadata["loop_break_kind"] == AbortKind.MAX_CALLS.value


def test_make_guard_returns_independent_instances():
    """Each turn MUST get a fresh guard — sharing one would leak state."""
    g1 = make_guard()
    g2 = make_guard()
    assert g1 is not g2
    g1.check("x", {})
    assert len(g1.history) == 1
    assert len(g2.history) == 0


# ---------------------------------------------------------------------------
# Tier-2 tests — HARD_STOP escalation + error_signature fast-trip (issue #149)
# ---------------------------------------------------------------------------


def test_guard_escalates_to_hard_stop_on_same_hash_after_abort():
    """Reproduce the run-2 failure mode: guard aborts on iteration N with
    IDENTICAL_REPEAT; LLM 'rephrases' with the SAME arguments on iteration
    N+1; guard must return HARD_STOP immediately instead of the same soft
    abort reason, so the executor knows to end the turn.

    Sequence:
      call 1: list_files(path="/Users/x/.github") — ok, add to history
      call 2: list_files(path="/Users/x/.github") — ok
      call 3: list_files(path="/Users/x/.github") — IDENTICAL_REPEAT (threshold=3)
      call 4: list_files(path="/Users/x/.github") — HARD_STOP (same hash, guard
              already aborted on it)
    """
    guard = make_guard()
    guard.repeat_threshold = 3
    args = {"path": "/Users/x/.github/ISSUE_TEMPLATE/"}

    r1 = guard.check("list_files", args)
    assert r1 is None

    r2 = guard.check("list_files", args)
    assert r2 is None

    r3 = guard.check("list_files", args)
    assert r3 is not None
    assert r3.kind is AbortKind.IDENTICAL_REPEAT

    r4 = guard.check("list_files", args)
    assert r4 is not None
    assert r4.kind is AbortKind.HARD_STOP, (
        f"Expected HARD_STOP on 4th call with same hash, got {r4.kind}"
    )
    assert "HARD STOP" in r4.user_message() or "hard" in r4.user_message().lower()


def test_guard_hard_stop_message_contains_tool_family_hint():
    """HARD_STOP user_message() must mention switching to a different tool
    so the LLM has a concrete next action."""
    guard = make_guard()
    guard.repeat_threshold = 2
    args = {"path": "/etc/hosts"}

    guard.check("read_file", args)
    r = guard.check("read_file", args)
    assert r is not None
    assert r.kind is AbortKind.IDENTICAL_REPEAT

    r2 = guard.check("read_file", args)
    assert r2 is not None
    assert r2.kind is AbortKind.HARD_STOP
    msg = r2.user_message()
    assert "bash_execute" in msg or "switch" in msg.lower() or "tool" in msg.lower()


def test_guard_record_result_error_signature_fast_trips_no_progress():
    """When record_result is called with the same error_signature twice in a
    row, the guard must fast-trip NO_PROGRESS on the very next check() call,
    even though the normal no_progress_threshold (6) hasn't been reached.

    Timeline: check()/record_result×2 same-sig → _consecutive_empty reaches
    threshold → check() #3 returns NO_PROGRESS abort.  Two record_result
    failures with the same signature are sufficient; a third call is not
    needed.  This is intentional — structural errors (same path, same
    rejection) should be short-circuited well before the generic threshold.
    """
    guard = make_guard()
    guard.no_progress_threshold = 6
    sig = "path_not_found:/Users/x/.github/ISSUE_TEMPLATE/"

    r1 = guard.check("list_files", {"path": "/a"})
    assert r1 is None
    guard.record_result(made_progress=False, error_signature=sig)

    r2 = guard.check("list_files", {"path": "/b"})
    assert r2 is None
    guard.record_result(made_progress=False, error_signature=sig)

    r3 = guard.check("list_files", {"path": "/c"})
    assert r3 is not None
    assert r3.kind is AbortKind.NO_PROGRESS, (
        f"Expected NO_PROGRESS fast-trip after 2 same-sig errors, got {r3.kind}"
    )


def test_guard_error_signature_resets_on_progress():
    """A successful call must reset the error_signature streak so a later
    failure with the same signature starts a new streak."""
    guard = make_guard()
    guard.no_progress_threshold = 6
    sig = "path_not_found:/x"

    guard.check("list_files", {"path": "/a"})
    guard.record_result(made_progress=False, error_signature=sig)

    guard.check("list_files", {"path": "/b"})
    guard.record_result(made_progress=True)

    guard.check("list_files", {"path": "/c"})
    guard.record_result(made_progress=False, error_signature=sig)
    assert guard._consecutive_same_error == 1

    r4 = guard.check("list_files", {"path": "/d"})
    assert r4 is None, "Should not abort after progress reset the streak"
