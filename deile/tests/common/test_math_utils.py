"""Tests for deile.common.math_utils."""

from deile.common.math_utils import somar


class TestSomar:
    """Coverage: positivos, negativos, zero, sinais mistos, comutatividade, grandes."""

    def test_soma_positivos(self) -> None:
        assert somar(2, 3) == 5

    def test_soma_negativos(self) -> None:
        assert somar(-1, -2) == -3

    def test_soma_positivo_com_zero(self) -> None:
        assert somar(5, 0) == 5

    def test_soma_negativo_com_zero(self) -> None:
        assert somar(-7, 0) == -7

    def test_soma_zero_com_zero(self) -> None:
        assert somar(0, 0) == 0

    def test_soma_sinais_mistos(self) -> None:
        assert somar(-3, 5) == 2

    def test_soma_comutativa(self) -> None:
        assert somar(2, 3) == somar(3, 2)

    def test_soma_inteiros_grandes(self) -> None:
        assert somar(10**18, 10**18) == 2 * 10**18
