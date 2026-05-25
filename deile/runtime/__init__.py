"""Runtime state — estado vivo por-processo publicado em ``~/.deile/run/``.

Separado de ``deile/memory/`` por contrato: memória é persistente, por-sessão,
com camadas de propósito (working/episodic/semantic/procedural); runtime state
é volátil, por-processo, e expõe metadados de execução para introspecção
externa (painel TUI, observabilidade futura).

API pública:

    from deile.runtime.instance_state import (
        InstanceState, get_instance_state, pid_alive
    )

Ver issue #303 e ``docs/system_design/DECISOES.md`` #34 (runtime state).
"""

from deile.runtime.instance_state import (InstanceState, get_instance_state,
                                          pid_alive, reset_instance_state)

__all__ = [
    "InstanceState",
    "get_instance_state",
    "pid_alive",
    "reset_instance_state",
]
