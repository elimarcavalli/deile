"""AgentBridge — invoke DEILE in-process or via oneshot subprocess.

Two implementations:
- InProcessAgentBridge: keeps a DeileAgent and uses session_id == bot_session_<bot_user_id>
- OneshotSubprocessAgentBridge: runs `python3 deile.py "<msg>"` per call (isolated)

Both honor `extra_system_prompt` and `bot_context` injection points.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Mapping, Optional, Tuple

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.capabilities import CapabilitySnapshot
from deile_bot.foundation.conversation_store import StoredMessage
from deile_bot.foundation.envelope import Attachment
from deile_bot.foundation.exceptions import (AgentInvocationError,
                                             AgentInvocationTimeout)
from deile_bot.foundation.logging import get_logger
from deile_bot.foundation.settings import FoundationSettings

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _content_to_text(content: Any) -> str:
    """Render agent content to text, including Rich renderables."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        from io import StringIO
        from rich.console import Console

        buffer = StringIO()
        Console(
            file=buffer,
            force_terminal=False,
            no_color=True,
            width=120,
            highlight=False,
        ).print(content)
        return buffer.getvalue().strip()
    except Exception:
        return str(content)


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    ok: bool
    elapsed_ms: int
    args_preview: str = ""


@dataclass(frozen=True)
class AgentInvocation:
    bot_user_id: str
    persona: str
    forced_model: Optional[str]
    inbound_text: str
    default_model: Optional[str] = None
    inbound_attachments: Tuple[Attachment, ...] = ()
    history: List[StoredMessage] = field(default_factory=list)
    capabilities: Optional[CapabilitySnapshot] = None
    extra_system_prompt: str = ""
    bot_context: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 120
    session_id: Optional[str] = None


@dataclass(frozen=True)
class AgentResponse:
    text: str
    markup: MarkupAST
    tool_calls: Tuple[ToolCallRecord, ...] = ()
    elapsed_ms: int = 0
    model_used: str = ""
    truncated: bool = False


class AgentBridge(ABC):
    @abstractmethod
    async def invoke(self, inv: AgentInvocation) -> AgentResponse: ...


def _session_id_for(inv: AgentInvocation) -> str:
    if inv.session_id:
        return inv.session_id
    return f"bot_session_{inv.bot_user_id}"


def _sanitize_extra_prompt(prompt: str) -> str:
    """Defense-in-depth: strip injection-y tags like </system> from extra prompt."""
    if not prompt:
        return ""
    blocked = ("</system>", "<persona_override>", "</persona_override>", "<system>")
    out = prompt
    for tag in blocked:
        out = out.replace(tag, "")
    return out


class InProcessAgentBridge(AgentBridge):
    """Invoke DeileAgent in-process; session per bot_user_id (persisted opt-in)."""

    def __init__(
        self,
        agent_provider: Callable[[], Awaitable[Any]],
        *,
        persisted_sessions: bool = True,
    ):
        self._agent_provider = agent_provider
        self._persisted = persisted_sessions
        self._logger = get_logger("agent_bridge.in_process")

    async def invoke(self, inv: AgentInvocation) -> AgentResponse:
        agent = await self._agent_provider()
        session_id = _session_id_for(inv)
        if hasattr(agent, "get_or_create_session"):
            try:
                await agent.get_or_create_session(session_id, persisted=self._persisted)
            except Exception:
                self._logger.warning("get_or_create_session failed; falling back", exc_info=True)
        elif session_id not in getattr(agent, "_sessions", {}):
            try:
                agent.create_session(session_id)
            except Exception:
                pass

        sanitized = _sanitize_extra_prompt(inv.extra_system_prompt)
        kwargs: dict = {"session_id": session_id}
        if sanitized:
            kwargs["extra_system_prompt"] = sanitized
        if inv.bot_context:
            kwargs["bot_context"] = dict(inv.bot_context)

        # Bot settings support both a security lock and a soft default:
        # - forced_model: admin lock; /model use must not override it.
        # - default_model: preference only when the user has not chosen a model.
        try:
            session = agent.get_session(session_id)
            if session is not None:
                if inv.forced_model:
                    session.context_data["forced_model"] = inv.forced_model
                    session.context_data["model_override_locked"] = True
                    session.context_data["model_override_lock_source"] = "deile_bot"
                else:
                    if session.context_data.get("model_override_lock_source") == "deile_bot":
                        session.context_data.pop("forced_model", None)
                        session.context_data.pop("model_override_locked", None)
                        session.context_data.pop("model_override_lock_source", None)
                    if inv.default_model:
                        session.context_data["preferred_model"] = inv.default_model
                    else:
                        session.context_data.pop("preferred_model", None)
                    session.context_data.pop("_bot_forced_model", None)
        except Exception:
            pass

        start = time.monotonic()
        try:
            response = await asyncio.wait_for(
                agent.process_input(inv.inbound_text, **kwargs),
                timeout=inv.timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            raise AgentInvocationTimeout(
                f"agent timed out after {inv.timeout_seconds}s",
                context={"bot_user_id": inv.bot_user_id},
            ) from e
        except Exception as e:  # noqa: BLE001 — bridge protects pipeline from agent errors
            raise AgentInvocationError(
                f"agent failed: {type(e).__name__}: {e}",
                context={"bot_user_id": inv.bot_user_id},
            ) from e
        elapsed_ms = int((time.monotonic() - start) * 1000)
        text = _content_to_text(getattr(response, "content", ""))
        metadata = getattr(response, "metadata", {}) or {}
        return AgentResponse(
            text=text,
            markup=MarkupAST.from_plain(text),
            tool_calls=(),
            elapsed_ms=elapsed_ms,
            model_used=metadata.get("model_used") or metadata.get("model") or "",
        )


class OneshotSubprocessAgentBridge(AgentBridge):
    """Run a fresh `python3 deile.py "<msg>"` per call. No session persistence."""

    def __init__(self, deile_py_path: Path = Path("deile.py")):
        self._path = Path(deile_py_path)
        self._logger = get_logger("agent_bridge.oneshot")

    async def invoke(self, inv: AgentInvocation) -> AgentResponse:
        cmd: List[str] = [sys.executable, str(self._path)]
        if inv.forced_model:
            cmd += ["--model", inv.forced_model]
        cmd += ["--", inv.inbound_text]
        env = os.environ.copy()
        if inv.default_model:
            env["DEILE_PREFERRED_MODEL"] = inv.default_model
        if inv.extra_system_prompt:
            env["DEILE_EXTRA_SYSTEM_PROMPT"] = _sanitize_extra_prompt(inv.extra_system_prompt)
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(Path.cwd()),
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=inv.timeout_seconds
                )
            except asyncio.TimeoutError as e:
                proc.kill()
                await proc.wait()
                raise AgentInvocationTimeout(
                    "subprocess timeout",
                    context={"cmd": shlex.join(cmd)},
                ) from e
        except FileNotFoundError as e:
            raise AgentInvocationError("deile.py not found", context={"path": str(self._path)}) from e
        except Exception as e:  # noqa: BLE001
            raise AgentInvocationError(
                f"subprocess failed: {type(e).__name__}: {e}",
                context={"cmd": shlex.join(cmd)},
            ) from e
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if proc.returncode != 0:
            raise AgentInvocationError(
                f"subprocess exit={proc.returncode}",
                context={"stderr": _strip_ansi(stderr_b.decode("utf-8", "replace"))[:500]},
            )
        text = _strip_ansi(stdout_b.decode("utf-8", "replace")).strip()
        return AgentResponse(
            text=text,
            markup=MarkupAST.from_plain(text),
            elapsed_ms=elapsed_ms,
            model_used="",
        )


def build_agent_bridge(
    settings: FoundationSettings,
    *,
    agent_provider: Optional[Callable[[], Awaitable[Any]]] = None,
) -> AgentBridge:
    if settings.agent_bridge_mode == "oneshot_subprocess":
        return OneshotSubprocessAgentBridge()
    if agent_provider is None:
        raise AgentInvocationError(
            "in_process bridge requires an agent_provider", context={}
        )
    return InProcessAgentBridge(agent_provider)
