def format_br(dt):
    """Formata um objeto datetime no padrão DD/MM/AAAA HH:MM."""
    return f"{dt:%d/%m/%Y %H:%M}"
