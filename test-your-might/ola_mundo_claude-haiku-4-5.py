#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script colorido para saudar o mundo! 🌍
Usa ANSI codes nativos (sem dependências externas)
"""

class Cores:
    """Códigos ANSI para cores no terminal"""
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    LIGHTGREEN = "\033[92m"
    LIGHTCYAN = "\033[96m"
    LIGHTMAGENTA = "\033[95m"
    LIGHTYELLOW = "\033[93m"
    WHITE = "\033[97m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    BG_MAGENTA = "\033[45m"

def main():
    """Função principal que cria uma interface colorida bonitinha"""
    
    # Limpa e cria espaço
    print("\n")
    
    # Cria uma caixa decorativa com cores
    print(f"{Cores.CYAN}╔═══════════════════════════════════════════╗{Cores.RESET}")
    print(f"{Cores.CYAN}║{Cores.RESET}                                           {Cores.CYAN}║{Cores.RESET}")
    print(f"{Cores.CYAN}║{Cores.RESET}  {Cores.BG_MAGENTA}{Cores.WHITE}✨ OLÁ MUNDO! ✨{Cores.RESET}                  {Cores.CYAN}║{Cores.RESET}")
    print(f"{Cores.CYAN}║{Cores.RESET}                                           {Cores.CYAN}║{Cores.RESET}")
    print(f"{Cores.CYAN}╠═══════════════════════════════════════════╣{Cores.RESET}")
    print(f"{Cores.CYAN}║{Cores.RESET} {Cores.GREEN}Bem-vindo à incrível jornada da{Cores.RESET}      {Cores.CYAN}║{Cores.RESET}")
    print(f"{Cores.CYAN}║{Cores.RESET} {Cores.YELLOW}programação em Python! 🐍{Cores.RESET}          {Cores.CYAN}║{Cores.RESET}")
    print(f"{Cores.CYAN}║{Cores.RESET}                                           {Cores.CYAN}║{Cores.RESET}")
    print(f"{Cores.CYAN}╚═══════════════════════════════════════════╝{Cores.RESET}")
    
    print("\n")
    
    # Mensagens coloridas adicionais
    print(f"{Cores.LIGHTGREEN}{'=' * 45}{Cores.RESET}")
    print(f"{Cores.LIGHTCYAN}🎯 Status: {Cores.LIGHTGREEN}✓ Executado com Sucesso!{Cores.RESET}")
    print(f"{Cores.LIGHTCYAN}🎨 Interface: {Cores.LIGHTMAGENTA}Colorida e Bonitinha!{Cores.RESET}")
    print(f"{Cores.LIGHTCYAN}🚀 Resultado: {Cores.LIGHTYELLOW}Espetacular!{Cores.RESET}")
    print(f"{Cores.LIGHTGREEN}{'=' * 45}{Cores.RESET}")
    
    print("\n")

if __name__ == "__main__":
    main()
