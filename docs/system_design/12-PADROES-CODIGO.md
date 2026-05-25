# 12 — Padrões de Código (Templates Concretos)

> Snippets canônicos para criar/editar artefatos. Princípios em [`03-PRINCIPIOS-ARQUITETURAIS.md`](03-PRINCIPIOS-ARQUITETURAIS.md). Modelo de componentes em [`04-MODELO-COMPONENTES.md`](04-MODELO-COMPONENTES.md).

## Snippet picker — vá direto à seção certa

| Você está criando/editando… | Use a seção |
|---|---|
| Novo arquivo em `deile/tools/**/*.py` | **Tool Development** |
| Novo arquivo em `deile/commands/**/*.py` | **Command Implementation** |
| Novo arquivo em `deile/parsers/**/*.py` | **Parser Development** |
| Novo `*.md` em `deile/skills/library/`, `~/.deile/skills/`, `.deile/skills/`, etc. | **Skill Development** |
| Estado cross-turn ou cross-session | **Memory System Integration** |
| Permission check / audit log / sanitização | **Security Implementation** |
| Padrões de intent / regex novos | **Intent Analysis Integration** |
| Nova exceção ou bloco `except` não trivial | **Error Handling** |
| Novo `test_*.py` ou marker pytest | **Testing Requirements** |
| Nova chave de configuração ou env var | **Configuration Management** |
| Operação repetida, longa ou pooled | **Performance Optimization** |
| Qualquer código emitindo logs | **Logging Standards** |
| Qualquer função com I/O | **Async-First** (sempre) + linha acima que casa |

> Se múltiplas linhas casarem, aplique cada uma. Se você está em `deile/` sem casar nada: **Async-First + Registry Pattern + snippet do análogo mais próximo**. Cada seção termina com a linha ❌ — leia antes de copiar.

---

## Async-First (sempre)

| Regra | Detalhe |
|---|---|
| Toda função com I/O | `async` |
| Await | Nunca esquecer |
| I/O bloqueante | Proibido em contexto async |
| Paralelo | `asyncio.gather()` |
| Cleanup | `async with` |

## Tool Development

```python
class CustomTool(Tool):
    @property
    def name(self) -> str:
        return "tool_name"

    @property
    def description(self) -> str:
        return "Clear description for the LLM"

    @property
    def category(self) -> str:
        return "category_name"  # ver ToolCategory

    def __init__(self):
        super().__init__(schema=ToolSchema(
            name="tool_name",
            description="Clear description for the LLM",
            parameters={
                "param": {"type": "string", "description": "Parameter description"},
            },
            required=["param"],
            security_level=SecurityLevel.MODERATE,
            category=ToolCategory.OTHER,
        ))

    async def execute(self, context: ToolContext) -> ToolResult:
        try:
            value = context.parsed_args.get("param")
            data = await self._perform_operation(value)
            return ToolResult.success_result(data=data, message="ok")
        except Exception as exc:
            return ToolResult.error_result(message=str(exc), error=exc)
```

❌ Nunca: validação manual em `execute()` (use `ToolSchema`); retornar dados crus; deixar exceção escapar de `execute()`; omitir `security_level`; instanciar a tool manualmente em vez de registrar; I/O síncrono dentro de `execute()`.

## Command Implementation

```python
class CustomCommand(SlashCommand):
    name = "command"
    description = "Command description"
    aliases = ["cmd", "c"]

    async def execute(self, context: CommandContext) -> CommandResult:
        if not self._validate_args(context.args):
            return CommandResult.error_result("Invalid arguments")

        try:
            result = await self._process(context)
            return CommandResult.success_result(
                content=result,
                content_type="rich",
            )
        except Exception as exc:
            logger.error("Command failed: %s", exc)
            return CommandResult.error_result(str(exc), error=exc)
```

❌ Nunca: `print` direto para stdout (use `content` + `content_type` no `CommandResult`); duplicar lógica de tool dentro do comando — invocar a tool pelo registry; pular `aliases` se há atalho natural; mutar global em vez de retornar via `CommandResult`.

## Parser Development

```python
class CustomParser(Parser):
    @property
    def name(self) -> str:
        return "custom_parser"

    @property
    def description(self) -> str:
        return "Parser description"

    @property
    def patterns(self) -> List[str]:
        return [r"^pattern$"]

    @property
    def priority(self) -> int:
        return 50  # default 0; maior executa antes

    def can_parse(self, input_text: str) -> bool:
        return self._matches_pattern(input_text)  # checagem rápida, síncrona

    def parse(self, input_text: str) -> ParseResult:
        try:
            commands = self._extract(input_text)
            return ParseResult(status=ParseStatus.SUCCESS, commands=commands)
        except Exception as exc:
            return ParseResult(status=ParseStatus.FAILED, error_message=str(exc))

    # Para parsers que precisam de I/O, sobrescreva parse_async em vez de parse:
    # async def parse_async(self, input_text: str) -> ParseResult: ...
```

❌ Nunca: trabalho pesado em `can_parse()` (ele roda para cada parser em cada input — mantenha checagem de padrão rápida); definir `priority` arbitrariamente sem comparar com vizinhos; retornar `ParseResult(status=ParseStatus.SUCCESS)` sem preencher `commands`/`file_references`/`tool_requests`; deixar exceção escapar de `parse()`.

## Memory System Integration

A entrada canônica é `MemoryManager` em `deile/memory/memory_manager.py`. Cada camada vive em seu módulo. Os nomes abaixo casam com a API pública atual; **sempre confira o módulo da camada antes de chamar** — assinaturas evoluem.

```python
# Caminho conveniente (cobre a maioria dos casos)
await memory_manager.store_interaction(...)

# Working Memory (deile/memory/working_memory.py)
await memory_manager.working_memory.store(...)
await memory_manager.working_memory.store_interaction(...)

# Episodic Memory (deile/memory/episodic_memory.py)
await memory_manager.episodic_memory.store_episode(...)

# Semantic Memory (deile/memory/semantic_memory.py)
await memory_manager.semantic_memory.store_knowledge(knowledge_dict)
await memory_manager.semantic_memory.store_correction(interaction_id, correction_data)

# Procedural Memory (deile/memory/procedural_memory.py)
patterns = await memory_manager.procedural_memory.get_relevant_patterns(query)
```

❌ Nunca: cachear estado cross-turn em globals de módulo ou atributos de classe — use a camada apropriada (working = TTL transitório, episodic = eventos da sessão, semantic = fatos/conhecimento, procedural = skills aprendidas); guardar segredos/PII em qualquer camada; escrever sem `await`; inventar nomes de método — abra o módulo antes.

## Security Implementation

```python
# Permission check
async def check_permission(self, resource: str, action: str) -> bool:
    return await self.permission_manager.check(
        resource=resource,
        action=action,
        context=self.security_context,
    )

# Audit logging
await self.audit_logger.log(AuditEvent(
    timestamp=datetime.now(),
    event_type=AuditEventType.TOOL_EXECUTION,
    severity=SeverityLevel.INFO,
    operation=operation,
    user=self.current_user,
    details=details,
))

# Sanitização de input
def sanitize_input(self, user_input: str) -> str:
    sanitized = re.sub(r"[;&|`$()]", "", user_input)
    if not self._validate_pattern(sanitized):
        raise ValidationError("Invalid input pattern")
    return sanitized
```

❌ Nunca: confiar em entrada do usuário para shell/SQL/filesystem sem sanitização; ação privilegiada sem `check_permission()` antes; logar segredos ou bodies completos; escrever formato livre — use `AuditEvent`.

## Intent Analysis Integration

```python
intent_pattern = IntentPattern(
    pattern=r"create a new (\w+) for (\w+)",
    intent_type="creation",
    confidence_threshold=0.8,
    extractors={"entity_type": 1, "target": 2},
)
await self.intent_analyzer.register_pattern(intent_pattern)

intent_result = await self.intent_analyzer.analyze(user_message)
if intent_result.confidence > 0.7:
    workflow = await self.workflow_generator.create(intent_result)
    await self.workflow_executor.execute(workflow)
```

## Error Handling

```python
class ToolExecutionError(DEILEError): ...
class ValidationError(DEILEError): ...
class PermissionError(DEILEError): ...

try:
    result = await dangerous_operation()
except PermissionError as exc:
    logger.warning("Permission denied: %s", exc)
    return ErrorResponse(code="PERMISSION_DENIED", message=str(exc))
except ValidationError as exc:
    logger.info("Validation failed: %s", exc)
    return ErrorResponse(code="VALIDATION_FAILED", message=str(exc))
except Exception as exc:  # último recurso
    logger.error("Unexpected error: %s", exc, exc_info=True)
    return ErrorResponse(code="INTERNAL_ERROR", message="Unexpected error")
```

❌ Nunca: `bare except`; `except Exception: pass`; capturar `asyncio.CancelledError` sem re-raise; vazar string de `Exception` para usuário — mapear a subclasse tipada de `DEILEError`.

## Testing Requirements

```python
@pytest.mark.unit
async def test_tool_execution():
    tool = CustomTool()
    context = ToolContext(parsed_args={"param": "value"})
    result = await tool.execute(context)
    assert result.is_success
    assert result.data == expected

@pytest.mark.integration
async def test_workflow_execution():
    async with TestAgent() as agent:
        response = await agent.process("create a new feature")
        assert "workflow_executed" in response
        assert response["steps_completed"] == 5

@pytest.mark.security
async def test_permission_enforcement():
    with pytest.raises(PermissionError):
        await restricted_operation(user="guest")
```

Lembre: `pytest.ini` usa `--strict-markers` — registre o marker antes de usar. `asyncio_mode=auto` torna `@pytest.mark.asyncio` desnecessário.

## Skill Development

Skill = arquivo Markdown puro, sem Python. Frontmatter YAML define quando ela auto-dispara; o body é o conteúdo que entra no prompt. Hot-reload em 0,5 s — basta dropar o arquivo num dos 5 diretórios de scan.

```markdown
---
name: rust                           # opcional; default = stem do arquivo (normalizado)
description: |
  Regras específicas do projeto sobre Rust — ownership, async/Tokio
  patterns. Sobrescreve qualquer conselho genérico do treinamento.
triggers:                            # tudo opcional; vazio = só responde a /<name> ou invoke_skill
  file_globs: ["*.rs", "Cargo.toml"]
  code_block_langs: [rust, rs]       # case-insensitive
  keywords: ["ownership", "borrow checker", "tokio"]
  file_content_patterns:             # regex MULTILINE; sample = 4 KiB do início do arquivo
    - '^use tokio::'
    - '#\[tokio::main\]'
priority: 50                         # int; default 0. Maior aparece primeiro no ranking
---

# Rust expertise

Conteúdo livre em Markdown. Quando uma trigger casa, esse body inteiro
entra no system prompt como "### Skill: rust". Quando o LLM chama
`invoke_skill(name="rust")`, esse body é o que ele recebe. Quando o usuário
digita `/rust [args]`, esse body é enviado como prompt (com os args
concatenados).

Recomendações de redação:
- Comece com regras imperativas curtas, não exposição teórica.
- Termine com exemplos concretos do projeto, não código genérico.
- Mencione decisões e exceções específicas do projeto que sobrescrevam
  conhecimento de treinamento.
```

| Onde dropar | Quando |
|---|---|
| `~/.deile/skills/<name>.md` | Skill pessoal — visível em qualquer projeto seu |
| `<cwd>/.deile/skills/<name>.md` | Skill do projeto — vai junto no git |
| `~/.claude/commands/<name>.md` | Compat Claude Code (nome vira UPPERCASE) |
| `deile/skills/library/**/<name>.md` | Bundled (vai no pacote DEILE; PR no repo) |

Override: a ordem é `bundled < user < user-claude < project < project-claude < extras`. Um arquivo posterior substitui o anterior em colisão de nome (com log INFO).

❌ Nunca: usar `name: null` ou ausente quando o stem normalizado fica vazio (skip silencioso); usar `priority: yes` (YAML 1.1 lê como `True` — rejeitado explicitamente); referenciar `../` em `file_content_patterns` para tentar ler fora do `project_root` (containment); duplicar nome de um built-in slash command (`/help`, `/model`, etc. — colisão filtrada com warning).

## Configuration Management

```python
class ToolSettings(BaseSettings):
    enabled: bool = True
    timeout: int = 30
    retry_count: int = 3

    class Config:
        env_prefix = "DEILE_TOOL_"
        case_sensitive = False
```

Para acesso global: sempre `from deile.config.settings import get_settings`.

## Logging Standards

```python
import logging
logger = logging.getLogger(__name__)

logger.info(
    "Tool executed",
    extra={
        "tool": tool_name,
        "duration": execution_time,
        "success": result.success,
        "user": context.user,
    },
)

logger.debug("Processing input: %s...", input[:100])

logger.error(
    "Operation failed",
    extra={"operation": op_name, "error": str(exc)},
    exc_info=True,
)
```

## Performance Optimization

```python
# Lazy loading
class LazyResource:
    def __init__(self):
        self._resource = None
    async def get(self):
        if self._resource is None:
            self._resource = await self._load()
        return self._resource

# Connection pooling com semaphore
class ConnectionPool:
    def __init__(self, size: int = 10):
        self._pool = asyncio.Queue(maxsize=size)
        self._semaphore = asyncio.Semaphore(size)
    async def acquire(self):
        async with self._semaphore:
            return await self._pool.get()
```
