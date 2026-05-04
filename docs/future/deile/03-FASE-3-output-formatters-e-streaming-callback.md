# Fase 3 — Output AST + Streaming chunk-a-chunk

> Disponibilizar saída como `MarkupAST` e streaming `AsyncIterator[StreamChunk]` para que o adapter possa renderizar por provider e atualizar a UI progressivamente.

## Pré-requisitos

- Fases 1 e 2 mergeadas.
- Branch `feature/streaming-ui` mergeada na main (ou rebaseada com este plano).
- Branch desta fase: `feat/deile-output-ast-stream`.

## Entregáveis

### 3.1. `deile/ui/markup.py` — Parser markdown → AST

```python
class MarkdownToASTParser:
    def parse(self, text: str) -> MarkupAST: ...
```

`MarkupAST` mora em **`deile/common/markup_ast.py`** (decisão final do `00-MASTER-EXECUTION-PLAN.md` §2.1). A foundation Fase 1 cria o skeleton (DTOs); esta fase 3 do plano DEILE adiciona o `MarkdownToASTParser` (parser de saída do agente) e os parsers de entrada por provider — `parse_discord_markdown`, `parse_telegram_markdown_v2`, `parse_whatsapp_text`. Sem duplicação. Sem `deilebot/foundation/markup_ast.py`.

Cobertura mínima: bold, italic, strike, code inline, code block (com language), quote, heading 1-3, link, bullet list, numbered list, plain.

### 3.2. `process_input_structured`

```python
@dataclass(frozen=True)
class StructuredResponse:
    text: str
    markup: MarkupAST
    tool_calls: list[ToolCallRecord]
    elapsed_ms: int
    model_used: str
    status: ResponseStatus

async def process_input_structured(
    self,
    user_input: str,
    session_id: str = "default",
    *,
    extra_system_prompt: Optional[str] = None,
    bot_context: Optional[Mapping[str, Any]] = None,
) -> StructuredResponse: ...
```

Implementação: chama `process_input` (sem reescrever lógica), parseia `response.content` com `MarkdownToASTParser`, retorna.

### 3.3. `process_input_stream`

```python
@dataclass(frozen=True)
class StreamChunk:
    kind: Literal["text", "markup_span", "tool_call_started", "tool_call_finished", "done", "error"]
    payload: Mapping[str, Any]                # depende de kind

async def process_input_stream(
    self,
    user_input: str,
    session_id: str = "default",
    *,
    extra_system_prompt: Optional[str] = None,
    bot_context: Optional[Mapping[str, Any]] = None,
) -> AsyncIterator[StreamChunk]: ...
```

Convenções:

- `text`: payload `{"text": "...", "incremental": True}`. Concatena com chunks anteriores.
- `markup_span`: payload é `MarkupSpan` serializado, para clientes que querem AST progressiva.
- `tool_call_started`: `{"tool_name": "...", "args_preview": "..."}`.
- `tool_call_finished`: `{"tool_name": "...", "ok": bool, "elapsed_ms": int}`.
- `done`: `{"text": "<full final text>", "markup": <ast>, "elapsed_ms": int, "model_used": "..."}`. Sempre o último chunk emitido.
- `error`: `{"type": "...", "message": "..."}`. Emitido em erro fatal; segue um `done` com texto vazio.

Implementação: reaproveitar pipeline streaming da `feature/streaming-ui`; expor o que já existe num iterator tipado.

### 3.4. Testes

- `process_input_structured` produz `markup` parseável; `text` igual a `process_input(...).content`.
- `process_input_stream` emite ≥ 2 chunks; último é sempre `done`; recombinação de `text` chunks bate com `done.text`.
- Teste de cancelamento: `async for chunk in stream: if chunk_n: break` — sem subprocess zumbi, sem deadlock.
- CLI continua funcionando — `process_input` (não-stream, não-structured) sem mudança.

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | `pytest deile/tests/core/test_structured_output.py` passa |
| AC-2 | `pytest deile/tests/core/test_stream.py` passa |
| AC-3 | Recombinação de chunks `text` é igual ao `done.text` em 100 execuções |
| AC-4 | Cancelamento limpo (sem warnings de tasks pendentes) |

## Estimativa

2 dias (depende de quão consolidada está a `feature/streaming-ui`).
