# test-your-might/calculadora/apresentacao.py

import os

class Apresentacao:
    """
    Classe responsável pela apresentação visual da calculadora.
    """
    def exibir_menu(self):
        print("\n" + "=" * 40)
        print("      ✨ Calculadora Interativa DEILE ✨")
        print("=" * 40)
        print("Operações disponíveis:")
        print("  ➕ Somar")
        print("  ➖ Subtrair")
        print("  ✖️ Multiplicar")
        print("  ➗ Dividir")
        print("  🚪 Sair")
        print("-" * 40)

    def exibir_resultado(self, resultado, operacao):
        print("\n" + "*" * 30)
        print(f"  ✅ Resultado da {operacao}: {resultado}")
        print("*" * 30)

    def exibir_erro(self, mensagem):
        print(f"\n  ❌ Erro: {mensagem}")

    def limpar_tela(self):
        # Comando para limpar a tela no Windows
        if os.name == 'nt':
            os.system('cls')
        # Comando para limpar a tela em sistemas Unix/Linux/macOS
        else:
            os.system('clear')

    def mensagem_boas_vindas(self):
        print("Bem-vindo à Calculadora Interativa DEILE! 🚀")

    def mensagem_despedida(self):
        print("\nObrigado por usar a Calculadora DEILE! Até mais! 👋")

    def prompt_continuar(self):
        input("Pressione Enter para continuar...")
