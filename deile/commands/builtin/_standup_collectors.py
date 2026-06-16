"""Standup data collectors — parsing + git/gh observation.

Extracted from ``standup_command.py`` so the command file owns LLM
dispatch + Rich panel rendering, while these helpers own argument
parsing and collection of commits/PRs/issues.

Pilar 03 §1 (Async-first) + §2 (Hexagonal): commit collection (``git
log``) keeps a synchronous subprocess path wrapped in
``asyncio.to_thread`` inside :func:`collect_standup_data`; PR/issue
collection delegates to :class:`GitHubClient` (the existing pipeline
adapter) instead of running ``gh`` directly — so the command stays in
the domain layer and the gh CLI transport lives in one place.

Git/gh pre-condition gates (`ensure_git_repo`, `ensure_gh_authenticated`)
come from :mod:`._git_helpers` and are re-exported here for tests that
patch the module namespace.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List

from ...core.exceptions import CommandError
from ._git_helpers import ensure_gh_authenticated  # noqa: F401 — re-exported
from ._git_helpers import (
    ensure_git_repo,
)

if TYPE_CHECKING:
    from ...orchestration.pipeline.github_client import GitHubClient


# Parses both HTTPS (``https://github.com/owner/name(.git)?``) and SSH
# (``git@github.com:owner/name(.git)?``) remote URLs to ``owner/name``.
_REMOTE_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9._-]+)/(?P<name>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?\s*$"
)


@dataclass
class StandupData:
    since_spec: str
    since_iso: str
    commits: List[Dict[str, str]] = field(default_factory=list)
    prs: List[Dict[str, Any]] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)


def parse_since(duration: str) -> timedelta:
    if not duration:
        raise CommandError("Duração vazia.")
    match = re.match(r"^\s*(\d+)([hdwHDW])\s*$", duration)
    if not match:
        raise CommandError(
            f"Duração inválida: {duration}. Use formato como 24h, 3d, 1w."
        )
    val, unit = int(match.group(1)), match.group(2).lower()
    if val == 0:
        raise CommandError("Duração não pode ser zero.")
    if unit == "h":
        return timedelta(hours=val)
    elif unit == "d":
        return timedelta(days=val)
    elif unit == "w":
        return timedelta(weeks=val)
    raise CommandError(f"Unidade inválida: {unit}")


def parse_args(args: str) -> str:
    args = args.strip()
    if not args:
        return "24h"
    if args.startswith("--since="):
        return args.split("=", 1)[1].strip()
    if args.startswith("--since "):
        return args.split(" ", 1)[1].strip()
    if args.startswith("--"):
        raise CommandError(f"Flag desconhecida: {args}")
    return "24h"


def _resolve_repo_from_git(*, timeout: int = 10) -> str:
    """Infere ``owner/name`` a partir de ``git remote get-url origin``.

    Aceita URLs HTTPS (``https://github.com/owner/name(.git)?``) e SSH
    (``git@github.com:owner/name(.git)?``). Levanta :class:`CommandError`
    com mensagem PT-BR quando ``git`` falha ou o remote não bate com o
    formato esperado de GitHub.
    """
    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise CommandError(
            "não consegui ler o remote origin (git falhou): " f"{exc}"
        ) from exc

    if res.returncode != 0:
        raise CommandError(
            "não consegui detectar o repo GitHub a partir do remote origin "
            f"(rc={res.returncode}): {(res.stderr or '').strip()}"
        )
    match = _REMOTE_RE.match(res.stdout)
    if not match:
        raise CommandError(
            "não consegui detectar o repo GitHub a partir do remote origin "
            f"(formato não reconhecido): {res.stdout.strip()!r}"
        )
    return f"{match.group('owner')}/{match.group('name')}"


def collect_commits(since_iso: str) -> List[Dict[str, str]]:
    try:
        res = subprocess.run(
            ["git", "log", f"--since={since_iso}", "--format=%h\x1f%an\x1f%s"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []
    if res.returncode != 0:
        return []
    commits = []
    for line in res.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) >= 3:
            commits.append({"hash": parts[0], "author": parts[1], "title": parts[2]})
    return commits


async def collect_prs(client: "GitHubClient", since_iso: str) -> List[Dict[str, Any]]:
    """Delega ao adapter (``GitHubClient.list_prs_updated_since``).

    Pilar 03 §2 (Hexagonal): o command não fala ``gh`` diretamente — o
    transporte vive no :class:`GitHubClient` que já trata erros tipados e
    retorna lista vazia em falha. A normalização (``author`` flatten,
    ``updated_at`` snake_case) acontece dentro do adapter.
    """
    return await client.list_prs_updated_since(since_iso)


async def collect_issues(
    client: "GitHubClient", since_iso: str
) -> List[Dict[str, Any]]:
    """Companion de :func:`collect_prs` para issues."""
    return await client.list_issues_updated_since(since_iso)


async def collect_standup_data(since_spec: str) -> StandupData:
    """Orquestra a coleta de commits + PRs + issues em uma janela.

    Async para acessar os helpers do adapter (PRs/issues) sem aninhar
    ``asyncio.run``. Commits permanecem síncronos (``git log``) e rodam em
    :func:`asyncio.to_thread` para não bloquear o event loop.
    """
    # Pre-condition gates — git/gh disponíveis e autenticados. Disparam
    # subprocess síncrono, então rodam em ``asyncio.to_thread`` (Pilar 03 §1)
    # para não bloquear o event loop.
    await asyncio.to_thread(ensure_git_repo)
    await asyncio.to_thread(ensure_gh_authenticated)

    delta = parse_since(since_spec)
    since_date = datetime.now(timezone.utc) - delta
    since_iso = since_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Late import — evita ciclo de import no carregamento do command package.
    from ...orchestration.pipeline.github_client import GitHubClient

    repo = await asyncio.to_thread(_resolve_repo_from_git)
    client = GitHubClient(repo)

    commits = await asyncio.to_thread(collect_commits, since_iso)
    prs = await collect_prs(client, since_iso)
    issues = await collect_issues(client, since_iso)

    return StandupData(
        since_spec=since_spec,
        since_iso=since_iso,
        commits=commits,
        prs=prs,
        issues=issues,
    )


def build_prompt(data: StandupData) -> str:
    prompt = f"Gere um resumo de standup em PT-BR para as últimas {data.since_spec} (desde {data.since_iso}).\n"
    prompt += "O resumo deve ter no máximo 8 linhas no corpo principal, seguido de bullets de Destaques.\n\n"

    prompt += f"Commits ({len(data.commits)}):\n"
    if not data.commits:
        prompt += "- (nenhum)\n"
    for c in data.commits:
        prompt += f"- {c['hash']} por {c['author']}: {c['title']}\n"

    prompt += f"\nPull Requests ({len(data.prs)}):\n"
    if not data.prs:
        prompt += "- (nenhuma)\n"
    for pr in data.prs:
        prompt += (
            f"- #{pr['number']} [{pr['state']}] por {pr['author']}: {pr['title']}\n"
        )

    prompt += f"\nIssues ({len(data.issues)}):\n"
    if not data.issues:
        prompt += "- (nenhuma)\n"
    for issue in data.issues:
        prompt += f"- #{issue['number']} [{issue['state']}] por {issue['author']}: {issue['title']}\n"

    return prompt
