"""Tests for the ``/reasoning`` slash command (DEILE CLI reasoning config)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deile.commands.builtin.reasoning_command import ReasoningCommand


def _ctx(args: str, context_data=None):
    session = SimpleNamespace(
        context_data=context_data if context_data is not None else {}
    )
    return SimpleNamespace(session=session, args=args, raw_args=args)


@pytest.mark.unit
async def test_show_when_no_args():
    cmd = ReasoningCommand()
    res = await cmd.execute(_ctx(""))
    assert res.success is True


@pytest.mark.unit
async def test_set_valid_level():
    cmd = ReasoningCommand()
    ctx = _ctx("high")
    res = await cmd.execute(ctx)
    assert res.success is True
    assert ctx.session.context_data["reasoning_effort"] == "high"


@pytest.mark.unit
async def test_set_normalizes_case():
    cmd = ReasoningCommand()
    ctx = _ctx("ULTRACODE")
    res = await cmd.execute(ctx)
    assert res.success is True
    assert ctx.session.context_data["reasoning_effort"] == "ultracode"


@pytest.mark.unit
async def test_invalid_level_rejected():
    cmd = ReasoningCommand()
    ctx = _ctx("bogus")
    res = await cmd.execute(ctx)
    assert res.success is False
    assert "reasoning_effort" not in ctx.session.context_data


@pytest.mark.unit
async def test_clear_removes_override():
    cmd = ReasoningCommand()
    ctx = _ctx("clear", context_data={"reasoning_effort": "high"})
    res = await cmd.execute(ctx)
    assert res.success is True
    assert "reasoning_effort" not in ctx.session.context_data


@pytest.mark.unit
def test_has_effort_alias():
    cmd = ReasoningCommand()
    assert "effort" in (cmd.aliases or [])


# ── /reasoning use <nível> — eixo HARD ────────────────────────────────────


@pytest.mark.unit
async def test_use_set_stores_forced():
    cmd = ReasoningCommand()
    ctx = _ctx("use high")
    res = await cmd.execute(ctx)
    assert res.success is True
    assert ctx.session.context_data["forced_reasoning_effort"] == "high"
    assert "reasoning_effort" not in ctx.session.context_data


@pytest.mark.unit
async def test_use_set_auto_stores_not_clears():
    cmd = ReasoningCommand()
    ctx = _ctx("use auto")
    res = await cmd.execute(ctx)
    assert res.success is True
    assert ctx.session.context_data["forced_reasoning_effort"] == "auto"


@pytest.mark.unit
async def test_use_set_normalizes_case():
    cmd = ReasoningCommand()
    ctx = _ctx("use ULTRACODE")
    res = await cmd.execute(ctx)
    assert res.success is True
    assert ctx.session.context_data["forced_reasoning_effort"] == "ultracode"


@pytest.mark.unit
async def test_use_set_invalid_rejected():
    cmd = ReasoningCommand()
    ctx = _ctx("use bogus")
    res = await cmd.execute(ctx)
    assert res.success is False
    assert "forced_reasoning_effort" not in ctx.session.context_data


@pytest.mark.unit
async def test_use_clear_removes_forced():
    cmd = ReasoningCommand()
    ctx = _ctx(
        "use clear",
        context_data={"forced_reasoning_effort": "high", "reasoning_effort": "low"},
    )
    res = await cmd.execute(ctx)
    assert res.success is True
    assert "forced_reasoning_effort" not in ctx.session.context_data
    # soft slot must be untouched
    assert ctx.session.context_data.get("reasoning_effort") == "low"


@pytest.mark.unit
async def test_use_clear_idempotent():
    cmd = ReasoningCommand()
    ctx = _ctx("use clear", context_data={})
    res = await cmd.execute(ctx)
    assert res.success is True
    assert "forced_reasoning_effort" not in ctx.session.context_data


@pytest.mark.unit
async def test_use_reset_clears_forced():
    cmd = ReasoningCommand()
    ctx = _ctx("use reset", context_data={"forced_reasoning_effort": "max"})
    res = await cmd.execute(ctx)
    assert res.success is True
    assert "forced_reasoning_effort" not in ctx.session.context_data


@pytest.mark.unit
async def test_use_no_target_fails():
    cmd = ReasoningCommand()
    ctx = _ctx("use")
    res = await cmd.execute(ctx)
    assert res.success is False


@pytest.mark.unit
async def test_show_reports_forced_source():
    cmd = ReasoningCommand()
    ctx = _ctx(
        "", context_data={"forced_reasoning_effort": "max", "reasoning_effort": "low"}
    )
    res = await cmd.execute(ctx)
    assert res.success is True
    assert res.metadata.get("source") == "forced (hard)"
    assert res.metadata.get("reasoning_effort") == "max"


@pytest.mark.unit
async def test_show_falls_back_to_soft_when_no_forced():
    cmd = ReasoningCommand()
    ctx = _ctx("", context_data={"reasoning_effort": "low"})
    res = await cmd.execute(ctx)
    assert res.success is True
    assert res.metadata.get("source") == "session (/reasoning)"
    assert res.metadata.get("reasoning_effort") == "low"


@pytest.mark.unit
async def test_soft_clear_does_not_touch_forced():
    cmd = ReasoningCommand()
    ctx = _ctx(
        "clear",
        context_data={"forced_reasoning_effort": "high", "reasoning_effort": "max"},
    )
    res = await cmd.execute(ctx)
    assert res.success is True
    assert "reasoning_effort" not in ctx.session.context_data
    assert ctx.session.context_data.get("forced_reasoning_effort") == "high"
