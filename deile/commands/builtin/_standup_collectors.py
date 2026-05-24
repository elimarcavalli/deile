"""Standup data collectors — pure CLI parsing + git/gh observation.

Extracted from ``standup_command.py`` so the command file owns LLM
dispatch + Rich panel rendering, while these helpers own argument
parsing and subprocess-based collection of commits/PRs/issues.

Pilar 03 §1: I/O subprocess síncrono é deliberadamente isolado aqui;
o caller (``StandupCommand.execute``) faz ``asyncio.to_thread`` para
não bloquear o event loop. Nenhuma dependência de Rich, model_router
ou outros subsistemas — coleta é fim em si mesmo, presentation e LLM
ficam no módulo principal.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from ...core.exceptions import CommandError


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
        raise CommandError(f"Duração inválida: {duration}. Use formato como 24h, 3d, 1w.")
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


def _ensure_git_repo() -> None:
    if not shutil.which("git"):
        raise CommandError("Git não está instalado.")
    res = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise CommandError("O diretório atual não é um repositório git.")


def _ensure_gh_available() -> None:
    if not shutil.which("gh"):
        raise CommandError("GitHub CLI (gh) não está instalada.")
    res = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise CommandError("CLI do GitHub (gh) não está autenticada.")


def collect_commits(since_iso: str) -> List[Dict[str, str]]:
    res = subprocess.run(
        ["git", "log", f"--since={since_iso}", "--format=%h\x1f%an\x1f%s"],
        capture_output=True,
        text=True,
    )
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


def _collect_gh_items(verb: str, since_iso: str) -> List[Dict[str, Any]]:
    """Coleta itens via ``gh <verb> list`` filtrados por ``updated:>=``.

    Compartilhado por :func:`collect_prs` (``verb='pr'``) e
    :func:`collect_issues` (``verb='issue'``) — ambos chamavam ``gh`` com
    a mesma forma de comando, mesmos campos JSON e mesma normalização
    de autor. Devolve ``[]`` em qualquer falha (returncode != 0 ou JSON
    inválido) — o caller decide se isso é um erro.
    """
    res = subprocess.run(
        [
            "gh",
            verb,
            "list",
            "--state",
            "all",
            "--search",
            f"updated:>={since_iso}",
            "--json",
            "number,title,state,author,url,updatedAt",
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return []
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return []
    items: List[Dict[str, Any]] = []
    for item in data:
        author = item.get("author")
        author_name = author.get("login") if isinstance(author, dict) else "?"
        items.append(
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "author": author_name,
                "url": item.get("url", ""),
                "updated_at": item.get("updatedAt", ""),
            }
        )
    return items


def collect_prs(since_iso: str) -> List[Dict[str, Any]]:
    return _collect_gh_items("pr", since_iso)


def collect_issues(since_iso: str) -> List[Dict[str, Any]]:
    return _collect_gh_items("issue", since_iso)


def collect_standup_data(since_spec: str) -> StandupData:
    _ensure_git_repo()
    _ensure_gh_available()

    delta = parse_since(since_spec)
    since_date = datetime.now(timezone.utc) - delta
    since_iso = since_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    commits = collect_commits(since_iso)
    prs = collect_prs(since_iso)
    issues = collect_issues(since_iso)

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
        prompt += f"- #{pr['number']} [{pr['state']}] por {pr['author']}: {pr['title']}\n"

    prompt += f"\nIssues ({len(data.issues)}):\n"
    if not data.issues:
        prompt += "- (nenhuma)\n"
    for issue in data.issues:
        prompt += f"- #{issue['number']} [{issue['state']}] por {issue['author']}: {issue['title']}\n"

    return prompt
