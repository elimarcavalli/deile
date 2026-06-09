#!/usr/bin/env python3
"""Bump da versão única do DEILE.

``deile/__version__.py`` é a ÚNICA literal de versão do app; todo o resto deriva
dela. Este script lê a versão atual (parse estático, sem importar o pacote),
calcula a próxima e reescreve SÓ ``deile/__version__.py`` (``__version__``,
``__version_info__`` e ``__build_date__``). Não faz git commit/tag — isso é
responsabilidade do Humano.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_VERSION_FILE = _ROOT / "deile" / "__version__.py"

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_VERSION_ASSIGN_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _read_current_version() -> str:
    text = _VERSION_FILE.read_text(encoding="utf-8")
    match = _VERSION_ASSIGN_RE.search(text)
    if not match:
        raise SystemExit(f"Não encontrei __version__ em {_VERSION_FILE}")
    version = match.group(1)
    if not _SEMVER_RE.match(version):
        raise SystemExit(f"Versão atual não é SemVer X.Y.Z: {version!r}")
    return version


def _next_version(current: str, part: str) -> str:
    major, minor, patch = (int(n) for n in current.split("."))
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:
        raise SystemExit(f"Parte inválida: {part!r}")
    return f"{major}.{minor}.{patch}"


def _validate_semver(version: str) -> str:
    if not _SEMVER_RE.match(version):
        raise SystemExit(f"--set exige SemVer X.Y.Z, recebido: {version!r}")
    return version


def _write_version(new_version: str, build_date: str) -> None:
    major, minor, patch = (int(n) for n in new_version.split("."))
    text = _VERSION_FILE.read_text(encoding="utf-8")
    text = _VERSION_ASSIGN_RE.sub(f'__version__ = "{new_version}"', text, count=1)
    text = re.sub(
        r"^__version_info__\s*=\s*\([^)]*\)",
        f"__version_info__ = ({major}, {minor}, {patch})",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r'^__build_date__\s*=\s*["\'][^"\']*["\']',
        f'__build_date__ = "{build_date}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    _VERSION_FILE.write_text(text, encoding="utf-8")


def _last_tag() -> str | None:
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tag = result.stdout.strip()
    return tag or None


def _commit_subjects() -> list[str]:
    tag = _last_tag()
    rev_range = f"{tag}..HEAD" if tag else "HEAD"
    try:
        result = subprocess.run(
            ["git", "log", "--format=%s%n%b%x00", rev_range],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [chunk.strip() for chunk in result.stdout.split("\0") if chunk.strip()]


def _suggest_part() -> str:
    feat = False
    fix = False
    for message in _commit_subjects():
        first_line = message.splitlines()[0] if message else ""
        if "BREAKING CHANGE" in message or re.match(r"^\w+(\([^)]*\))?!:", first_line):
            return "major"
        if re.match(r"^feat(\([^)]*\))?:", first_line):
            feat = True
        elif re.match(r"^fix(\([^)]*\))?:", first_line):
            fix = True
    if feat:
        return "minor"
    if fix:
        return "patch"
    return "patch"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bump_version.py",
        description="Bump da versão única do DEILE (deile/__version__.py).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--part",
        choices=("major", "minor", "patch"),
        help="Incrementa a parte SemVer (reseta as menores).",
    )
    group.add_argument("--set", dest="set_to", metavar="X.Y.Z", help="Crava uma versão SemVer.")
    group.add_argument(
        "--suggest",
        action="store_true",
        help="Sugere o nível por Conventional Commits desde a última tag (só sugere).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra atual→próxima sem gravar.",
    )
    parser.add_argument(
        "--build-date",
        default=_dt.date.today().isoformat(),
        help="Data de build a gravar (default: hoje, ISO YYYY-MM-DD).",
    )
    args = parser.parse_args(argv)

    current = _read_current_version()

    if args.suggest:
        suggested = _suggest_part()
        nxt = _next_version(current, suggested)
        print(f"Sugestão (Conventional Commits): --part {suggested}  ({current} → {nxt})")
        print("Aplicar exige --part/--set explícito.")
        return 0

    if args.part:
        new_version = _next_version(current, args.part)
    elif args.set_to:
        new_version = _validate_semver(args.set_to)
    else:
        parser.error("informe uma ação: --part {major,minor,patch}, --set X.Y.Z ou --suggest")

    if args.dry_run:
        print(f"{current} → {new_version} (dry-run, nada gravado)")
        return 0

    _write_version(new_version, args.build_date)
    print(f"{current} → {new_version} gravado em {_VERSION_FILE.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
