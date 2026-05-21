"""Comando /calculator — calculadora minimalista via CLI (issue #247).

Uso:
    /calculator 4 + 5
    /calculator 10 / 3
    /calculator 7 x 2
    deile --calculator "4 + 5"

Zero dependências externas. Quatro operações: +, -, x/*, /.
"""

from __future__ import annotations

from ..base import CommandContext, CommandResult, DirectCommand


class CalculatorCommand(DirectCommand):
    """``/calculator`` — executa operação aritmética básica."""

    cli_flag = "--calculator"
    cli_flag_aliases = ["-c"]
    cli_takes_arg = True
    cli_arg_metavar = '"A + B"'
    cli_help = "Calculadora: 'deile --calculator \"4 + 5\"' → 9"
    cli_subcommand = None
    cli_requires_provider = False

    def __init__(self) -> None:
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="calculator",
            description="Calculadora minimalista: soma, subtração, multiplicação e divisão.",
            aliases=["calc"],
        )
        super().__init__(config)
        self.category = "utility"

    async def execute(self, context: CommandContext) -> CommandResult:
        from deile.calculator import parse_and_calculate

        args = context.args.strip()
        if not args:
            return CommandResult.success_result(
                "Uso: /calculator <a> <op> <b>\n"
                "     /calculator 4 + 5      → 9\n"
                "     /calculator 10 / 3     → 3.333...\n"
                "     /calculator 7 x 2      → 14\n\n"
                "Operadores: +, -, x, *, /"
            )

        # Converte args string em lista de tokens
        # Ex: "4 + 5" → ["4", "+", "5"]; "10 / 3" → ["10", "/", "3"]
        tokens = args.split()
        result = parse_and_calculate(tokens)
        return CommandResult.success_result(result, "text")
