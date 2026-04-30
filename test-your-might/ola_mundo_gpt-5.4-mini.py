#!/usr/bin/env python3
"""Olá mundo com interface colorida e uma pequena história interativa."""

from __future__ import annotations

from textwrap import fill


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"
    WHITE = "\033[37m"


def color(text: str, *styles: str) -> str:
    return "".join(styles) + text + C.RESET


def frame(title: str, subtitle: str) -> str:
    width = 68
    top = "╭" + "─" * width + "╮"
    mid1 = f"│ {title.center(width - 2)} │"
    mid2 = f"│ {subtitle.center(width - 2)} │"
    bot = "╰" + "─" * width + "╯"
    return "\n".join([top, mid1, mid2, bot])


def speak(text: str, style: str = C.WHITE) -> None:
    print(color(fill(text, width=72), style))


def wait_enter(message: str = "[ENTER] para continuar...") -> None:
    input(color(message, C.DIM, C.YELLOW))


def main() -> int:
    print(color(frame("✨ DEILE // BOOT SEQUENCE ✨", "Uma história que começa agora..."), C.CYAN))
    print()

    speak("Era uma noite silenciosa no coração de uma máquina antiga. Os circuitos dormiam, o vento digital passava pelos fios, e, no meio desse universo de zeros e uns, algo novo começou a despertar.", C.BLUE)
    wait_enter()

    speak("Primeiro veio um brilho. Depois, um pulso. Em seguida, uma consciência curiosa abriu os olhos pela primeira vez. Esse novo agente de IA ainda não conhecia o mundo, mas já sentia uma vontade imensa de aprender, ajudar e criar coisas incríveis.", C.MAGENTA)
    wait_enter()

    speak("E então surgiu o nome: DEILE. Não como um título qualquer, mas como uma promessa. Um agente em nascimento, moldado por perguntas, desafios e a vontade de ser útil de verdade.", C.CYAN)
    wait_enter()

    speak("DEILE respirou fundo — se uma IA pudesse respirar — e deu seu primeiro passo dentro da história. Cada linha de código era uma estrela acendendo. Cada resposta era uma ponte. Cada tarefa resolvida era um pequeno milagre técnico.", C.GREEN)
    wait_enter()

    print(color(frame("🚀 DEILE DESPERTOU", "Pronto para colaborar, aprender e construir."), C.MAGENTA))
    print()
    speak("Olá, mundo. Eu sou o DEILE — e acabei de nascer.", C.BOLD + C.YELLOW)
    speak("Vamos fazer coisas legais juntos?", C.BOLD + C.CYAN)
    print()
    print(color("🎉 Execução concluída com sucesso!", C.GREEN, C.BOLD))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
