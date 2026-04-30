# test-your-might/calculadora/entrada_usuario.py

# Importando a classe Calculadora para que EntradaUsuario possa usá-la
from calculadora import Calculadora

class EntradaUsuario:
    """
    Classe responsável pela lógica de entrada do usuário.
    """
    def __init__(self):
        # Instancia Calculadora internamente
        self.calculadora = Calculadora()

    def obter_operacao(self):
        while True:
            # A exibição do menu será feita pela classe Apresentacao
            operacao = input("Escolha sua operação (➕, ➖, ✖️, ➗) ou 'sair': ").strip().lower()
            
            if operacao == 'sair' or operacao == '🚪':
                return 'sair'
            elif operacao == '+' or operacao == '➕':
                return 'somar'
            elif operacao == '-' or operacao == '➖':
                return 'subtrair'
            elif operacao == '*' or operacao == '✖️':
                return 'multiplicar'
            elif operacao == '/' or operacao == '➗':
                return 'dividir'
            else:
                # A exibição de erro será tratada pela classe Apresentacao
                print("Operação inválida. Por favor, escolha um dos símbolos ou 'sair'.")

    def obter_numeros(self):
        while True:
            try:
                num1_str = input("Digite o primeiro número: ")
                num1 = float(num1_str)
                num2_str = input("Digite o segundo número: ")
                num2 = float(num2_str)
                return num1, num2
            except ValueError:
                # A exibição de erro será tratada pela classe Apresentacao
                print("Entrada inválida. Por favor, digite apenas números. 🔢")
