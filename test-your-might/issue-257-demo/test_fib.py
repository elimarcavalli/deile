import pytest
from fib import fibonacci


def test_fibonacci_0():
    assert fibonacci(0) == 0


def test_fibonacci_1():
    assert fibonacci(1) == 1


def test_fibonacci_2():
    assert fibonacci(2) == 1


def test_fibonacci_5():
    assert fibonacci(5) == 5


def test_fibonacci_10():
    assert fibonacci(10) == 55


def test_fibonacci_negative():
    with pytest.raises(ValueError, match="n must be non-negative"):
        fibonacci(-1)
