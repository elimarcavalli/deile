from datetime import datetime


def format_br(dt: datetime) -> str:
    """Recebe um objeto datetime e retorna string no formato DD/MM/AAAA HH:MM.

    Exemplo: datetime(2025, 5, 23, 14, 30) → '23/05/2025 14:30'
    Usa strftime com locale independente (não depende de locale pt_BR).
    """
    return dt.strftime("%d/%m/%Y %H:%M")
