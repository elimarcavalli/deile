# Fase 2 — `extra_system_prompt` e `bot_context`

> Permitir que o bot injete um bloco extra no system prompt **por invocation** e passe contexto de provider/canal disponível para tools.

## Pré-requisitos

- Fase 1 mergeada.
- Branch: `feat/deile-extra-system-prompt`.

## Entregáveis

### 2.1. `process_input` estendido

```python
async def process_input(
    self,
    user_input: str,
    session_id: str = "default",
    *,
    extra_system_prompt: Optional[str] = None,    # NOVO
    bot_context: Optional[Mapping[str, Any]] = None,  # NOVO
) -> AgentResponse:
    ...
```

Behavior:

- `extra_system_prompt`: concatenado ao final do system prompt da persona resolvida, com separador claro:

  ```
  <persona_system_prompt>

  ---
  <bot_capabilities>
  {extra_system_prompt}
  </bot_capabilities>
  ```

  Se `None`, comportamento atual (sem mudança).
- `bot_context`: armazenado em `session.context_data["bot_context"]` (sobrescreve o anterior se diferente). Tools que precisam, leem via `ToolContext.extra["bot_context"]`.

### 2.2. `ToolContext.extra`

Se `ToolContext` (em `deile/tools/base.py`) ainda não tem campo `extra: Mapping[str, Any]`, adicionar:

```python
@dataclass
class ToolContext:
    ...
    extra: Mapping[str, Any] = MappingProxyType({})
```

`agent` popula `extra={"bot_context": session.context_data.get("bot_context", {})}` ao despachar uma tool, sempre.

### 2.3. Sanitização de `extra_system_prompt`

A foundation **deve** sanitizar antes de mandar — mas como defesa em profundidade, o agente também:

```python
def _sanitize_extra_prompt(s: str) -> str:
    # Remove tags que abusam estrutura: </system>, <persona_override>, etc.
    # Lista bloqueada documentada em comment.
    ...
```

### 2.4. Testes

- `extra_system_prompt="<bot_capabilities>tool_x: ...</bot_capabilities>"` aparece no prompt enviado ao provider (mock do `ModelRouter`).
- `bot_context={"provider":"discord","channel_scope":"DM"}` é repassado a uma tool de teste que lê `ctx.extra["bot_context"]`.
- `extra_system_prompt` com `</system>` é sanitizado (linha removida ou escapada).
- CLI sem `extra_system_prompt` produz prompt idêntico ao atual (golden test).

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | `pytest deile/tests/core/test_extra_system_prompt.py` passa |
| AC-2 | Golden test do system prompt da CLI sem mudança |
| AC-3 | Tool de teste recebe bot_context |
| AC-4 | Tags adversariais sanitizadas |

## Estimativa

1 dia.
