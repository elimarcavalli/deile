# Fase 1 — Pacote base e modelo de domínio

> Esqueleto do pacote `deile_bot/`, DTOs imutáveis, `MarkupAST`, settings, exceptions, ABC `ProviderAdapter`. **Nada que faça I/O ou persista nada.** Esta fase termina sendo importável e testável apenas com fakes de uma linha.

## Pré-requisitos

- Pasta `docs/future/deile-bot/foundation/` lida (00-PLAN, este arquivo).
- `python3 deile.py` ainda funcionando (não deve regredir).
- Branch própria: `feat/bot-foundation-fase-1`.

## Entregáveis

### 1.1. Layout do pacote

```
deile_bot/
├── __init__.py                      # __version__ = "0.1.0"
├── foundation/
│   ├── __init__.py
│   ├── envelope.py                  # MessageEnvelope, BotUser, Channel, Attachment, ReplyContext,
│   │                                # OutboundEnvelope, OutboundIntent, TemplateMessage, ConversationWindow
│   ├── interactive.py               # InteractiveControls (ABC) + InteractiveButton/Row/List/Section/QuickReply/QuickReplies
│   ├── capabilities.py              # ProviderCapabilities (ainda não o catalog)
│   ├── settings.py                  # BotSettings, FoundationSettings, ProviderRegistrySettings
│   ├── exceptions.py
│   └── _testing.py                  # fakes para outros pacotes consumirem em testes
├── providers/
│   ├── __init__.py
│   └── base.py                      # ProviderAdapter ABC
└── tests/
    ├── __init__.py
    └── foundation/
        ├── __init__.py
        ├── test_envelope.py         # inbound + outbound + window
        ├── test_interactive.py
        ├── test_settings.py
        ├── test_exceptions.py
        └── test_provider_adapter_abc.py
```

> **Nota sobre `MarkupAST`**: o tipo canônico vive em `deile/common/markup_ast.py` (criado pela fase 3 do plano DEILE). A foundation **importa** dali. Como a fase 3 do DEILE pode ainda não estar mergeada quando esta fase começar, a fase 1 da foundation pode criar `deile/common/markup_ast.py` como skeleton mínimo (apenas DTOs); a fase 3 do DEILE então adiciona o `MarkdownToASTParser` e o pipeline streaming. Decisão única, sem duplicação.

### 1.2. `foundation/envelope.py` — DTOs imutáveis (inbound + outbound + janela)

Classes (lista canônica de campos no `00-PLAN.md` §4):

**Inbound:**
- `enum ChannelScope`: `DM`, `GROUP`, `THREAD`, `BROADCAST`.
- `enum AttachmentKind`: `IMAGE`, `VIDEO`, `AUDIO`, `FILE`, `STICKER`, `OTHER`.
- `@dataclass(frozen=True, slots=True) BotUser`.
- `@dataclass(frozen=True, slots=True) Channel` (com `parent_channel_id` opcional para THREAD).
- `@dataclass(frozen=True, slots=True) Attachment`.
- `@dataclass(frozen=True, slots=True) ReplyContext`.
- `@dataclass(frozen=True, slots=True) MessageEnvelope`.

**Outbound:**
- `enum OutboundIntent`: `FREE_TEXT`, `TEMPLATE`.
- `@dataclass(frozen=True, slots=True) TemplateMessage`.
- `@dataclass(frozen=True, slots=True) ConversationWindow` com property `is_open`.
- `@dataclass(frozen=True, slots=True) OutboundEnvelope`.

Regras:

- Todos `frozen=True` para impedir mutação acidental entre tasks.
- `slots=True` para footprint baixo.
- `raw: Mapping[str, Any]` em `MessageEnvelope` é o payload original do provider, somente-leitura — útil para debug e para tools que precisem de campos raros. Tipo concreto: `MappingProxyType` ou `frozendict`.
- Métodos auxiliares (sem I/O): `MessageEnvelope.has_attachments`, `MessageEnvelope.is_dm`, `MessageEnvelope.mentions_self(self_user_id)`, `MessageEnvelope.has_force_respond` (lê `raw.get("force_respond")` — contrato oficial documentado em `00-MASTER-EXECUTION-PLAN.md` §2.7), `Attachment.is_inline`, `OutboundEnvelope.requires_template_window` (`intent == TEMPLATE`).
- Validação em `__post_init__`: `message_id` não vazio, `sent_at.tzinfo is not None` (sempre UTC-aware), `provider` em set extensível.
- `OutboundEnvelope.__post_init__`: se `intent == FREE_TEXT`, `text` é obrigatório; se `intent == TEMPLATE`, `template` é obrigatório.

### 1.2.b. `foundation/interactive.py` — Controles interativos

- `class InteractiveControls(ABC)`: marcador.
- `@dataclass(frozen=True, slots=True) InteractiveButton` (label + `callback_data` ou `url`).
- `@dataclass(frozen=True, slots=True) InteractiveButtonRow(InteractiveControls)`.
- `@dataclass(frozen=True, slots=True) InteractiveListSection`.
- `@dataclass(frozen=True, slots=True) InteractiveList(InteractiveControls)`.
- `@dataclass(frozen=True, slots=True) QuickReply`.
- `@dataclass(frozen=True, slots=True) QuickReplies(InteractiveControls)`.

Validações:
- `InteractiveButtonRow.buttons`: max 5 (Discord components), reduzido por adapter conforme provider.
- `QuickReplies.options`: max 13.
- `InteractiveListSection.items`: max 10 por seção (limite WhatsApp).
- Renderização concreta vive em `OutputFormatter` de cada provider — foundation nunca renderiza.

### 1.3. `deile/common/markup_ast.py` — Representação intermediária de texto formatado

> **Reside em `deile/common/`, não em `deile_bot/foundation/`.** Decisão final do `00-MASTER-EXECUTION-PLAN.md` §2.1: tipo único compartilhado por DEILE (CLI/streaming) e foundation (renderização por provider). Esta fase **cria** o módulo com DTOs mínimos; parsers e `MarkdownToASTParser` são entregues pela fase 3 do plano DEILE.

```python
# deile/common/markup_ast.py
from enum import Enum
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

class SpanKind(str, Enum):
    PLAIN = "plain"
    BOLD = "bold"
    ITALIC = "italic"
    STRIKE = "strike"
    CODE_INLINE = "code_inline"
    CODE_BLOCK = "code_block"        # meta: {"language": "py"}
    QUOTE = "quote"
    LINK = "link"                     # meta: {"url": "..."}
    HEADING = "heading"               # meta: {"level": 1..3}
    BULLET = "bullet"
    NUMBERED = "numbered"
    LINE_BREAK = "linebreak"

@dataclass(frozen=True, slots=True)
class MarkupSpan:
    kind: SpanKind
    text: str
    meta: Mapping[str, Any] = MappingProxyType({})

class MarkupAST(tuple[MarkupSpan, ...]):
    """Lista plana de spans. Ordem importa. Sem aninhamento."""
    @classmethod
    def from_plain(cls, text: str) -> "MarkupAST": ...
```

Imports do lado da foundation:

```python
from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
```

Parsers de entrada (`parse_discord_markdown`, `parse_telegram_markdown_v2`, `parse_whatsapp_text`, `parse_plain`) entram no plano DEILE Fase 3 ou no respectivo plano de provider — **não** nesta fase da foundation.

A renderização (`MarkupAST → str do provider`) vive em `OutputFormatter` (foundation fase 3, ABC) e nas subclasses `providers/<x>/formatter.py`.

### 1.4. `foundation/settings.py` — `BotSettings` singleton

```python
from pydantic import BaseSettings
from pathlib import Path

class FoundationSettings(BaseSettings):
    sqlite_path: Path = Path("./data/deile_bot.sqlite")
    data_retention_days: int = 90
    default_persona: str = "developer"
    intent_classifier: Literal["heuristic", "llm", "always_respond_to_addressed", "always_respond"] = "heuristic"
    rate_limit_user_burst: int = 5
    rate_limit_user_refill_per_minute: int = 30
    rate_limit_global_concurrent: int = 16
    agent_bridge_mode: Literal["in_process", "oneshot_subprocess"] = "in_process"
    agent_invocation_timeout_seconds: int = 120
    forced_model: Optional[str] = None    # ex.: "deepseek:deepseek-chat"
    audit_log_to_file: bool = True
    metrics_enabled: bool = True

    class Config:
        env_prefix = "DEILE_BOT_"
        env_file = ".env"
```

```python
class ProviderRegistrySettings(BaseSettings):
    """Habilitação de providers + mapeamento para classes."""
    enabled_providers: list[str] = []  # ['discord'] / ['discord', 'telegram']

    class Config:
        env_prefix = "DEILE_BOT_PROVIDERS_"
```

```python
class BotSettings:
    foundation: FoundationSettings
    providers: ProviderRegistrySettings
    # Mais campos conforme cada adapter chega (DiscordSettings, TelegramSettings, …)
    # — esses ficam em arquivos separados nos seus respectivos pacotes.

@lru_cache(maxsize=1)
def get_bot_settings() -> BotSettings: ...
```

Carga: prioridade `env > .env > YAML em ./config/deile_bot.yaml > defaults`.

### 1.5. `foundation/exceptions.py`

```python
class BotFoundationError(Exception): ...
class IdentityError(BotFoundationError): ...
class PermissionDenied(BotFoundationError): ...
class RateLimited(BotFoundationError): ...
class ConversationStoreError(BotFoundationError): ...
class AgentInvocationError(BotFoundationError): ...
class AgentInvocationTimeout(AgentInvocationError): ...
class FormatterError(BotFoundationError): ...
class CapabilityNotSupported(BotFoundationError): ...
class ProviderError(BotFoundationError): ...
class DLQError(BotFoundationError): ...
```

Cada uma com `__init__` aceitando `message: str` + `context: dict | None = None` para anexar dados estruturados.

### 1.6. `foundation/capabilities.py` — Capability flags do provider

```python
@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    can_edit_message: bool
    can_react: bool
    can_send_dm: bool
    can_threads: bool
    can_polls: bool
    can_inline_keyboards: bool
    can_slash_commands: bool
    can_voice_messages: bool
    can_send_typing: bool
    can_fetch_user_profile: bool
    has_conversation_window: bool          # WhatsApp = True (24h)
    max_message_chars: int
    max_attachments_per_message: int
    supported_attachment_kinds: frozenset[AttachmentKind]
```

### 1.7. `providers/base.py` — `ProviderAdapter` ABC

```python
from abc import ABC, abstractmethod

class ProviderAdapter(ABC):
    name: str                             # 'discord' | 'telegram' | ...
    capabilities: ProviderCapabilities

    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...

    # Outbound — providers implementam o que conseguem; default levanta CapabilityNotSupported.
    @abstractmethod
    async def send_message(
        self,
        channel: Channel,
        text: str,
        reply_to: Optional[str] = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:                              # retorna message_id criado
        ...

    async def edit_message(self, channel: Channel, message_id: str, new_text: str) -> None:
        if not self.capabilities.can_edit_message:
            raise CapabilityNotSupported(f"{self.name} cannot edit messages")
        raise NotImplementedError

    async def react(self, channel: Channel, message_id: str, emoji: str) -> None:
        if not self.capabilities.can_react:
            raise CapabilityNotSupported(f"{self.name} cannot react")
        raise NotImplementedError

    async def send_dm(self, user: BotUser, text: str, attachments: Sequence[Attachment] = ()) -> str:
        if not self.capabilities.can_send_dm:
            raise CapabilityNotSupported(f"{self.name} cannot send DM")
        raise NotImplementedError

    async def fetch_user_profile(self, user: BotUser) -> Mapping[str, Any]:
        if not self.capabilities.can_fetch_user_profile:
            raise CapabilityNotSupported(f"{self.name} cannot fetch profile")
        raise NotImplementedError

    async def send_typing(self, channel: Channel) -> None:
        if not self.capabilities.can_send_typing:
            return  # silencioso, não é erro

    # Inbound — providers chamam isto quando recebem evento.
    # Definido como callback que é setado pela foundation no start.
    on_inbound: Callable[[MessageEnvelope], Awaitable[None]]
```

### 1.8. `foundation/_testing.py` — Fakes públicos

- `class FakeProviderAdapter(ProviderAdapter)`: implementa tudo em memória, expõe `inbox` (mensagens enviadas) e `inject(envelope)` para simular entrada.
- `def make_envelope(...)`: factory para testes consumirem rapidamente.
- `def make_user(...)`, `def make_channel(...)`.

### 1.9. Testes desta fase

| Arquivo | Cobertura |
|---|---|
| `test_envelope.py` | Imutabilidade, validações `__post_init__` (inbound + outbound), métodos `is_dm`/`mentions_self`/`has_attachments`/`has_force_respond`/`requires_template_window`; `OutboundEnvelope(intent=FREE_TEXT, text=None)` levanta. |
| `test_interactive.py` | Limites de tamanho (`InteractiveButtonRow` >5 levanta; `InteractiveListSection.items` >10 levanta; `QuickReplies.options` >13 levanta). |
| `test_settings.py` | Override por env var (`DEILE_BOT_INTENT_CLASSIFIER=llm` deve refletir); singleton `get_bot_settings()` retorna mesma instância. |
| `test_exceptions.py` | Hierarquia, atributo `context`. |
| `test_provider_adapter_abc.py` | `FakeProviderAdapter().send_message(...)` aparece em `inbox`; chamar `edit_message` em adapter sem `can_edit_message` levanta `CapabilityNotSupported`. |
| `test_markup_ast_skeleton.py` | DTOs criados em `deile/common/markup_ast.py` instanciam corretamente (mesmo sem parsers); `MarkupAST.from_plain` produz único span `PLAIN`. |

### 1.10. Configuração e CI

- Adicionar `aiosqlite`, `pydantic>=2`, `tenacity` em `requirements.txt` da raiz (foundation usa-os; CI tem que instalar).
- Adicionar pasta `deile_bot/` ao `pytest.ini` (`testpaths = deile/tests/ deile_bot/tests/`).
- Adicionar `deile_bot/` ao `ruff` e `isort` na configuração existente.

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | `pytest deile_bot/tests/foundation/ -v` passa sem failures e sem skips |
| AC-2 | Coverage da pasta `deile_bot/foundation/` ≥ 90% (configurável no `pytest.ini`) |
| AC-3 | `python3 -c "from deile_bot.foundation.envelope import MessageEnvelope; print(MessageEnvelope.__mro__)"` funciona |
| AC-4 | `ruff check deile_bot/` passa sem warnings |
| AC-5 | `python3 deile.py "olá"` continua respondendo (regressão zero do CLI) |
| AC-6 | Nenhum import de `deile_bot.providers` em `deile_bot/foundation/` (verificado por teste de imports) |
| AC-7 | `BotSettings` lido com 3 fontes (env, .env, defaults) testado |

## Pontos de atenção

- **Não** criar `ConversationStore`, `AgentBridge`, nada de I/O nesta fase. Se você está mexendo em SQLite, parou na fase errada.
- **Não** adicionar `discord.py`, `python-telegram-bot` ou qualquer SDK de provider em `requirements.txt`. Cada provider declara as suas no respectivo plano.
- **Validar UTC**: `datetime.now()` é proibido na foundation; sempre `datetime.now(timezone.utc)`. Adicionar regra de lint custom se houver tempo.
- **`raw: Mapping`** deve ser `MappingProxyType` ou `frozendict` para garantir imutabilidade.

## Estimativa de esforço

1.5–2 dias de dev sênior. Maior parte é design e testes; o código é pequeno.
