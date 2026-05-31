"""Tests for the ``/reasoning`` slash command (DEILE CLI reasoning config)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deile.commands.builtin.reasoning_command import ReasoningCommand


def _ctx(args: str, context_data=None):
    session = SimpleNamespace(context_data=context_data if context_data is not None else {})
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
