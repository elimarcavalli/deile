#!/usr/bin/env python3
"""
🌟 DEILE v5.1 ULTRA — Olá Mundo Colorido 🌟
Um script bonitinho com interface colorida para saudar o mundo!
"""

import sys
import time


# ─── Paleta de Cores ANSI ───────────────────────────────────────────
class Cor:
    """Cores e estilos ANSI para deixar tudo mais bonito 🌈"""

    # Reset
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"

    # Cores do arco-íris
    VERMELHO = "\033[91m"
    LARANJA = "\033[38;5;208m"
    AMARELO = "\033[93m"
    VERDE = "\033[92m"
    CIANO = "\033[96m"
    AZUL = "\033[94m"
    MAGENTA = "\033[95m"
    ROSA = "\033[38;5;213m"
    ROXO = "\033[38;5;129m"

    # Backgrounds
    BG_PRETO = "\033[40m"
    BG_VERMELHO = "\033[41m"
    BG_VERDE = "\033[42m"
    BG_AMARELO = "\033[43m"
    BG_AZUL = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CIANO = "\033[46m"
    BG_BRANCO = "\033[47m"

    # Efeitos especiais
    PISCANTE = "\033[5m"
    INVERTIDO = "\033[7m"


def efeito_digitando(texto: str, delay: float = 0.03, cor: str = ""):
    """Simula efeito de digitação letra por letra ✍️"""
    for char in texto:
        sys.stdout.write(f"{cor}{char}{Cor.RESET}")
        sys.stdout.flush()
        time.sleep(delay)
    print()


def arco_iris(texto: str) -> str:
    """Retorna o texto colorido com gradiente arco-íris 🌈"""
    cores = [
        Cor.VERMELHO,
        Cor.LARANJA,
        Cor.AMARELO,
        Cor.VERDE,
        Cor.CIANO,
        Cor.AZUL,
        Cor.MAGENTA,
        Cor.ROSA,
    ]
    resultado = ""
    for i, char in enumerate(texto):
        if char.strip():
            resultado += f"{cores[i % len(cores)]}{Cor.BOLD}{char}"
        else:
            resultado += char
    resultado += Cor.RESET
    return resultado


def banner_estrelado():
    """Desenha um banner decorativo 🌟"""
    estrelas = ["⭐", "🌟", "✨", "💫", "⭐", "🌟", "✨", "💫"]

    print()
    # Linha superior
    topo = "╔" + "═" * 50 + "╗"
    print(f"{Cor.CIANO}{Cor.BOLD}{topo}{Cor.RESET}")

    # Linhas decorativas
    for i, estrela in enumerate(estrelas):
        espacos_antes = " " * (i * 6 + 1)
        print(
            f"{Cor.CIANO}║{Cor.RESET}{espacos_antes}"
            f"{estrela}"
            f"{Cor.CIANO}║{Cor.RESET}"
        )
        time.sleep(0.08)

    # Linha inferior
    base = "╚" + "═" * 50 + "╝"
    print(f"{Cor.CIANO}{Cor.BOLD}{base}{Cor.RESET}")
    print()


def coracao_pulsante():
    """Anima um coração pulsante ❤️"""
    coracoes = ["♡", "♥", "❤️", "💗", "💖", "💝", "💕", "💓"]
    linha = ""
    for i, c in enumerate(coracoes):
        cor = [Cor.VERMELHO, Cor.ROSA, Cor.MAGENTA, Cor.VERMELHO][i % 4]
        linha += f"{cor}{c} {Cor.RESET}"
    return linha


def main():
    """Função principal — O Grande Espetáculo 🎪"""

    # Limpa a tela (funciona na maioria dos terminais)
    print("\033[2J\033[H", end="")

    # ─── INTRO ──────────────────────────────────────────────
    banner_estrelado()

    efeito_digitando(
        "   🚀  DEILE v5.1 ULTRA apresenta...",
        delay=0.04,
        cor=Cor.DIM,
    )
    time.sleep(0.5)

    print()

    # ─── TÍTULO PRINCIPAL ──────────────────────────────────
    titulo = """
    ┌──────────────────────────────────────────────────┐
    │                                                  │
    │       O L Á   M U N D O   C O L O R I D O        │
    │                                                  │
    └──────────────────────────────────────────────────┘
    """
    for linha in titulo.split("\n"):
        if "O L Á" in linha:
            print(f"    {arco_iris(linha)}")
        else:
            print(f"    {Cor.MAGENTA}{linha}{Cor.RESET}")
        time.sleep(0.06)

    print()
    time.sleep(0.3)

    # ─── MENSAGEM PRINCIPAL ──────────────────────────────
    quadrado = "█"

    print(f"    {Cor.VERMELHO}{quadrado * 48}{Cor.RESET}")
    print(
        f"    {Cor.VERDE}{quadrado}{Cor.RESET} "
        f"{Cor.BOLD}🌍  Hello, World!  —  Olá, Mundo!  🌍{Cor.RESET}"
        f"     {Cor.VERDE}{quadrado}{Cor.RESET}"
    )
    print(f"    {Cor.AZUL}{quadrado * 48}{Cor.RESET}")

    print()

    # ─── CORAÇÕES ────────────────────────────────────────
    print(f"        {coracao_pulsante()}")
    print()

    # ─── INFORMAÇÕES DO SISTEMA ──────────────────────────
    print(f"    {Cor.BOLD}{Cor.DIM}🐍 Python {sys.version.split()[0]}{Cor.RESET}")
    print(
        f"    {Cor.BOLD}{Cor.DIM}💻 Terminal com "
        f"{'suporte' if sys.stdout.isatty() else 'saída'} a cores{Cor.RESET}"
    )
    print(
        f"    {Cor.BOLD}{Cor.DIM}✨ Codificado com amor por DEILE{Cor.RESET}"
    )

    print()
    efeito_digitando(
        "         🎉 Script executado com sucesso! 🎉",
        delay=0.05,
        cor=Cor.VERDE,
    )
    print()


if __name__ == "__main__":
    main()
