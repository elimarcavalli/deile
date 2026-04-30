#!/usr/bin/env python3
"""Olá mundo com interface colorida e bonitinha."""

from __future__ import annotations

import sys


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"


def color(text: str, *styles: str) -> str:
    return "".join(styles) + text + C.RESET


def main() -> int:
    banner = f"""
{color('╭────────────────────────────────────╮', C.CYAN)}
{color('│', C.CYAN)} {color('✨ Olá, Mundo! ✨', C.BOLD, C.MAGENTA)} {color('│', C.CYAN)}
{color('│', C.CYAN)} {color('Um script simples, bonito e colorido.', C.BLUE)} {color('│', C.CYAN)}
{color('╰────────────────────────────────────╯', C.CYAN)}
""".strip("\n")

    print(banner)
    print(color("🚀 Execução concluída com sucesso!", C.GREEN, C.BOLD))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
