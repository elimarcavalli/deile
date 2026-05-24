import pytest
from fib import fibonacci


def test_fib_zero():
    assert fibonacci(0) == 0


def test_fib_one():
    assert fibonacci(1) == 1


def test_fib_small():
    assert fibonacci(10) == 55


def test_fib_large():
    assert fibonacci(30) == 832040


def test_fib_negative():
    with pytest.raises(ValueError):
        fibonacci(-1)
