"""Calculadora minimalista — quatro operações básicas via linha de comando.

Uso:
    python -m deile.calculator 4 + 5
    python -m deile.calculator 10 / 3
    python -m deile.calculator 7 x 2    # ou '*' na shell

Zero dependências além da stdlib.
"""

from __future__ import annotations

import sys
from typing import Union


def calculate(a: Union[int, float], op: str, b: Union[int, float]) -> Union[int, float, str]:
    """Executa operação aritmética entre *a* e *b*.

    Args:
        a: Primeiro operando.
        op: Operador — ``+``, ``-``, ``x``, ``*``, ``/``.
        b: Segundo operando.

    Returns:
        Resultado numérico, ou string de erro para divisão por zero
        ou operador inválido.
    """
    if op in ("x", "*"):
        return a * b
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "/":
        if b == 0:
            return "Erro: divisão por zero"
        return a / b
    return f"Erro: operador '{op}' desconhecido (use +, -, x, /)"


def parse_and_calculate(args: list[str]) -> str:
    """Parseia ``sys.argv[1:]`` e retorna string formatada com o resultado.

    Aceita exatamente 3 argumentos: ``<a> <op> <b>``.
    ``a`` e ``b`` podem ser inteiros ou floats.
    ``op`` pode ser ``+``, ``-``, ``x``, ``*`` ou ``/``.
    """
    if len(args) != 3:
        return (
            "Uso: deile calculator <a> <op> <b>\n"
            "     deile calculator 4 + 5\n"
            "     deile calculator 10 / 3\n"
            "     deile calculator 7 x 2\n\n"
            f"Recebidos {len(args)} argumento(s): {args}"
        )
    a_str, op, b_str = args
    try:
        a = float(a_str) if "." in a_str else int(a_str)
        b = float(b_str) if "." in b_str else int(b_str)
    except ValueError:
        return f"Erro: operandos devem ser numéricos (recebidos: {a_str!r}, {b_str!r})"
    result = calculate(a, op, b)
    if isinstance(result, str):
        return result
    # Preserva inteiro quando resultado é exato
    if isinstance(result, float) and result == result // 1:
        return str(int(result))
    return str(result)


def main(argv: list[str] | None = None) -> int:
    """Entry point para ``python -m deile.calculator``."""
    if argv is None:
        argv = sys.argv[1:]
    print(parse_and_calculate(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
