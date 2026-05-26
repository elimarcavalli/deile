"""Runtime state — estado vivo por-processo publicado em ``~/.deile/run/``.

Separado de ``deile/memory/`` por contrato: memória é persistente, por-sessão,
com camadas de propósito (working/episodic/semantic/procedural); runtime state
é volátil, por-processo, e expõe metadados de execução para introspecção
externa (painel TUI, observabilidade futura).

API pública (Fase 1 — state file + heartbeat):

    from deile.runtime import InstanceState, get_instance_state, pid_alive

API pública (Fases 2/3 — status server + registry):

    from deile.runtime import StatusServer, StatusClient
    from deile.runtime import Registry, RegistryEntry

Ver issue #303 e ``docs/system_design/DECISOES.md`` #35 (state file + heartbeat),
#36 (status server + registry).
"""

from deile.runtime.instance_state import (InstanceState, get_instance_state,
                                          peek_instance_state, pid_alive,
                                          reset_instance_state)
from deile.runtime.registry import (REGISTRY_SCHEMA_VERSION, Registry,
                                    RegistryEntry)
from deile.runtime.status_server import (MAX_LINE_BYTES, StatusClient,
                                         StatusServer, format_metrics)

__all__ = [
    # instance_state
    "InstanceState",
    "get_instance_state",
    "peek_instance_state",
    "pid_alive",
    "reset_instance_state",
    # status_server
    "StatusServer",
    "StatusClient",
    "format_metrics",
    "MAX_LINE_BYTES",
    # registry
    "Registry",
    "RegistryEntry",
    "REGISTRY_SCHEMA_VERSION",
]
