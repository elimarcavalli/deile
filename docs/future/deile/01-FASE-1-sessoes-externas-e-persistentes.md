# Fase 1 — Sessões externas e persistentes

> Permitir que o bot crie sessões com `session_id` estável (`bot_session_<bot_user_id>`) que sobrevivam a restart do processo.

## Pré-requisitos

- Branch própria: `feat/deile-external-sessions`.
- Conhecimento do `DeileAgent.create_session` atual (ver `deile/core/agent.py`).
- `aiosqlite` no projeto (foundation já adiciona).

## Entregáveis

### 1.1. Schema SQLite (em `deile/storage/sessions/`)

```sql
CREATE TABLE IF NOT EXISTS persisted_session (
    session_id          TEXT PRIMARY KEY,
    working_directory   TEXT NOT NULL,
    context_data_json   TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    last_used_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_last_used ON persisted_session(last_used_at);
```

Caminho default: `./data/deile_sessions.sqlite`.

### 1.2. `SessionStore` em `deile/core/session_store.py`

```python
class SessionStore:
    def __init__(self, db_path: Path): ...
    async def init(self) -> None: ...

    async def get(self, session_id: str) -> Optional[PersistedSessionRow]: ...
    async def upsert(self, session_id: str, working_directory: str, context_data: dict) -> None: ...
    async def touch(self, session_id: str) -> None: ...
    async def purge_older_than(self, days: int) -> int: ...
```

### 1.3. `DeileAgent.get_or_create_session`

```python
async def get_or_create_session(
    self,
    session_id: str,
    working_directory: Optional[str] = None,
    *,
    persisted: bool = False,
) -> Session:
    """
    Se session_id já existe na memória do agente, devolve.
    Se persisted=True e existe no SessionStore, ressuscita (carrega context_data).
    Senão, cria nova e (se persisted) registra no store.
    """
```

Behavior:

- `persisted=False` (default da CLI): comportamento atual; sessão vive só em memória.
- `persisted=True`: registra no `SessionStore` no create; toda mudança em `context_data` é flushed (debounce 500ms para não escrever a cada turno).

### 1.4. Sessão snapshot/resume

Adicionar a `Session` (em `deile/core/session.py`) os métodos:

```python
def snapshot(self) -> dict: ...        # serializa context_data + working_directory + metadata
@classmethod
def from_snapshot(cls, snap: dict) -> "Session": ...
```

### 1.5. Ciclo de vida

- `agent.shutdown()` (se não existir, criar) faz `session_store.flush_all()` antes de fechar.
- Comando admin `python3 -m deile.tools.session_admin --purge --older-than-days N` para limpeza manual.

### 1.6. Testes

- Criar sessão persistente, mutar `context_data["foo"] = "bar"`, esperar debounce, fechar agente, abrir agente novo, `get_or_create_session(...)` devolve sessão com `context_data["foo"] == "bar"`.
- `purge_older_than` remove sessões antigas e devolve count.
- `persisted=False` não toca no store.

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | `pytest deile/tests/core/test_session_store.py` passa |
| AC-2 | CLI atual sem mudança visível (smoke) |
| AC-3 | Sessão persistente sobrevive restart (teste E2E) |
| AC-4 | `purge_older_than(days=0)` apaga tudo; `days=1000` não apaga nada novo |

## Pontos de atenção

- **Debounce** evita write amplification em conversas rápidas.
- **Não persistir secrets**: scanner do `deile/security/secrets_scanner.py` deve ser aplicado a `context_data` antes de serializar; valores que casam com padrões de segredo são redacted no JSON.
- **Migração**: primeira execução cria schema; sem migração de dados antigos (sessões CLI eram transient mesmo).

## Estimativa

1.5 dia.
