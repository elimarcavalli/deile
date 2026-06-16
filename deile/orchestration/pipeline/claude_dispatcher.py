"""Programmatic invocation of ``claude -p "<prompt>"`` (Claude Code one-shot).

The autonomous pipeline delegates the actual implementation/review work to
Claude Code via its non-interactive (``-p``/``--print``) mode. This module
encapsulates the subprocess plumbing and prompt templates.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

from deile.orchestration.pipeline.constants import (
    ISSUE_BODY_MAX_CHARS,
    claude_timeout_seconds,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaudeRunResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    cmd: tuple

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ClaudeDispatcher:
    """Run ``claude -p "<prompt>"`` inside a chosen working directory.

    The dispatcher is intentionally narrow: it just executes the command,
    captures stdout/stderr, and surfaces the result. Higher-level decisions
    (which prompt, when to retry, how to interpret the output) live in
    :mod:`monitor`.
    """

    def __init__(
        self,
        *,
        claude_path: Optional[str] = None,
        timeout_seconds: int = claude_timeout_seconds(),
        prefer_subscription_auth: bool = True,
    ) -> None:
        self._claude = claude_path or shutil.which("claude") or "claude"
        self.timeout_seconds = timeout_seconds
        # When True, strip ANTHROPIC_API_KEY (and friends) from the subprocess
        # env so `claude` falls back to the operator's Claude Pro/Max
        # subscription. Most local-dev setups carry an API key in `.env` for
        # the DEILE agent itself, but that key is often on a *different*
        # billing account than the subscription paying for Claude Code.
        self.prefer_subscription_auth = prefer_subscription_auth

    _STRIP_KEYS = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BEARER_TOKEN",
    )

    def _build_env(self, override: Optional[Mapping[str, str]]) -> Optional[dict]:
        """Return the env dict to pass to ``claude``, or None to inherit.

        If ``prefer_subscription_auth`` is True (default), copies the parent
        env minus the keys that would force API-key auth. If False, behaves
        like before: explicit ``override`` wins, otherwise inherits.
        """
        if override is not None:
            return dict(override)
        if not self.prefer_subscription_auth:
            return None
        env = dict(os.environ)
        for k in self._STRIP_KEYS:
            env.pop(k, None)
        return env

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        env: Optional[Mapping[str, str]] = None,
        extra_args: Sequence[str] = (),
    ) -> ClaudeRunResult:
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        if not cwd.exists():
            raise FileNotFoundError(f"cwd does not exist: {cwd}")

        cmd: List[str] = [self._claude, *extra_args, "-p", prompt]
        logger.info("invoking Claude Code: %s in %s", shlex.join(cmd[:3]) + " …", cwd)

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_env(env),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            duration = time.monotonic() - start
            return ClaudeRunResult(
                returncode=124,
                stdout="",
                stderr=f"claude -p timed out after {self.timeout_seconds}s",
                duration_seconds=duration,
                cmd=tuple(cmd),
            )
        duration = time.monotonic() - start
        stderr_text = stderr_b.decode("utf-8", "replace")
        rc = proc.returncode or 0
        if rc != 0 and self.prefer_subscription_auth:
            # When we stripped API keys to prefer subscription auth and the
            # subprocess failed, check for auth-related error messages so we
            # can surface a clear warning instead of leaving the operator
            # guessing about the root cause.
            _auth_hints = (
                "authentication",
                "unauthorized",
                "api key",
                "login",
                "sign in",
                "auth",
            )
            if any(h in stderr_text.lower() for h in _auth_hints):
                logger.warning(
                    "claude -p failed with a possible auth error and "
                    "prefer_subscription_auth=True (ANTHROPIC_API_KEY was stripped). "
                    "If you are not logged in with a Claude Pro/Max subscription, "
                    "set prefer_subscription_auth=False or run `claude login`. "
                    "stderr: %s",
                    stderr_text.strip()[:300],
                )
        return ClaudeRunResult(
            returncode=rc,
            stdout=stdout_b.decode("utf-8", "replace"),
            stderr=stderr_text,
            duration_seconds=duration,
            cmd=tuple(cmd),
        )


# ---------------------------------------------------------------------------
# Canonical prompt templates
# ---------------------------------------------------------------------------

IMPLEMENT_PROMPT_TEMPLATE = textwrap.dedent("""\
    Você está numa worktree isolada (.worktrees/<branch>) do repositório {repo}.
    Sua tarefa: pegar a issue #{number} ({title}), implementar a feature seguindo
    o workflow do projeto, criar testes para todos os casos de uso, rodar tudo
    com 100% de aprovação e abrir uma {pr_noun} com branch já criado.

    Restrições:
    - NÃO troque de diretório — você JÁ está no worktree certo.
    - Faça commits pequenos e atômicos.
    - Adicione testes ANTES de finalizar; rode `pytest` e ajuste até 100% pass.
    - Push do branch e abra {pr_noun} via `{create_cmd}`. Use o título e corpo
      coerentes com a issue. Adicione `{close_keyword} #{number}` no corpo.
    - Quando terminar, responda EXATAMENTE com a URL da {pr_noun} aberta numa única
      linha (ex: {pr_url_pattern}). Sem prosa adicional.

    Contexto da issue:
    {issue_body}
    """)


REVIEW_PROMPT_TEMPLATE = textwrap.dedent("""\
    Você está numa worktree isolada do repositório {repo} ({pr_noun} #{number}: {title}).
    Sua tarefa: revisar a {pr_noun}, corrigir o que estiver errado, cobrir todos os
    casos de uso com testes, garantir 100% de aprovação dos testes, documentar
    correções no corpo da {pr_noun} e dar merge quando estiver pronto pra produção.

    Restrições:
    - NÃO faça force-push.
    - Aprove apenas após todos os checks passarem.
    - Use `{merge_cmd}` (não squash) e respeite a config do repo.
    - Quando terminar, responda EXATAMENTE com a URL da {pr_noun} mergeada numa
      única linha (ex: {pr_url_pattern}). Sem prosa adicional.
    """)


def _default_forge_for_dispatch(repo: str):
    """Build a default GitHub :class:`ForgeConfig` for prompt rendering.

    Used when the caller did not pass an explicit ``forge``. Mirrors the
    fallback used by the brief renderers so legacy ``render_*_prompt(repo,
    ...)`` callers keep getting GH-shaped commands without code changes.
    """
    from deile.orchestration.forge.base import ForgeConfig, ForgeKind

    cli = shutil.which("gh") or "gh"
    return ForgeConfig(
        kind=ForgeKind.GITHUB,
        host="github.com",
        project_path=repo,
        cli_path=cli,
    )


def _resolve_forge_for_prompt(repo: str, forge, *, number: int, branch: str):
    """Resolve ``(cfg, cmds, pr_noun)`` for a prompt renderer.

    Centralises the three-line preamble shared by every ``render_*_prompt``:
    pick the explicit forge or build the GH default, render the per-forge
    CLI snippets, and derive the PR/MR noun. Keeps the import-locals inside
    one helper so each render function is one ``.format()`` call.
    """
    from deile.orchestration.forge.base import ForgeKind
    from deile.orchestration.forge.cli_renderer import render_brief_cmds

    cfg = forge or _default_forge_for_dispatch(repo)
    cmds = render_brief_cmds(cfg, number=number, branch=branch, main="main")
    pr_noun = "PR" if cfg.kind is ForgeKind.GITHUB else "MR"
    return cfg, cmds, pr_noun


def render_implement_prompt(
    repo: str,
    number: int,
    title: str,
    issue_body: str,
    *,
    forge=None,
) -> str:
    cfg, cmds, pr_noun = _resolve_forge_for_prompt(
        repo,
        forge,
        number=number,
        branch="<branch>",
    )
    # ``create_cmd`` is the bare verb the prompt mentions in prose (``gh
    # pr create`` / ``glab mr create``) — not the fully-parameterised
    # snippet, which would over-specify and confuse the agent.
    from deile.orchestration.forge.base import ForgeKind

    create_cmd = "gh pr create" if cfg.kind is ForgeKind.GITHUB else "glab mr create"
    # Spikes deliver measured evidence, not production code — their PR must
    # ``Refs`` (never ``Closes``) the issue, mirroring the pipeline implement
    # brief. This legacy local path (``ClaudeImplementer`` / ``claude -p`` on the
    # host) does not carry the full Definition-of-Done evidence block — that
    # lives in :mod:`deile.orchestration.pipeline.briefs` for the in-cluster
    # worker path — but it shares the same close-keyword safety so a spike run
    # locally never auto-closes a half-proven issue.
    from deile.orchestration.pipeline.briefs import _close_keyword

    close_keyword = _close_keyword(title, issue_body)
    return IMPLEMENT_PROMPT_TEMPLATE.format(
        repo=repo,
        number=number,
        title=title,
        issue_body=issue_body.strip()[:ISSUE_BODY_MAX_CHARS],
        pr_noun=pr_noun,
        create_cmd=create_cmd,
        close_keyword=close_keyword,
        pr_url_pattern=cmds["pr_url_pattern"],
    )


def render_review_prompt(
    repo: str,
    number: int,
    title: str,
    *,
    forge=None,
) -> str:
    _cfg, cmds, pr_noun = _resolve_forge_for_prompt(
        repo,
        forge,
        number=number,
        branch=f"pr/{number}",
    )
    return REVIEW_PROMPT_TEMPLATE.format(
        repo=repo,
        number=number,
        title=title,
        pr_noun=pr_noun,
        merge_cmd=cmds["merge_fallback_cmd"],
        pr_url_pattern=cmds["pr_url_pattern"],
    )
