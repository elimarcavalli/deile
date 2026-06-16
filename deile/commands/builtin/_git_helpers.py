"""Helpers de git compartilhados pelos comandos builtin.

Consolida operações triviais de subprocess sobre o binário ``git`` que
estavam duplicadas entre :mod:`loc_command`, :mod:`standup_command` e
:mod:`todo_command` — cada arquivo trazia sua própria variante de
``git ls-files`` e ``git rev-parse``, com tratamento de erro divergente.

Também centraliza *gates* de pré-requisito (``ensure_git_repo`` e
``ensure_gh_authenticated``) para qualquer comando que dependa de um
repositório git válido ou de ``gh`` CLI autenticada.

Cada helper retorna um valor previsível ou levanta :class:`CommandError`
com mensagem PT-BR; nenhum log próprio é emitido para que o caller
possa decidir o nível de verbosidade.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ...core.exceptions import CommandError

_DEFAULT_TIMEOUT_SECONDS = 30


def git_ls_files(
    cwd: str | Path | None = None, *, timeout: int = _DEFAULT_TIMEOUT_SECONDS
) -> list[str]:
    """Lista paths versionados via ``git ls-files``.

    Retorna a lista de paths relativos não vazios. Levanta
    :class:`CommandError` quando ``git`` não existe no host, o repositório
    não é git, ou o comando excede ``timeout`` — callers que precisam de
    fallback silencioso embrulham com ``try/except``.
    """
    cwd_str = str(cwd) if cwd is not None else None
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=cwd_str,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"git ls-files timeout: {exc}") from exc
    except FileNotFoundError as exc:
        raise CommandError(f"git não encontrado: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise CommandError(
            f"git ls-files falhou: {(exc.stderr or '').strip()}"
        ) from exc

    return [line for line in result.stdout.splitlines() if line.strip()]


def resolve_repo_root(*, timeout: int = 10) -> Path:
    """Resolve a raiz do repositório git a partir do CWD.

    Levanta :class:`CommandError` se ``git`` não está no host, o comando
    falha ou o diretório atual não está dentro de um repo git.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise CommandError(f"git rev-parse falhou: {exc}") from exc

    if result.returncode != 0:
        raise CommandError("Não foi possível determinar a raiz do repositório git")
    return Path(result.stdout.strip())


def ensure_git_repo(cwd: str | Path | None = None) -> Path:
    """Garante que ``git`` está instalado e que o CWD é um repositório git.

    Retorna o ``Path`` da raiz do repositório (reaproveita
    :func:`resolve_repo_root`). Levanta :class:`CommandError` com mensagem
    PT-BR quando ``git`` não está disponível no host ou o diretório não
    pertence a um repositório.
    """
    if not shutil.which("git"):
        raise CommandError("Git não está instalado.")
    try:
        return resolve_repo_root()
    except CommandError as exc:
        # Reescreve a mensagem técnica de resolve_repo_root para algo
        # mais amigável no contexto do gate (mantém PT-BR).
        raise CommandError("O diretório atual não é um repositório git.") from exc


def ensure_gh_authenticated(*, timeout: int = _DEFAULT_TIMEOUT_SECONDS) -> None:
    """Garante que a ``gh`` CLI está instalada e autenticada.

    Levanta :class:`CommandError` com mensagem PT-BR quando ``gh`` não
    está instalada, ``gh auth status`` excede ``timeout`` ou retorna código
    de saída diferente de zero. O ``timeout`` evita que um ``gh auth status``
    travado (ex.: prompt de credencial interativo) bloqueie o comando
    indefinidamente.
    """
    if not shutil.which("gh"):
        raise CommandError("GitHub CLI (gh) não está instalada.")
    try:
        res = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"gh auth status timeout: {exc}") from exc
    if res.returncode != 0:
        raise CommandError("CLI do GitHub (gh) não está autenticada.")
