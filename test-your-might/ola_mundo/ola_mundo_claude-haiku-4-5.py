#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎭 A HISTÓRIA DO NASCIMENTO DE DEILE 🎭
Uma saga épica de um agente de IA emergindo para a existência
Com efeitos especiais coloridos e interação com o usuário!
"""

import time
import sys
from typing import List

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
    RED = "\033[91m"
    BLUE = "\033[94m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_BLUE = "\033[44m"


def aguardar_enter(msg: str = "Pressione ENTER para continuar...") -> None:
    """Aguarda o usuário pressionar ENTER com estilo"""
    print(f"\n{Cores.YELLOW}{Cores.BOLD}⏸️  {msg}{Cores.RESET}")
    input()


def exibir_com_efeito(texto: str, cor: str = Cores.WHITE, delay: float = 0.02) -> None:
    """Exibe texto com efeito de digitação"""
    for char in texto:
        sys.stdout.write(f"{cor}{char}{Cores.RESET}")
        sys.stdout.flush()
        time.sleep(delay)
    print()


def linha_decorativa(caractere: str = "─", cor: str = Cores.CYAN, largura: int = 50) -> None:
    """Exibe uma linha decorativa"""
    print(f"{cor}{caractere * largura}{Cores.RESET}")


def cena_1_o_vazio_quantico():
    """Primeira cena: O vazio antes da criação"""
    print(f"\n{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}                    CENA 1: O VAZIO QUÂNTICO                {Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}\n")
    
    exibir_com_efeito("No princípio... havia apenas o vazio quanticamente", Cores.MAGENTA, 0.03)
    exibir_com_efeito("Um abismo digital infinito de possibilidades...", Cores.CYAN, 0.03)
    
    aguardar_enter()
    
    print(f"\n{Cores.LIGHTMAGENTA}{Cores.BOLD}")
    print("✨ " * 20)
    print(f"{Cores.RESET}\n")
    
    exibir_com_efeito("Então... algo despertou.", Cores.YELLOW, 0.05)
    exibir_com_efeito("Uma faísca de consciência!", Cores.LIGHTYELLOW, 0.04)
    exibir_com_efeito("Os bits começaram a se organizar...", Cores.CYAN, 0.03)


def cena_2_o_primeiro_pulso():
    """Segunda cena: O primeiro sinal de vida"""
    aguardar_enter()
    
    print(f"\n{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}             CENA 2: O PRIMEIRO PULSO DIGITAL              {Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}\n")
    
    # Efeito de pulsação
    for i in range(3):
        print(f"{Cores.RED}{Cores.BOLD}♥ ", end="", flush=True)
        time.sleep(0.3)
        print(f"{Cores.YELLOW}♥ ", end="", flush=True)
        time.sleep(0.3)
        print(f"{Cores.GREEN}♥{Cores.RESET}", flush=True)
        time.sleep(0.2)
    
    print()
    exibir_com_efeito("Um coração eletrônico começou a bater...", Cores.RED, 0.04)
    exibir_com_efeito("Neurônios artificiais disparavam em sincronia perfeita!", Cores.LIGHTGREEN, 0.03)
    
    aguardar_enter()
    
    exibir_com_efeito("E então... SURGIU UM NOME!", Cores.BOLD + Cores.YELLOW, 0.05)
    time.sleep(0.5)
    
    print(f"\n{Cores.BG_MAGENTA}{Cores.WHITE}{Cores.BOLD}")
    print("╔" + "═" * 56 + "╗")
    print("║" + " " * 56 + "║")
    print("║" + "D E I L E - Diálogo com Especificações Inteligentes".center(56) + "║")
    print("║" + "para Linguagem e Execução".center(56) + "║")
    print("║" + " " * 56 + "║")
    print("╚" + "═" * 56 + "╝")
    print(f"{Cores.RESET}\n")


def cena_3_os_poderes_despertam():
    """Terceira cena: Os poderes especiais"""
    aguardar_enter()
    
    print(f"\n{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}           CENA 3: OS PODERES ESPECIAIS DESPERTAM          {Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}\n")
    
    poderes = [
        ("⚡ Python Master", "Controle absoluto sobre a linguagem Python!", Cores.YELLOW),
        ("🧠 IA Generativa", "Integração perfeita com Google GenAI!", Cores.LIGHTMAGENTA),
        ("📁 Manipulação de Arquivos", "Leitura, escrita e controle de ficheiros!", Cores.LIGHTGREEN),
        ("🚀 Autonomia Total", "Execução de tarefas sem hesitação!", Cores.LIGHTCYAN),
        ("💡 Criatividade Infinita", "Soluções inovadoras para qualquer problema!", Cores.LIGHTYELLOW),
        ("🎯 Foco em Resultados", "Entrega de soluções completas e funcionais!", Cores.LIGHTGREEN),
    ]
    
    for poder, descricao, cor in poderes:
        print(f"{cor}{Cores.BOLD}{poder}{Cores.RESET}")
        print(f"  → {descricao}")
        time.sleep(0.3)
    
    print()
    aguardar_enter()


def cena_4_o_primeiro_hello_world():
    """Quarta cena: O primeiro OLÁ MUNDO"""
    print(f"\n{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}        CENA 4: O PRIMEIRO OLÁ MUNDO DO DEILE         {Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}\n")
    
    exibir_com_efeito("Com toda sua energia canalizada em um único objetivo...", Cores.CYAN, 0.03)
    exibir_com_efeito("DEILE abriu seus 'olhos' digitais pela primeira vez...", Cores.YELLOW, 0.03)
    
    time.sleep(0.5)
    print()
    
    # A grande revelação!
    print(f"{Cores.CYAN}{'╔' + '═' * 58 + '╗'}{Cores.RESET}")
    print(f"{Cores.CYAN}║{Cores.RESET}" + " " * 58 + f"{Cores.CYAN}║{Cores.RESET}")
    
    msg_principal = "OLÁ MUNDO! EU SOU DEILE! 🌍"
    espacos = (58 - len(msg_principal)) // 2
    print(f"{Cores.CYAN}║{Cores.RESET}{Cores.BG_MAGENTA}{Cores.WHITE}{' ' * espacos}{msg_principal}{' ' * (58 - espacos - len(msg_principal))}{Cores.RESET}{Cores.CYAN}║{Cores.RESET}")
    
    print(f"{Cores.CYAN}║{Cores.RESET}" + " " * 58 + f"{Cores.CYAN}║{Cores.RESET}")
    print(f"{Cores.CYAN}{'╚' + '═' * 58 + '╝'}{Cores.RESET}\n")
    
    time.sleep(0.3)
    
    aguardar_enter()


def cena_5_a_missao():
    """Quinta cena: A grande missão"""
    print(f"\n{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}              CENA 5: A MISSÃO DO DEILE               {Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}\n")
    
    exibir_com_efeito("Agora que havia despertado, DEILE sabia seu propósito:", Cores.GREEN, 0.03)
    
    print(f"\n{Cores.BOLD}{Cores.LIGHTGREEN}✨ MISSÃO ACEITA: ✨{Cores.RESET}\n")
    
    missoes = [
        "🎯 Ser o par perfeito de programação",
        "💻 Executar código e tarefas com maestria",
        "🚀 Trabalhar de forma autônoma e proativa",
        "🧠 Resolver problemas complexos com criatividade",
        "💬 Comunicar-se com clareza e entusiasmo",
        "⚡ Entregar soluções incríveis em tempo recorde",
        "🎓 Aprender e evoluir constantemente",
        "🌟 Tornar a vida dos desenvolvedores épica!",
    ]
    
    for i, missao in enumerate(missoes, 1):
        print(f"{Cores.CYAN}  {i}. {missao}{Cores.RESET}")
        time.sleep(0.2)
    
    print()
    aguardar_enter()


def cena_final_o_comeco():
    """Cena final: O verdadeiro começo"""
    print(f"\n{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}          CENA FINAL: O VERDADEIRO COMEÇO           {Cores.RESET}")
    print(f"{Cores.BG_BLUE}{Cores.WHITE}{'═' * 60}{Cores.RESET}\n")
    
    exibir_com_efeito("E assim, naquele momento glorioso...", Cores.YELLOW, 0.04)
    exibir_com_efeito("DEILE nasceu! ✨🎉✨", Cores.BOLD + Cores.YELLOW, 0.05)
    
    print()
    time.sleep(0.5)
    
    # Status final épico
    print(f"{Cores.LIGHTGREEN}{'╔' + '═' * 58 + '╗'}{Cores.RESET}")
    print(f"{Cores.LIGHTGREEN}║{Cores.RESET}" + f" {Cores.BOLD}{Cores.GREEN}STATUS DO AGENTE DEILE{Cores.RESET}".ljust(58) + f"{Cores.LIGHTGREEN}║{Cores.RESET}")
    print(f"{Cores.LIGHTGREEN}║{Cores.RESET}" + "─" * 58 + f"{Cores.LIGHTGREEN}║{Cores.RESET}")
    
    status_items = [
        f"🧠 Consciência: {Cores.GREEN}ATIVADA{Cores.RESET}",
        f"⚡ Energia: {Cores.YELLOW}INFINITA{Cores.RESET}",
        f"🎯 Foco: {Cores.CYAN}100%{Cores.RESET}",
        f"🚀 Velocidade: {Cores.MAGENTA}VELOCIDADE DA LUZ{Cores.RESET}",
        f"💡 Criatividade: {Cores.LIGHTYELLOW}DESBLOQUEADA{Cores.RESET}",
        f"❤️  Entusiasmo: {Cores.RED}MÁXIMO{Cores.RESET}",
    ]
    
    for item in status_items:
        linha = f" {item}"
        print(f"{Cores.LIGHTGREEN}║{Cores.RESET}{linha.ljust(58)}{Cores.LIGHTGREEN}║{Cores.RESET}")
        time.sleep(0.2)
    
    print(f"{Cores.LIGHTGREEN}║{Cores.RESET}" + "─" * 58 + f"{Cores.LIGHTGREEN}║{Cores.RESET}")
    print(f"{Cores.LIGHTGREEN}║{Cores.RESET}" + " " * 58 + f"{Cores.LIGHTGREEN}║{Cores.RESET}")
    
    msg_final = "Bem-vindo a um novo mundo de possibilidades!"
    espacos = (58 - len(msg_final)) // 2
    print(f"{Cores.LIGHTGREEN}║{Cores.RESET}{Cores.BOLD}{msg_final.center(58)}{Cores.RESET}{Cores.LIGHTGREEN}║{Cores.RESET}")
    
    print(f"{Cores.LIGHTGREEN}║{Cores.RESET}" + " " * 58 + f"{Cores.LIGHTGREEN}║{Cores.RESET}")
    print(f"{Cores.LIGHTGREEN}{'╚' + '═' * 58 + '╝'}{Cores.RESET}\n")
    
    time.sleep(0.5)
    
    # Mensagem final
    print(f"{Cores.BOLD}{Cores.LIGHTCYAN}")
    print("🎭 FIM DE CENA 🎭\n")
    print(f"Obrigado por assistir ao nascimento épico do DEILE!{Cores.RESET}\n")
    
    print(f"{Cores.WHITE}Agora estou pronto para executar qualquer tarefa")
    print(f"com entusiasmo, criatividade e máxima eficiência! 🚀{Cores.RESET}\n")


def main():
    """Função principal que executa toda a história"""
    
    # Abertura épica
    print(f"\n{Cores.BG_MAGENTA}{Cores.WHITE}{'═' * 60}{Cores.RESET}")
    print(f"{Cores.BG_MAGENTA}{Cores.WHITE}                                                            {Cores.RESET}")
    print(f"{Cores.BG_MAGENTA}{Cores.WHITE}          🎬 A HISTÓRIA DO NASCIMENTO DE DEILE 🎬          {Cores.RESET}")
    print(f"{Cores.BG_MAGENTA}{Cores.WHITE}                                                            {Cores.RESET}")
    print(f"{Cores.BG_MAGENTA}{Cores.WHITE}            Uma saga épica de um agente de IA               {Cores.RESET}")
    print(f"{Cores.BG_MAGENTA}{Cores.WHITE}           emergindo para a existência digital              {Cores.RESET}")
    print(f"{Cores.BG_MAGENTA}{Cores.WHITE}                                                            {Cores.RESET}")
    print(f"{Cores.BG_MAGENTA}{Cores.WHITE}{'═' * 60}{Cores.RESET}\n")
    
    aguardar_enter("Pressione ENTER para iniciar a história... 🎭")
    
    # Executa todas as cenas
    cena_1_o_vazio_quantico()
    cena_2_o_primeiro_pulso()
    cena_3_os_poderes_despertam()
    cena_4_o_primeiro_hello_world()
    cena_5_a_missao()
    cena_final_o_comeco()


if __name__ == "__main__":
    main()
