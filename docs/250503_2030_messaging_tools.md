# Messaging Tools — Mensageria Proativa via deile-bot Daemon

## 1. Overview

Adiciona à DEILE uma família de **tools de mensageria** (`messaging.discord_*`) que permite ao agente, durante uma sessão, enviar mensagens, DMs, reactions, threads e pins por meio de um daemon `deile-bot` rodando em paralelo (repo separado: `elimarcavalli/deile-bot`).

Hoje o fluxo é unidirecional (`bot → agent`: o usuário fala com o bot, o bot consulta a agente). Esta feature inverte a flecha (`agent → bot`): a agente decide proativamente falar em canais quando o usuário pede ("DEILE, avisa no #ops que o deploy terminou", "manda DM pro Tiago…").

**Significado arquitetural**: primeira hexagonal-adapter da DEILE para um serviço externo persistente (não LLM); estabelece o padrão `deile/integrations/<service>/` que outras integrações poderão seguir.

## 2. Architectural Decisions

| Decisão | Por quê |
|---|---|
| **HTTP control-plane** (não in-process, não outbox) | Daemons de chat têm ciclo de vida diferente da CLI; in-process forçaria subir o bot toda vez que a CLI abrir. SQLite outbox introduziria latência e perderia feedback síncrono (msg_id, falhas). HTTP em `127.0.0.1` é simples, isola repos, mantém ack imediato. |
| **Bind em localhost + Bearer token** | Não expor ao mundo. Token gerado em setup, persistido em `.env` de ambos os lados. |
| **Cliente fino separado das deps do daemon** | DEILE puxa só `httpx` + `pydantic` (já presentes). A pesada `discord.py` fica no daemon. Quem usa a CLI sem bot não paga o custo. |
| **Auto-discovery condicional** das tools | Se `deile-bot-client` não está instalado **ou** `bot.endpoint` não está configurado, as tools simplesmente não registram. Princípio 10 (Extensibilidade). |
| **Cada tool passa por `PermissionManager` + `AuditLogger`** | Princípios 5 e 11. DM e role-mention são `SecurityLevel.DANGEROUS` → exigem aprovação via `ApprovalSystem`. Channel-post e react são `MODERATE`. |
| **Tools são assíncronas** (`Tool`, não `SyncTool`) | I/O de rede. Princípio 1. |
| **Erros do daemon viram `ToolResult.error_result(code=...)` tipados** | Princípio 6. Sem `bare except`, sem deixar exceção escapar. |
| **aiohttp.web no daemon** (não FastAPI) | Sem nova dep — discord.py já traz aiohttp. |
| **Singleton `BotClientFacade`** | Reuso de connection pool httpx; teste injeta via `set_underlying`. |

Detalhe completo em [`DECISOES.md` #17](system_design/DECISOES.md).

## 3. Component Architecture

```
+---------------------------------+        +---------------------------------+
|  deile (este repo)              |        |  deile-bot (repo separado)      |
|                                 |        |                                 |
|  deile/tools/messaging/         |        |  deile_bot/runtime/             |
|    ├ _base.py (MessagingTool)   |        |    control_plane/               |
|    ├ discord_send_message.py    |        |    ├ server.py (aiohttp.web)    |
|    ├ discord_send_dm.py         |  HTTP  |    ├ routes.py                  |
|    ├ discord_react.py           |  POST  |    ├ auth.py (Bearer)           |
|    ├ discord_start_thread.py    | -----> |    ├ errors.py                  |
|    ├ discord_pin_message.py     | Bearer |    └ settings.py                |
|    ├ discord_mention_role.py    | token  |                                 |
|    └ discord_get_user_profile.py|        |  deile_bot_client/              |
|                                 |        |    ├ client.py (httpx, tenacity)|
|  deile/integrations/bot/        |        |    ├ models.py (pydantic v2)    |
|    ├ client.py (BotClientFacade)|        |    └ errors.py                  |
|    ├ config.py (Settings)       |        |                                 |
|    └ __init__.py                |        |  deps daemon: aiohttp, discord  |
|                                 |        |  deps client: httpx, pydantic   |
|  deps: deile-bot-client (extra) |        |                                 |
+---------------------------------+        +---------------------------------+
```

## 4. Implementation Details

```
deile/
├── integrations/
│   ├── __init__.py
│   └── bot/
│       ├── __init__.py
│       ├── client.py          # BotClientFacade — singleton wrapper
│       └── config.py          # BotIntegrationSettings
└── tools/
    ├── base.py                # ToolCategory.MESSAGING added
    ├── registry.py            # auto_discover() calls register_messaging_tools()
    └── messaging/
        ├── __init__.py
        ├── _base.py           # MessagingTool — common pipeline
        ├── auto_discover.py   # conditional registration
        ├── discord_send_message.py
        ├── discord_send_dm.py
        ├── discord_react.py
        ├── discord_start_thread.py
        ├── discord_pin_message.py
        ├── discord_mention_role.py
        └── discord_get_user_profile.py

deile_bot/                     # nested working tree, separate .git, separate repo
├── pyproject.toml             # NEW — packageize the bot daemon + client
├── runtime/
│   └── control_plane/         # NEW
│       ├── __init__.py
│       ├── server.py          # aiohttp lifecycle
│       ├── routes.py          # 8 endpoints
│       ├── auth.py            # Bearer middleware
│       ├── errors.py          # canonical envelope
│       └── settings.py        # ControlPlaneSettings
├── deile_bot_client/          # NEW
│   ├── __init__.py
│   ├── client.py              # BotControlClient
│   ├── models.py              # Pydantic v2 (shared with server)
│   └── errors.py              # typed exceptions
└── tests/control_plane/
    └── test_endpoints.py
```

## 5. API Specification

### Control-plane HTTP (deile-bot side)

| Method | Path | Body model | Response model | Notes |
|---|---|---|---|---|
| GET  | `/v1/health` | – | `HealthResponse` | Public; no auth required |
| POST | `/v1/outbound/discord/channel.post` | `ChannelPostRequest` | `ChannelPostResponse` | Bearer required |
| POST | `/v1/outbound/discord/dm.send` | `DMSendRequest` | `DMSendResponse` | Bearer required |
| POST | `/v1/outbound/discord/reaction.add` | `ReactionAddRequest` | `ReactionAddResponse` | Bearer required |
| POST | `/v1/outbound/discord/thread.start` | `ThreadStartRequest` | `ThreadStartResponse` | Bearer required |
| POST | `/v1/outbound/discord/message.pin` | `MessagePinRequest` | `MessagePinResponse` | Bearer required |
| POST | `/v1/outbound/discord/role.mention` | `RoleMentionRequest` | `RoleMentionResponse` | Bearer required |
| GET  | `/v1/users/{user_id}` | – | `UserProfileResponse` | Bearer required |

Error envelope on every non-2xx: `{"error": {"code": "...", "message": "...", "details": {...}}}`.

Codes: `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `BAD_REQUEST`, `RATE_LIMITED`, `UPSTREAM_ERROR`, `NOT_READY`, `INTERNAL_ERROR`.

### DEILE-side tools

Each tool inherits `MessagingTool` and exposes the standard `Tool` interface (`name`, `description`, `category`, `execute(ToolContext) -> ToolResult`). The schema is auto-generated from the subclass's `parameters` / `required_params` / `security_level`.

| Tool | Required params | Security | Approval? |
|---|---|---|---|
| `discord_send_message` | `channel_id`, `text` | MODERATE | no |
| `discord_send_dm` | `text` (one of `user_id`/`bot_user_id`) | DANGEROUS | yes |
| `discord_react` | `channel_id`, `message_id`, `emoji` | MODERATE | no |
| `discord_start_thread` | `channel_id`, `name` | MODERATE | no |
| `discord_pin_message` | `channel_id`, `message_id` | MODERATE | no |
| `discord_mention_role` | `channel_id`, `role_id` | DANGEROUS | yes |
| `discord_get_user_profile` | `user_id` | SAFE | no |

## 6. Configuration Schema

| Env var | Default | Notes |
|---|---|---|
| `DEILE_BOT_ENDPOINT` | `""` | Daemon URL (e.g. `http://127.0.0.1:8765`) |
| `DEILE_BOT_AUTH_TOKEN` | `""` | Bearer token shared with the daemon |
| `DEILE_BOT_TIMEOUT_S` | `10.0` | HTTP timeout |
| `DEILE_BOT_DEFAULT_GUILD_ID` | – | Optional Discord guild hint |
| `DEILE_BOT_CONTROL_PLANE_HOST` | `127.0.0.1` | Daemon-side bind host |
| `DEILE_BOT_CONTROL_PLANE_PORT` | `8765` | Daemon-side bind port (0 = auto) |
| `DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN` | `""` | Daemon-side Bearer token (must match client side) |
| `DEILE_BOT_CONTROL_PLANE_RATE_LIMIT_PER_MINUTE` | `120` | Per-IP soft rate limit |

## 7. Security Implementation

- **Permission**: every messaging op calls `PermissionManager.check_permission(tool_name, resource, "execute")` where `resource = messaging:<tool>:<scope_id>`. False → `ToolResult.error_result(code="PERMISSION_DENIED")`.
- **Approval**: `discord_send_dm` and `discord_mention_role` go through `ApprovalSystem.request_approval(...)` with `risk_level="high"`. Recusa/timeout → `ToolResult.error_result(code="APPROVAL_REQUIRED")`.
- **Audit**: every invocation emits `AuditEvent(TOOL_EXECUTION)`. Raw text is **never** logged — only a SHA8 hash of the body.
- **Token redaction**: both `BotIntegrationSettings.__repr__` and `BotControlSettings.__repr__` mask `auth_token`. The secrets scanner has new patterns for `DEILE_BOT_AUTH_TOKEN`, `DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN`, and `DEILE_BOT_DISCORD_TOKEN`.
- **Auth time-safe**: control-plane uses `hmac.compare_digest` for the Bearer comparison.
- **Bind discipline**: control-plane defaults to `127.0.0.1`. Exposing publicly requires explicit env override.

## 8. Testing Strategy

| Suite | Location | Count | Notes |
|---|---|---|---|
| Client unit | `deile/tests/integrations/bot/test_client.py` | 14 | health/post/dm/react/thread/pin/mention/user; auth, timeout, 5xx retry, 429 retry-after, 503 NOT_READY, 401 UNAUTHORIZED, schema-rejected user_id, repr masking |
| Settings | `deile/tests/integrations/bot/test_config.py` | 7 | env loading, defaults, both-required, disabled override, repr |
| Tool per op | `deile/tests/tools/messaging/test_discord_*.py` | 7 files (~30 tests) | Per-op success, permission denied (where applicable), audit emission, approval gate (DM/mention) |
| Auto-discovery | `deile/tests/tools/messaging/test_auto_discover.py` | 4 | Missing client / unconfigured / full setup / idempotent |
| Schemas | `deile/tests/tools/messaging/test_schemas.py` | 22 | Anthropic / OpenAI / Gemini representations + category check |
| E2E real daemon | `deile/tests/tools/messaging/test_e2e_against_fake_daemon.py` | 1 (marker `integration`) | Boots `ControlPlaneServer` + uses real `BotControlClient` + real tool |
| Permissions | `deile/tests/security/test_messaging_permissions.py` | 3 | Approval-required default, resource string shape, denylist |
| Secrets scanner | `deile/tests/security/test_secrets_scanner_bot.py` | 4 | New token patterns |
| Control-plane | `deile_bot/tests/control_plane/test_endpoints.py` | 4 | Health public, protected requires token, round-trip |

Total: 85 new tests on the deile side + 4 on the deile-bot side. Coverage on new code: 90%.

## 9. Usage Examples

### Programmatic — DEILE side

```python
from deile.tools.messaging import DiscordSendMessageTool
from deile.tools.base import ToolContext

tool = DiscordSendMessageTool()
ctx = ToolContext(
    user_input="",
    parsed_args={"channel_id": "123456789", "text": "deploy 5.1.0 done"},
    session_data={},  # PermissionManager, AuditLogger picked up via singletons
)
result = await tool.execute(ctx)
print(result.is_success, result.data["message_id"])
```

### CLI

```
> avisa no #releases que o build 5.1.0 subiu

DEILE → tool messaging.discord_send_message(channel=releases, text="build 5.1.0 ...")
        ✓ enviado (msg_id=...)
```

### Daemon (deile-bot side)

```bash
# .env on daemon side
export DEILE_BOT_DISCORD_TOKEN="…"
export DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN="$(openssl rand -hex 24)"
deile-bot run --provider discord
```

## 10. Performance Characteristics

| Aspect | Detail |
|---|---|
| Latency | Loopback HTTP ≈ <1 ms client→daemon; Discord round-trip dominates (typically 50-200 ms) |
| Connection pooling | `httpx.AsyncClient` reuses connections; the facade keeps one client for the lifetime of the process |
| Concurrency | Each tool call is independent; the registry can dispatch in parallel (`asyncio.gather`) without contention |
| Rate limiting | Per-IP soft limit on the daemon (`DEILE_BOT_CONTROL_PLANE_RATE_LIMIT_PER_MINUTE`, default 120) returns 429+`Retry-After`; client honors one auto-retry then surfaces `BotClientRateLimited` |
| Retry policy | Tenacity exponential backoff (0.5s/1s/2s) on 5xx and timeouts; 3 attempts default. 4xx are not retried |

## 11. Monitoring & Observability

| Signal | Where |
|---|---|
| `AuditEvent(TOOL_EXECUTION)` | `deile/security/audit_logger.py` — emitted by `MessagingTool` on every call (success/denied/failed) |
| `AuditEvent(APPROVAL_GRANTED/DENIED)` | Same channel, on approval flow for DM / role-mention |
| Daemon-side audit | `BotAuditLogger` emits `OUTBOUND_SENT` / `OUTBOUND_FAILED` per request (when wired) |
| Server logs | `logger.info("control_plane outbound", extra={...})` per route |
| Client logs | Errors only; tokens never logged |
| Health | `GET /v1/health` returns `{ok, version, providers, is_ready}` for liveness checks |

## 12. Migration & Deployment

| Aspect | Detail |
|---|---|
| Breaking change | `pyproject.toml` removed extras `discord/telegram/whatsapp/meta/all-bots` and the `deile-bot` console-script. Users running `pip install deile[discord]` need to migrate to `pip install deile-bot[discord]` |
| Repo-split | `deile_bot/` left in this repo's working tree as a nested `.git` (untracked here, tracked in its own repo). Once both PRs merge, ops can either keep the nested layout or move it elsewhere |
| Rollout | (1) Merge PR `elimarcavalli/deile-bot#1`. (2) Publish `deile-bot-client` to PyPI (or use path install during dev). (3) Merge this PR. (4) Operator sets env vars and restarts both processes |
| Rollback | Revert this PR — tools simply stop registering. Daemon side is unaffected |
| Feature flag | Implicit: integration deactivates when env vars are absent. `BotIntegrationSettings.disabled=True` is an explicit kill switch |

## 13. Troubleshooting Guide

| Problem | Likely cause | Fix |
|---|---|---|
| Tool not appearing in `tool list` | Either `deile-bot-client` not installed or env vars unset | `pip install deile[bot]`; set `DEILE_BOT_ENDPOINT` and `DEILE_BOT_AUTH_TOKEN` |
| `BOT_AUTH_ERROR` (401) | Token mismatch between CLI and daemon | Same value in both `.env` files |
| `BOT_NOT_READY` (503) | Daemon booted but Discord adapter not connected yet | Wait a few seconds, retry; check `GET /v1/health` |
| `BOT_TIMEOUT` | Daemon hung or network blocked | Check daemon logs; increase `DEILE_BOT_TIMEOUT_S` |
| `BOT_RATE_LIMITED` | Hitting per-IP throttle | Reduce volume, or raise `DEILE_BOT_CONTROL_PLANE_RATE_LIMIT_PER_MINUTE` if you control the daemon |
| `APPROVAL_REQUIRED` for DM | Approval was denied or timed out | Re-issue and respond to the prompt within the timeout |
| Tools registered but daemon unreachable | DEILE was started before the daemon | Restart DEILE after the daemon is up; or call `reset_bot_client()` |
| `INTERNAL_ERROR` (500) on a route | Adapter raised unexpectedly | Daemon log will have the traceback; file an issue |

## 14. Future Considerations

- **Telegram** — same client, new endpoints (`/v1/outbound/telegram/...`). Adapter scaffolding already exists in the daemon.
- **Streaming long messages** — currently one tool call = one message; could chunk and stream.
- **Embeds / interactive components** — Discord embeds, buttons, dropdowns.
- **Outbox-on-failure** — persist failed outbound calls and replay when the daemon comes back up. Currently failures surface synchronously and the agent decides what to do.
- **Idempotency keys** — protect against retry-induced duplicates if upstream returns a transient error after committing.
- **Slash command shortcuts** — `/notify`, `/dm` as DEILE slash commands (vs LLM-decided tool calls).
