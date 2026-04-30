# test-your-might/calculadora/main.py

# Importando as classes necessárias
from apresentacao import Apresentacao
from entrada_usuario import EntradaUsuario
# A classe Calculadora é instanciada dentro de EntradaUsuario, então não precisa ser importada aqui diretamente

class App:
    """
    Classe principal que orquestra a execução da calculadora com interface aprimorada.
    """
    def __init__(self):
        self.apresentacao = Apresentacao()
        self.entrada = EntradaUsuario()

    def executar(self):
        self.apresentacao.mensagem_boas_vindas()
        while True:
            self.apresentacao.limpar_tela()
            self.apresentacao.exibir_menu()
            operacao_escolhida = self.entrada.obter_operacao()
            
            if operacao_escolhida == 'sair':
                self.apresentacao.mensagem_despedida()
                break

            num1, num2 = self.entrada.obter_numeros()

            try:
                resultado = None
                op_nome = ""
                # A lógica de cálculo é feita através da instância de Calculadora dentro de EntradaUsuario
                if operacao_escolhida == 'somar':
                    resultado = self.entrada.calculadora.somar(num1, num2)
                    op_nome = "soma"
                elif operacao_escolhida == 'subtrair':
                    resultado = self.entrada.calculadora.subtrair(num1, num2)
                    op_nome = "subtração"
                elif operacao_escolhida == 'multiplicar':
                    resultado = self.entrada.calculadora.multiplicar(num1, num2)
                    op_nome = "multiplicação"
                elif operacao_escolhida == 'dividir':
                    resultado = self.entrada.calculadora.dividir(num1, num2)
                    op_nome = "divisão"
                
                if resultado is not None:
                    self.apresentacao.exibir_resultado(resultado, op_nome)

            except ValueError as e:
                self.apresentacao.exibir_erro(str(e))
            except Exception as e:
                self.apresentacao.exibir_erro(f"Ocorreu um erro inesperado: {e}")

            self.apresentacao.prompt_continuar()

if __name__ == "__main__":
    app = App()
    app.executar()
