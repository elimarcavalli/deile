"""Pure mathematical utility functions."""


def somar(a: int, b: int) -> int:
    """Retorna a soma de dois números inteiros.

    Função pura — sem efeitos colaterais, determinística.

    Args:
        a: Primeiro inteiro.
        b: Segundo inteiro.

    Returns:
        A soma de a e b como inteiro.

    Examples:
        >>> somar(2, 3)
        5
        >>> somar(-1, -2)
        -3
        >>> somar(5, 0)
        5
    """
    return a + b
