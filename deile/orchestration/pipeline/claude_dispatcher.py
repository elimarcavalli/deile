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
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

from deile.orchestration.pipeline.constants import (CLAUDE_TIMEOUT_SECONDS,
                                                    ISSUE_BODY_MAX_CHARS)

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
        timeout_seconds: int = CLAUDE_TIMEOUT_SECONDS,
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

        loop = asyncio.get_event_loop()
        start = loop.time()
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
            duration = loop.time() - start
            return ClaudeRunResult(
                returncode=124,
                stdout="",
                stderr=f"claude -p timed out after {self.timeout_seconds}s",
                duration_seconds=duration,
                cmd=tuple(cmd),
            )
        duration = loop.time() - start
        return ClaudeRunResult(
            returncode=proc.returncode or 0,
            stdout=stdout_b.decode("utf-8", "replace"),
            stderr=stderr_b.decode("utf-8", "replace"),
            duration_seconds=duration,
            cmd=tuple(cmd),
        )


# ---------------------------------------------------------------------------
# Canonical prompt templates
# ---------------------------------------------------------------------------

IMPLEMENT_PROMPT_TEMPLATE = textwrap.dedent(
    """\
    Você está numa worktree isolada (.worktrees/<branch>) do repositório {repo}.
    Sua tarefa: pegar a issue #{number} ({title}), implementar a feature seguindo
    o workflow do projeto, criar testes para todos os casos de uso, rodar tudo
    com 100% de aprovação e abrir uma Pull Request com branch já criado.

    Restrições:
    - NÃO troque de diretório — você JÁ está no worktree certo.
    - Faça commits pequenos e atômicos.
    - Adicione testes ANTES de finalizar; rode `pytest` e ajuste até 100% pass.
    - Push do branch e abra PR via `gh pr create`. Use o título e corpo
      coerentes com a issue. Adicione `Closes #{number}` no corpo.
    - Quando terminar, responda EXATAMENTE com a URL da PR aberta numa única
      linha (ex: https://github.com/{repo}/pull/N). Sem prosa adicional.

    Contexto da issue:
    {issue_body}
    """
)


REVIEW_PROMPT_TEMPLATE = textwrap.dedent(
    """\
    Você está numa worktree isolada do repositório {repo} (PR #{number}: {title}).
    Sua tarefa: revisar a PR, corrigir o que estiver errado, cobrir todos os
    casos de uso com testes, garantir 100% de aprovação dos testes, documentar
    correções no corpo da PR e dar merge quando estiver pronto pra produção.

    Restrições:
    - NÃO faça force-push.
    - Use `gh pr review --approve` apenas após todos os checks passarem.
    - Use `gh pr merge --merge` (não squash) e respeite a config do repo.
    - Quando terminar, responda EXATAMENTE com a URL da PR mergeada numa
      única linha (ex: https://github.com/{repo}/pull/N). Sem prosa adicional.
    """
)


def render_implement_prompt(repo: str, number: int, title: str, issue_body: str) -> str:
    return IMPLEMENT_PROMPT_TEMPLATE.format(
        repo=repo,
        number=number,
        title=title,
        issue_body=issue_body.strip()[:ISSUE_BODY_MAX_CHARS],
    )


def render_review_prompt(repo: str, number: int, title: str) -> str:
    return REVIEW_PROMPT_TEMPLATE.format(repo=repo, number=number, title=title)
