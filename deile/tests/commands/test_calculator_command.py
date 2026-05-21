"""Testes do comando /calculator (issue #247).

Cobre:
  - Operações básicas: soma, subtração, multiplicação, divisão
  - Divisão por zero
  - Operador inválido
  - Argumentos faltando
  - Argumentos não numéricos
  - Operandos float
  - Operador x e * (ambos multiplicação)
  - Registro como comando no registry
  - Metadados do CLI flag
"""

from __future__ import annotations

from deile.calculator import calculate, parse_and_calculate
from deile.commands.base import CommandContext
from deile.commands.builtin.calculator_command import CalculatorCommand


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(args: str = "") -> CommandContext:
    return CommandContext(user_input=f"/calculator {args}", args=args)


def _cmd() -> CalculatorCommand:
    return CalculatorCommand()


# ---------------------------------------------------------------------------
# Testes da função calculate (unitários)
# ---------------------------------------------------------------------------

class TestCalculateFunction:
    def test_soma_inteiros(self):
        assert calculate(2, "+", 3) == 5

    def test_soma_floats(self):
        assert calculate(2.5, "+", 3.1) == 5.6

    def test_subtracao(self):
        assert calculate(10, "-", 4) == 6

    def test_subtracao_negativo(self):
        assert calculate(3, "-", 10) == -7

    def test_multiplicacao_x(self):
        assert calculate(3, "x", 4) == 12

    def test_multiplicacao_asterisco(self):
        assert calculate(3, "*", 4) == 12

    def test_multiplicacao_float(self):
        assert calculate(2.5, "x", 2) == 5.0

    def test_divisao_exata(self):
        assert calculate(10, "/", 2) == 5

    def test_divisao_nao_exata(self):
        assert calculate(10, "/", 3) == 10 / 3

    def test_divisao_por_zero(self):
        assert "divisão por zero" in calculate(1, "/", 0)

    def test_operador_invalido(self):
        result = calculate(1, "^", 2)
        assert "desconhecido" in result
        assert "^" in result

    def test_zero_dividido_por_numero(self):
        assert calculate(0, "/", 5) == 0


# ---------------------------------------------------------------------------
# Testes do parse_and_calculate
# ---------------------------------------------------------------------------

class TestParseAndCalculate:
    def test_uso_basico_soma(self):
        assert parse_and_calculate(["4", "+", "5"]) == "9"

    def test_uso_basico_subtracao(self):
        assert parse_and_calculate(["100", "-", "1"]) == "99"

    def test_uso_basico_multiplicacao(self):
        assert parse_and_calculate(["7", "x", "3"]) == "21"

    def test_uso_basico_divisao(self):
        assert parse_and_calculate(["10", "/", "2"]) == "5"

    def test_divisao_float(self):
        result = parse_and_calculate(["10", "/", "3"])
        assert result == str(10 / 3)

    def test_argumentos_insuficientes(self):
        result = parse_and_calculate(["4", "+"])
        assert "Uso:" in result
        assert "2 argumento(s)" in result or "Recebidos 2 argumento(s)" in result

    def test_argumentos_demais(self):
        result = parse_and_calculate(["1", "+", "2", "+", "3"])
        assert "Uso:" in result

    def nenhum_argumento(self):
        result = parse_and_calculate([])
        assert "Uso:" in result

    def test_operandos_nao_numericos(self):
        result = parse_and_calculate(["a", "+", "b"])
        assert "numéricos" in result

    def test_operando_float_string(self):
        # 2.5 + 3.5 = 6.0 → a calculadora preserva inteiro (6)
        assert parse_and_calculate(["2.5", "+", "3.5"]) == "6"

    def test_zero_args_retorna_uso(self):
        result = parse_and_calculate([])
        assert result.startswith("Uso:")


# ---------------------------------------------------------------------------
# Testes do slash command
# ---------------------------------------------------------------------------

class TestCalculatorCommandBasic:
    async def test_command_exists_and_is_direct(self):
        cmd = _cmd()
        assert cmd.name == "calculator"
        assert not cmd.has_prompt_template  # DirectCommand

    async def test_aliases_include_calc(self):
        cmd = _cmd()
        assert "calc" in cmd.aliases

    async def test_category_is_utility(self):
        cmd = _cmd()
        assert cmd.category == "utility"

    async def test_description_not_empty(self):
        cmd = _cmd()
        assert len(cmd.description) > 0

    async def test_soma_via_command(self):
        result = await _cmd().execute(_ctx("4 + 5"))
        assert result.success
        assert result.content == "9"

    async def test_subtracao_via_command(self):
        result = await _cmd().execute(_ctx("100 - 1"))
        assert result.success
        assert result.content == "99"

    async def test_multiplicacao_via_command(self):
        result = await _cmd().execute(_ctx("7 x 3"))
        assert result.success
        assert result.content == "21"

    async def test_divisao_via_command(self):
        result = await _cmd().execute(_ctx("10 / 2"))
        assert result.success
        assert result.content == "5"

    async def test_divisao_por_zero_via_command(self):
        result = await _cmd().execute(_ctx("1 / 0"))
        assert result.success
        assert "divisão por zero" in result.content

    async def test_args_vazios_mostra_uso(self):
        result = await _cmd().execute(_ctx(""))
        assert result.success
        assert "Uso:" in result.content

    async def test_invalid_op_via_command(self):
        result = await _cmd().execute(_ctx("2 ^ 3"))
        assert result.success
        assert "desconhecido" in result.content

    async def test_non_numeric_args(self):
        result = await _cmd().execute(_ctx("a + b"))
        assert result.success
        assert "numéricos" in result.content

    async def test_content_type_is_text(self):
        result = await _cmd().execute(_ctx("4 + 5"))
        assert result.content_type == "text"


# ---------------------------------------------------------------------------
# Testes do CLI flag metadata
# ---------------------------------------------------------------------------

class TestCLIFlagMetadata:
    def test_cli_flag_exists(self):
        assert CalculatorCommand.cli_flag == "--calculator"

    def test_cli_flag_aliases(self):
        assert "-c" in CalculatorCommand.cli_flag_aliases

    def test_cli_takes_arg(self):
        assert CalculatorCommand.cli_takes_arg is True

    def test_cli_requires_provider(self):
        assert CalculatorCommand.cli_requires_provider is False

    def test_cli_help_not_empty(self):
        assert len(CalculatorCommand.cli_help) > 0

    def test_cli_arg_metavar_not_empty(self):
        assert len(CalculatorCommand.cli_arg_metavar) > 0


# ---------------------------------------------------------------------------
# Testes de registro no registry
# ---------------------------------------------------------------------------

class TestCommandRegistration:
    async def test_registry_discovers_calculator(self):
        from deile.commands.registry import get_command_registry
        registry = get_command_registry()
        registry.clear()
        count = registry.auto_discover_builtin_commands()
        assert count > 0
        cmd = registry.get_command("calculator")
        assert cmd is not None
        assert cmd.name == "calculator"

    async def test_registry_alias_calc(self):
        from deile.commands.registry import get_command_registry
        registry = get_command_registry()
        cmd = registry.get_command("calc")
        assert cmd is not None
        assert cmd.name == "calculator"

    async def test_registry_executes_via_registry(self):
        from deile.commands.registry import get_command_registry
        registry = get_command_registry()
        result = await registry.execute_command("calculator", _ctx("10 + 20"))
        assert result.success
        assert result.content == "30"
