"""Helpers compartilhados pelos comandos builtin.

Centraliza padrões repetidos (parsing de args, painéis Rich, mapas
PT-BR de descrições, recuperação de subsistemas via `context.agent`)
que apareciam duplicados em vários `*_command.py`.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..base import CommandContext


# Descrições PT-BR para cada feature flag declarada em `deile.__version__.FEATURES`.
# Consumido pelos comandos /version e /welcome — mantenha em sincronia com
# `__version__.FEATURES`.
PROJECT_LINKS: Dict[str, str] = {
    "Repositório": "https://github.com/elimarcavalli/deile",
    "Documentação": "docs/system_design/00-VISAO-GERAL.md",
    "Licença": "MIT — https://opensource.org/licenses/MIT",
    "Issues": "https://github.com/elimarcavalli/deile/issues",
}


FLAG_DESCRICOES_PTBR: Dict[str, str] = {
    "orchestration": "Orquestração multi-step e gestão de planos",
    "security": "Permissões, audit log e sandbox",
    "ui_polish": "Interface polida e atalhos de teclado",
    "testing": "Suíte de testes automatizados",
    "ci_cd": "Integração e entrega contínua",
    "documentation": "Documentação estruturada por pilares",
    "events": "Arquitetura orientada a eventos",
    "evolution": "Motor de auto-aprendizado",
    "memory": "Memória em quatro camadas (working/episodic/semantic/procedural)",
    "personas": "Troca dinâmica de personas",
    "plugins": "Arquitetura extensível de plugins",
    "config_profiles": "Perfis de configuração por ambiente",
}


def split_args(context: CommandContext) -> List[str]:
    """Tokeniza `context.args` em uma lista de palavras.

    Trata `args` ausente, vazio ou só com espaços como `[]`. Substitui
    o idioma `args = context.args if hasattr(context, "args") else ""`
    seguido de `parts = args.strip().split() if args.strip() else []`.
    """
    raw = getattr(context, "args", "") or ""
    stripped = raw.strip()
    return stripped.split() if stripped else []
