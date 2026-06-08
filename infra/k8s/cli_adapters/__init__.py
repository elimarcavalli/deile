#!/usr/bin/env python3
"""cli_adapters — registro auto-descoberto dos adapters de CLI worker.

**Single source of truth da frota.** Este pacote é escaneado em import: cada
módulo ``<kind>.py`` (que não comece com ``_`` e não seja ``base``) é importado
e inspecionado por uma instância que satisfaça o Protocol
:class:`~cli_adapters.base.CliAdapter`. O dicionário resultante
``ADAPTERS = {kind: adapter}`` é consumido por TODOS os pontos da frota
(``dispatch_resolver`` deriva ``VALID_DISPATCHERS`` daqui, o painel deriva a
lista de workers, ``deploy.py gen-worker`` lê os metadados, a NetworkPolicy lê
``egress_hosts``). Adicionar um worker = criar ``cli_adapters/<kind>.py`` com um
adapter; **nenhum consumidor é editado**.

Convenção de descoberta — um módulo participa do registro se expõe **um** de:

* ``ADAPTER``  — atributo apontando para a instância do adapter (preferido);
* ``get_adapter()`` — factory que retorna a instância;
* uma única subclasse/instância detectável que satisfaça ``CliAdapter`` via
  varredura dos atributos do módulo (fallback).

A descoberta é tolerante a falhas: um módulo que estoure no import é logado e
PULADO (não derruba o registro inteiro) — um adapter quebrado não pode impedir
os demais de carregarem. O ``base.py`` e módulos ``_privados`` nunca são
escaneados.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Dict

from .base import (AuthMode, BaseCliAdapter, CliAdapter, GitStrategy,
                   ModelAuth, ModelInfo, OAuthSpec, ResumeCtx, WorkResult)

logger = logging.getLogger("deile.cli_adapters")

#: Módulos que nunca são tratados como adapters (infra do pacote).
_SKIP_MODULES = frozenset({"base"})


def _extract_adapter(module) -> object | None:
    """Extrai a instância de adapter de um módulo, ou ``None`` se não houver.

    Ordem de resolução (primeira que existir vence):
    1. ``module.ADAPTER`` — instância exportada explicitamente.
    2. ``module.get_adapter()`` — factory.
    3. varredura: primeiro atributo público que satisfaça ``CliAdapter`` e não
       seja a própria base/Protocol.
    """
    candidate = getattr(module, "ADAPTER", None)
    if candidate is not None:
        return candidate

    factory = getattr(module, "get_adapter", None)
    if callable(factory):
        return factory()

    for attr_name in dir(module):
        if attr_name.startswith("_"):
            continue
        obj = getattr(module, attr_name)
        # Exclui as próprias classes-base/contrato re-exportadas.
        if obj in (CliAdapter, BaseCliAdapter):
            continue
        if isinstance(obj, CliAdapter):
            return obj
    return None


def _discover() -> Dict[str, CliAdapter]:
    """Escaneia o pacote e monta o mapa ``{kind: adapter}``.

    Itera os módulos do próprio pacote via :mod:`pkgutil`, importa cada um e
    extrai seu adapter. Colisão de ``kind`` é logada (warning) e o primeiro
    registrado prevalece — a descoberta é determinística pela ordem alfabética
    de :func:`pkgutil.iter_modules`.
    """
    registry: Dict[str, CliAdapter] = {}
    for mod_info in pkgutil.iter_modules(__path__):
        name = mod_info.name
        if name in _SKIP_MODULES or name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:  # noqa: BLE001 — um adapter quebrado não derruba os demais
            logger.warning("falha ao importar adapter %r: %s", name, exc)
            continue

        adapter = _extract_adapter(module)
        if adapter is None:
            logger.debug("módulo %r não expõe um CliAdapter — ignorado", name)
            continue
        if not isinstance(adapter, CliAdapter):
            logger.warning(
                "objeto de %r não satisfaz CliAdapter (faltam atributos/métodos)"
                " — ignorado", name,
            )
            continue

        kind = getattr(adapter, "kind", "") or ""
        if not kind:
            logger.warning("adapter de %r não declara 'kind' — ignorado", name)
            continue
        if kind in registry:
            logger.warning(
                "kind %r duplicado (módulo %r); mantendo o primeiro registrado",
                kind, name,
            )
            continue
        registry[kind] = adapter
        logger.debug("adapter registrado: kind=%r (módulo %r)", kind, name)
    return registry


def reload_adapters() -> Dict[str, CliAdapter]:
    """Re-escaneia o pacote e ATUALIZA o ``ADAPTERS`` global in-place.

    Usado em testes que dropam um adapter sintético no pacote em runtime. Muta o
    dicionário existente (em vez de rebind) para que referências já capturadas
    pelos consumidores continuem válidas.
    """
    fresh = _discover()
    ADAPTERS.clear()
    ADAPTERS.update(fresh)
    return ADAPTERS


def get_adapter(kind: str) -> CliAdapter:
    """Retorna o adapter registrado para ``kind`` ou levanta ``KeyError``.

    Usado pelo ``cli_worker_server`` para resolver o adapter selecionado por
    ``DEILE_CLI_WORKER_KIND``.
    """
    try:
        return ADAPTERS[kind]
    except KeyError:
        raise KeyError(
            f"nenhum CLI adapter registrado para kind={kind!r}; "
            f"conhecidos: {sorted(ADAPTERS)}"
        ) from None


#: Mapa ``{kind: adapter}`` montado em import — a fonte única da frota.
ADAPTERS: Dict[str, CliAdapter] = _discover()


__all__ = [
    "ADAPTERS",
    "get_adapter",
    "reload_adapters",
    "CliAdapter",
    "BaseCliAdapter",
    "WorkResult",
    "ResumeCtx",
    "ModelInfo",
    "OAuthSpec",
    "AuthMode",
    "ModelAuth",
    "GitStrategy",
]
