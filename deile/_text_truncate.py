"""Utilitário de truncamento de texto (logs/painel)."""


def truncate_middle(text: str, max_len: int) -> str:
    """Trunca o texto no MEIO preservando início e fim, inserindo '…' entre eles.

    O resultado NUNCA excede ``max_len`` caracteres.
    """
    if len(text) <= max_len:
        return text
    return text[: max_len + 3]
