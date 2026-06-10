"""Regressão estrutural: a versão do app vive em UM lugar só.

``deile/__version__.py`` é a ÚNICA literal de versão do DEILE; todo o resto
deriva dela (``pyproject.toml`` via ``[tool.setuptools.dynamic]``,
``deile/__init__.py`` por re-export, settings/UI/personas por import). Este
teste varre o repo procurando a STRING da versão corrente FORA dos locais
permitidos e falha apontando ``arquivo:linha`` — impede que alguém volte a
cravar o número e crie duplicatas stale (como os ``5.1.0`` que existiam antes).

Espelha o estilo de ``deile/tests/commands/test_table_widths_adaptive.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from deile.__version__ import __version__

ROOT = Path(__file__).resolve().parents[2]

# Arquivos onde a versão literal É legítima (fonte única + histórico).
ALLOWED = {
    ROOT / "deile" / "__version__.py",
    ROOT / "CHANGELOG.md",
}

# Alvos varridos. Diretórios são percorridos recursivamente; arquivos avulsos
# são checados diretamente.
TARGETS = [
    ROOT / "deile",
    ROOT / "infra",
    ROOT / "README.md",
    ROOT / "pyproject.toml",
]

_SCANNED_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini"}
# Casa a versão como token isolado (não como substring de 11.1.0, etc.).
_VERSION_RE = re.compile(rf"(?<![\w.]){re.escape(__version__)}(?![\w.])")


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for target in TARGETS:
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(
                p
                for p in target.rglob("*")
                if p.is_file() and p.suffix in _SCANNED_SUFFIXES
            )
    return files


@pytest.mark.unit
def test_app_version_is_not_hardcoded_outside_source_of_truth() -> None:
    """A versão corrente não aparece literal fora de ``__version__.py``/CHANGELOG.

    Por que: a versão é fonte única em ``deile/__version__.py``. Qualquer outra
    ocorrência literal é uma duplicata que vai ficar stale no próximo bump.
    """
    offenders: list[str] = []
    for path in _iter_files():
        if path in ALLOWED:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _VERSION_RE.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        f"Versão {__version__!r} cravada fora de deile/__version__.py / CHANGELOG.md "
        "— derive de `from deile.__version__ import __version__` em vez de hardcodar:\n"
        + "\n".join(offenders)
    )
