def fibonacci(n: int) -> int:
    """Return the n-th Fibonacci number (0-indexed, F(0)=0, F(1)=1).

    Args:
        n: Non-negative integer index.

    Returns:
        The n-th Fibonacci number.

    Raises:
        ValueError: If n is negative.
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b
