from datetime import datetime
from datebr import format_br


def test_meio_2026():
    """23/05/2026 15:30 deve formatar corretamente."""
    dt = datetime(2026, 5, 23, 15, 30)
    assert format_br(dt) == "23/05/2026 15:30"


def test_ano_novo():
    """01/01/2025 00:00 deve formatar corretamente."""
    dt = datetime(2025, 1, 1, 0, 0)
    assert format_br(dt) == "01/01/2025 00:00"


def test_borda_mes():
    """31/01/2024 23:59 — borda de mês."""
    dt = datetime(2024, 1, 31, 23, 59)
    assert format_br(dt) == "31/01/2024 23:59"


def test_ano_bissexto():
    """29/02/2024 12:00 — ano bissexto."""
    dt = datetime(2024, 2, 29, 12, 0)
    assert format_br(dt) == "29/02/2024 12:00"


def test_borda_ano():
    """31/12/2026 00:00 — virada de ano."""
    dt = datetime(2026, 12, 31, 0, 0)
    assert format_br(dt) == "31/12/2026 00:00"
