"""Validação de bearer tokens compartilhada entre os clientes de infra.

Centraliza a regex ``_TOKEN_SAFE_CHARS`` e a função ``_validate_token_charset``
que eram duplicadas em ``deile_worker_client`` e ``deile_monitor_client``
(issue #765 — consolidação). Ambas são publicamente importáveis deste módulo
para que os clientes de infra (e os testes) importem de um único lugar.

Não importa nenhum módulo do deile — apenas stdlib — para que este módulo
possa ser importado antes do bootstrap completo do agente.
"""

from __future__ import annotations

import re

# Tokens são tratados como bearer values: rejeitamos qualquer caractere
# que possa quebrar o header HTTP (CR, LF, NUL) — defense-in-depth contra
# header injection em caso de secret file corrompido. O floor de 16
# caracteres alinha com ``secrets_scanner`` (``DEILE_BOT_AUTH_TOKEN`` /
# ``DEILE_WORKER_BEARER_TOKEN`` exigem ``{16,}`` no scanner — ver pilar
# 08 §"Padrões cobertos"); manter o floor uniforme garante que o scanner
# e o validador concordem sobre o que é "token plausível".
_TOKEN_SAFE_CHARS = re.compile(r"^[A-Za-z0-9._\-+/=:~]{16,4096}$")


def _validate_token_charset(token: str) -> bool:
    """True se ``token`` não tem CR/LF/NUL e cabe no charset bearer comum."""
    return bool(_TOKEN_SAFE_CHARS.match(token))
