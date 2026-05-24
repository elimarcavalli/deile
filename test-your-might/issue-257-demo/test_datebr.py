import pytest
from datetime import datetime
from datebr import format_br


def test_format_basic():
    """datetime(2025, 5, 23, 14, 30) → '23/05/2025 14:30'"""
    assert format_br(datetime(2025, 5, 23, 14, 30)) == "23/05/2025 14:30"


def test_format_single_digit():
    """datetime(2025, 1, 3, 9, 5) → '03/01/2025 09:05'"""
    assert format_br(datetime(2025, 1, 3, 9, 5)) == "03/01/2025 09:05"


def test_format_year_end():
    """datetime(1999, 12, 31, 23, 59) → '31/12/1999 23:59'"""
    assert format_br(datetime(1999, 12, 31, 23, 59)) == "31/12/1999 23:59"


def test_format_midnight():
    """datetime(2024, 6, 15, 0, 0) → '15/06/2024 00:00'"""
    assert format_br(datetime(2024, 6, 15, 0, 0)) == "15/06/2024 00:00"


def test_format_type_error():
    """Chama format_br('string') e espera TypeError ou AttributeError."""
    with pytest.raises((TypeError, AttributeError)):
        format_br("string")
