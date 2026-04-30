# test-your-might/calculadora/calculadora.py

class Calculadora:
    """
    Classe responsável pelas operações matemáticas básicas.
    """
    def somar(self, a, b):
        return a + b

    def subtrair(self, a, b):
        return a - b

    def multiplicar(self, a, b):
        return a * b

    def dividir(self, a, b):
        if b == 0:
            raise ValueError("Erro: Divisão por zero não é permitida. 🚫")
        return a / b
