def fibonacci(n: int) -> int:
    """Return the n-th Fibonacci number (0-indexed).

    Args:
        n: A non-negative integer.

    Returns:
        The n-th Fibonacci number (fibonacci(0)=0, fibonacci(1)=1).

    Raises:
        ValueError: If n is negative.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n == 0:
        return 0
    if n == 1:
        return 1

    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b
