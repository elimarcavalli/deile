# Fase 2 — Serviços core

> Identidade, permissões, rate limit, persistência de conversa, audit, intent classifier. Tudo o que toca disco/estado vive aqui. Termina com a foundation capaz de **classificar e persistir** uma mensagem, mas ainda sem invocar o agente.

## Pré-requisitos

- Fase 1 mergeada e verde no CI.
- `aiosqlite`, `pydantic>=2`, `tenacity` instalados.
- Decisão sobre o caminho do SQLite (recomendado: `./data/deile_bot.sqlite`, mesmo arquivo do `deile/storage/` se possível, para reutilizar conexões — confirmar com `06-MEMORIA.md` antes).

## Entregáveis

### 2.1. `foundation/conversation_store.py` — Persistência SQLite

Schema (em `deile_bot/foundation/sql/V001__init.sql`):

```sql
CREATE TABLE IF NOT EXISTS bot_user (
    bot_user_id     TEXT PRIMARY KEY,           -- ULID/UUID
    provider        TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    is_bot          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (provider, provider_user_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_user_last_seen ON bot_user(last_seen_at);

CREATE TABLE IF NOT EXISTS channel (
    provider              TEXT NOT NULL,
    provider_channel_id   TEXT NOT NULL,
    name                  TEXT,
    scope                 TEXT NOT NULL,        -- DM | GROUP | THREAD | BROADCAST
    parent_channel_id     TEXT,                 -- para threads
    PRIMARY KEY (provider, provider_channel_id)
);

CREATE TABLE IF NOT EXISTS message (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    provider              TEXT NOT NULL,
    provider_channel_id   TEXT NOT NULL,
    provider_message_id   TEXT NOT NULL,
    direction             TEXT NOT NULL,        -- inbound | outbound
    bot_user_id           TEXT NOT NULL REFERENCES bot_user(bot_user_id),
    text                  TEXT NOT NULL,
    reply_to_message_id   TEXT,
    sent_at               TEXT NOT NULL,
    persisted_at          TEXT NOT NULL,
    raw_json              TEXT,                 -- payload original (compactado se grande)
    UNIQUE (provider, provider_channel_id, provider_message_id, direction)
);
CREATE INDEX IF NOT EXISTS idx_message_channel_time
    ON message(provider, provider_channel_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_user_time
    ON message(bot_user_id, sent_at DESC);

CREATE TABLE IF NOT EXISTS attachment (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id            INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    kind                  TEXT NOT NULL,
    url                   TEXT,
    mime                  TEXT,
    filename              TEXT,
    size_bytes            INTEGER,
    bytes_inline_b64      TEXT                  -- só para anexos pequenos (< 256KB)
);

CREATE TABLE IF NOT EXISTS dlq (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    provider              TEXT NOT NULL,
    payload_json          TEXT NOT NULL,
    last_error            TEXT NOT NULL,
    attempts              INTEGER NOT NULL,
    enqueued_at           TEXT NOT NULL,
    next_retry_at         TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type            TEXT NOT NULL,         -- inbound_received | should_respond_decided | …
    bot_user_id           TEXT,
    provider              TEXT,
    provider_channel_id   TEXT,
    provider_message_id   TEXT,
    payload_json          TEXT,
    occurred_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_occurred ON audit(occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit(bot_user_id, occurred_at);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
```

API pública:

```python
class ConversationStore:
    def __init__(self, db_path: Path): ...
    async def init(self) -> None: ...                        # roda migrations
    async def close(self) -> None: ...

    async def upsert_user(self, user: BotUser) -> None: ...
    async def upsert_channel(self, channel: Channel) -> None: ...

    async def record_inbound(self, env: MessageEnvelope) -> int: ...   # retorna id local
    async def record_outbound(
        self,
        provider: str, channel: Channel,
        provider_message_id: str, bot_user_id: str,
        text: str, reply_to: Optional[str], sent_at: datetime,
    ) -> int: ...

    async def get_recent_messages(
        self, provider: str, channel: Channel, limit: int = 40,
    ) -> list[StoredMessage]: ...

    async def was_outbound_sent_for(
        self, provider: str, channel: Channel, in_reply_to_message_id: str,
    ) -> bool: ...

    async def purge_older_than(self, days: int) -> int: ...
```

Detalhes obrigatórios:

- **WAL mode** habilitado no init: `PRAGMA journal_mode=WAL;` + `PRAGMA synchronous=NORMAL;`.
- **Foreign keys** habilitadas: `PRAGMA foreign_keys=ON;`.
- **Lock**: usar `aiosqlite.Connection` único por `ConversationStore`; transações curtas; nunca segurar lock cross-await.
- **Anexos > 256 KB** não são serializados inline — apenas `url` é guardado.
- **`raw_json`**: em chunks > 4 KB, gzip antes (campo é texto, então base64+gzip). Threshold em `BotSettings`.
- Migrations: loader busca `V*.sql` em ordem lexicográfica e aplica as ainda não registradas em `schema_version`.

### 2.2. `foundation/identity.py` — `IdentityResolver`

```python
class IdentityResolver:
    def __init__(self, store: ConversationStore): ...

    async def resolve(self, provider: str, provider_user_id: str, display_name: str, is_bot: bool) -> BotUser:
        """Encontra ou cria o BotUser no store. Atualiza last_seen_at e display_name (se mudou)."""

    async def by_bot_user_id(self, bot_user_id: str) -> Optional[BotUser]: ...
    async def search_by_display_name(self, q: str) -> list[BotUser]: ...    # informativo, não autorizativo
```

`bot_user_id` é gerado como ULID ao primeiro encontro; estável dali em diante. **Nunca** derivado de display_name.

### 2.3. `foundation/permissions.py` — `PermissionGate`

```python
class Action(str, Enum):
    READ_MESSAGE = "read_message"
    INVOKE_AGENT = "invoke_agent"
    SEND_DM = "send_dm"
    EXECUTE_TOOL = "execute_tool"        # tools que mudam estado
    ADMIN_COMMAND = "admin_command"
    DEBUG_COMMAND = "debug_command"

class PermissionDecision(NamedTuple):
    allowed: bool
    reason: str

class PermissionGate:
    def __init__(self, settings: BotSettings, identity: IdentityResolver): ...

    async def check(self, user: BotUser, action: Action, *, scope: ChannelScope, context: Mapping = {}) -> PermissionDecision: ...

    async def is_owner(self, user: BotUser) -> bool: ...
```

Settings:

```yaml
permissions:
  owners:                            # bot_user_ids
    - "01HZ..."
  allowlist_invoke_agent:            # bot_user_ids ou wildcard "*"
    - "*"                            # default = todos podem falar com o agente em qualquer DM/canal onde o bot está
  blocklist:
    - "01HX..."                      # bloqueia totalmente
  per_action:
    EXECUTE_TOOL:
      mode: owner_only               # | allowlist | wildcard
    ADMIN_COMMAND:
      mode: owner_only
    DEBUG_COMMAND:
      mode: owner_only
    SEND_DM:
      mode: allowlist
      list: ["01HZ..."]
```

Decisão: `blocklist > owners > per_action > allowlist_invoke_agent`.

### 2.4. `foundation/rate_limit.py` — `RateLimiter`

```python
class TokenBucket: ...                     # asyncio-friendly

class RateLimiter:
    def __init__(self, settings: BotSettings): ...

    async def acquire_inbound(self, user: BotUser) -> None:
        """Bloqueia ou raises RateLimited(reason='user_burst' | 'global_concurrent')."""

    async def acquire_outbound(self, user: BotUser, channel: Channel) -> None: ...

    def stats(self) -> dict: ...           # para métricas
```

Defaults: `user_burst=5`, `user_refill=30/min`, `global_concurrent=16`. Tudo configurável.

Métrica obrigatória: contador de `rate_limited{action,reason}`.

### 2.5. `foundation/audit.py` — Wrapper sobre `deile.security.audit_logger`

```python
class BotAuditLogger:
    def __init__(self, store: ConversationStore, deile_audit: DeileAuditLogger): ...

    async def log(
        self,
        event_type: AuditEventType,
        *,
        user: Optional[BotUser] = None,
        channel: Optional[Channel] = None,
        message_id: Optional[str] = None,
        payload: Mapping[str, Any] = {},
    ) -> None:
        """Persiste no SQLite + emite no deile.audit_logger para canal unificado."""
```

`AuditEventType` enum com pelo menos: `inbound_received`, `should_respond_decided`, `agent_invoked`, `agent_responded`, `agent_failed`, `outbound_sent`, `outbound_failed`, `permission_denied`, `rate_limited`, `dlq_enqueued`, `dlq_replayed`.

### 2.6. `foundation/intent.py` — `IntentClassifier`

```python
class IntentDecision(NamedTuple):
    should_respond: bool
    reason: str

class IntentClassifier(Protocol):
    async def decide(self, env: MessageEnvelope, history: list[StoredMessage], self_user_id: str) -> IntentDecision: ...

class HeuristicIntentClassifier(IntentClassifier):
    """
    Regras (em ordem):
    1. DM → sempre responde
    2. Mention ao bot → responde
    3. Reply a msg do bot → responde
    4. Mensagem < min_chars (default 4) → não responde
    5. Mensagem começa com prefixo de comando configurado → não responde (vai virar comando)
    6. Caso contrário → não responde
    """

class LLMIntentClassifier(IntentClassifier):
    """Mantém compat com o classificador atual: pergunta 'RESPONDER' ou ''."""
    def __init__(self, model: str, max_tokens: int = 10, temperature: float = 0.3): ...

class AlwaysRespondToAddressed(IntentClassifier):
    """DM, mention, reply → sim. Resto → não. Sem LLM."""

class AlwaysRespond(IntentClassifier):
    """Tudo é responder. Útil para canais 1:1 dedicados ao bot."""

def build_intent_classifier(settings: FoundationSettings) -> IntentClassifier:
    """Factory que olha settings.intent_classifier."""
```

### 2.7. Testes desta fase

Cobertura mínima:

| Módulo | Casos |
|---|---|
| `conversation_store` | Init roda V001; upsert idempotente; record_inbound respeita unique; get_recent_messages ordena por sent_at desc; purge_older_than remove e devolve count; concorrência (10 inserts paralelos no mesmo canal não duplicam nem deadlock) |
| `identity` | resolve cria na primeira vez; resolve devolve mesmo bot_user_id na segunda; mudança de display_name atualiza last_seen mas mantém id; bot=True não é confundido com humano |
| `permissions` | owner sempre permitido; blocklist bloqueia owner também; allowlist wildcard vs explícita; per_action override |
| `rate_limit` | burst respeitado; refill funciona; global concurrent enfileira; rate_limited com reason |
| `audit` | log persiste no SQLite + emite no event_bus mockado |
| `intent` | cada classifier nas 4 modalidades retorna o decidido para entrada padrão; build_intent_classifier devolve a classe certa por config |

Total de testes esperado: ~40.

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | `pytest deile_bot/tests/foundation/ -v` passa, sem failures/skips |
| AC-2 | Coverage da fase ≥ 85% |
| AC-3 | Migrations re-rodam sem erro (idempotência) |
| AC-4 | Teste de carga simples: 1000 inbound em 10 canais paralelos sem corrupção (`pytest -m slow`) |
| AC-5 | Audit log SQL inspecionável: `sqlite3 deile_bot.sqlite "SELECT event_type, count(*) FROM audit GROUP BY event_type"` |
| AC-6 | `RateLimited` exception expõe `.context["reason"]` |
| AC-7 | `python3 deile.py "olá"` continua funcionando (regressão zero) |

## Pontos de atenção

- **Lock por canal** é mais simples e suficiente (uma única conexão SQLite com WAL faz a serialização). Não criar Mutex application-level desnecessário.
- **`raw_json` cresce rápido** — instrumentar tamanho médio nos testes de carga; se >2KB médio, ativar gzip por default.
- **Backups**: documentar em `09-CONFIGURACAO.md` (system_design) que `deile_bot.sqlite` precisa entrar no esquema de backup do operador.
- **Privacidade**: `text` armazena mensagens reais — adicionar comando admin `/forget --user <id> --before <date>` em fase posterior do plano discord. Documentar aqui que o método `purge_older_than` é parte do TTL automático.

## Estimativa de esforço

3 dias de dev sênior. Maior parte é o `ConversationStore` + testes de concorrência.
