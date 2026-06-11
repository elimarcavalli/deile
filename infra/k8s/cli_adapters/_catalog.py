"""Catálogo OpenRouter compartilhado entre adapters de CLI workers (#614 follow-up).

Quatro :class:`ModelInfo` apareciam IDENTICAMENTE (mesmo ``id``/``price_in``/
``price_out``/``context``/``provider``) em três adapters: ``opencode.py``,
``goose.py`` e ``aider.py``. Mudar o preço do DeepSeek V4 Flash exigiria
editar 3 arquivos — drift potencial real.

Centralizado aqui com ``notes`` defaults razoáveis. Cada adapter monta sua
lista incluindo as constantes diretamente (quando a ``notes`` default serve)
ou via ``dataclasses.replace(MODEL, notes='...')`` (variantes locais —
ex.: o aider descreve o claude-sonnet como "tarefas cirúrgicas críticas"
porque o aider É a frente cirúrgica).

Modulo privado por convenção (``_catalog``) — não é API pública dos adapters.
"""

from __future__ import annotations

from .base import ModelInfo

#: DeepSeek V4 Flash via OpenRouter — mais barato do catálogo coding.
OPENROUTER_DEEPSEEK_V4_FLASH = ModelInfo(
    id="openrouter/deepseek/deepseek-v4-flash",
    label="DeepSeek V4 Flash (OpenRouter)",
    provider="openrouter",
    price_in=0.0983, price_out=0.1966, context=1_048_576,
    notes="MAIS BARATO de coding; default recomendado",
)

#: DeepSeek V4 Pro via OpenRouter — melhor custo-benefício de coding.
OPENROUTER_DEEPSEEK_V4_PRO = ModelInfo(
    id="openrouter/deepseek/deepseek-v4-pro",
    label="DeepSeek V4 Pro (OpenRouter)",
    provider="openrouter",
    price_in=0.435, price_out=0.87, context=1_048_576,
    notes="MELHOR custo-benefício de coding (promo)",
)

#: Claude Sonnet 4.6 via OpenRouter — premium para review/arquitetura.
OPENROUTER_CLAUDE_SONNET_4_6 = ModelInfo(
    id="openrouter/anthropic/claude-sonnet-4.6",
    label="Claude Sonnet 4.6 (OpenRouter)",
    provider="openrouter",
    price_in=3.00, price_out=15.00, context=1_000_000,
    notes="premium; review crítico / arquitetura",
)

#: Qwen3 Coder 480B via OpenRouter — bom custo-benefício de implementação.
OPENROUTER_QWEN3_CODER = ModelInfo(
    id="openrouter/qwen/qwen3-coder",
    label="Qwen3 Coder 480B (OpenRouter)",
    provider="openrouter",
    price_in=0.22, price_out=1.80, context=1_000_000,
    notes="bom custo-benefício p/ implementação",
)


__all__ = [
    "OPENROUTER_DEEPSEEK_V4_FLASH",
    "OPENROUTER_DEEPSEEK_V4_PRO",
    "OPENROUTER_CLAUDE_SONNET_4_6",
    "OPENROUTER_QWEN3_CODER",
]
