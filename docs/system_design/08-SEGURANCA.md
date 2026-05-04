# 08 — Segurança

> Permissões, audit logging, scanner de segredos, sistema de aprovação. Implementação dispersa em `deile/security/`, `deile/orchestration/approval_system.py`, `deile/plugins/sandbox.py` e `deile/evolution/safety_sandbox.py`.

## Permissões (em `deile/security/permissions.py`)

| Símbolo | Papel |
|---|---|
| `PermissionLevel` (enum) | Níveis de permissão |
| `ResourceType` (enum) | Tipos de recursos protegidos |
| `PermissionRule` | Regra individual de permissão |
| `PermissionManager` | Gerenciador rule-based; singleton via `get_permission_manager()` |
| Configuração default | Alimentada por `config/permissions.yaml` (no diretório raiz `config/`) |

## Audit logging (em `deile/security/audit_logger.py`)

| Símbolo | Papel |
|---|---|
| `AuditEventType` (enum) | Tipos: `SANDBOX_VIOLATION`, etc. |
| `SeverityLevel` (enum) | Severidade |
| `AuditEvent` (dataclass) | Evento tipado com `timestamp`, `event_type`, `severity`, `details`, etc. |
| `AuditLogger` | Persiste e indexa eventos; singleton via `get_audit_logger()` |

### Helpers de conveniência

| Helper | Quando usar |
|---|---|
| `log_permission_check(tool_name, resource, action, allowed, **kwargs)` | Decisões de permissão |
| `log_secret_detection(file_path, secret_type, line_number, confidence, redacted=True)` | Detecção de segredo |
| `log_tool_execution(tool_name, resource, success, **kwargs)` | Execução de tool |
| `log_sandbox_violation(tool_name, violated_resource, violation_type, blocked=True)` | Violação de sandbox |
| `log_plan_execution(plan_id, action, result, step_count=0, duration_ms=0, **kwargs)` | Execução de plano |
| `log_approval_event(plan_id, step_id, approval_action, tool_name, risk_level, **kwargs)` | Decisão de aprovação |

## Scanner de segredos (em `deile/security/secrets_scanner.py`)

| Símbolo | Papel |
|---|---|
| `SecretType` (enum) | Categorias de segredo |
| `SecretMatch` (dataclass) | Match individual com posição, redação |
| `SecretsScanner` | Varredura em strings/arquivos com padrões conhecidos |
| Tool de uso visível pelo LLM | `deile/tools/secrets_tool.py` |

### Padrões cobertos

Além das chaves de cloud/SCM tradicionais (AWS, GitHub, Slack, RSA…), o scanner detecta tokens da integração com o daemon `deile-bot`:

| Token | Pattern (resumo) |
|---|---|
| `DEILE_BOT_AUTH_TOKEN` / `DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN` | `DEILE_BOT(_CONTROL_PLANE)?_AUTH_TOKEN\s*=\s*[A-Za-z0-9_-]{16,}` |
| Token de bot Discord | `(?:DISCORD\|DEILE_BOT_DISCORD)_TOKEN\s*=\s*xxx.yyy.zzz` (3 segmentos `.`-separados) |

## Mensageria proativa (deile → deile-bot)

| Aspecto | Detalhe |
|---|---|
| Categoria das tools | `ToolCategory.MESSAGING` (em `deile/tools/base.py`) |
| Permission gate | `MessagingTool` (em `deile/tools/messaging/_base.py`) chama `PermissionManager.check_permission()` antes de qualquer operação. Resource string: `messaging:<tool_name>:<channel_id\|user_id\|role_id>` |
| Approval gate | Tools com `require_approval=True` (`discord_send_dm`, `discord_mention_role`) passam por `ApprovalSystem.request_approval(...)` antes de executar; recusa → `ToolResult.error_result(code="APPROVAL_REQUIRED")` |
| Audit obrigatório | Cada chamada emite `AuditEvent(TOOL_EXECUTION)` com `details={tool, channel_id?, user_id?, role_id?, message_id?, text_hash?}`. **Texto cru nunca é logado** — apenas SHA8 do conteúdo |
| Auth do canal | Bearer token via `DEILE_BOT_AUTH_TOKEN`. Bind do daemon em `127.0.0.1` por padrão (controle do operador) |
| Tokens auditados | `secrets_scanner` detecta `DEILE_BOT_AUTH_TOKEN`, `DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN`, `DEILE_BOT_DISCORD_TOKEN` |

## Sistema de aprovação (em `deile/orchestration/approval_system.py`)

| Símbolo | Papel |
|---|---|
| `ApprovalStatus` (enum) | `pending`, `approved`, `rejected`, … |
| `RiskLevel` (enum) | Risco da operação |
| `ApprovalRequest` | Pedido com `id`, contexto, expiração; expõe `is_expired()`, `time_remaining()`, `to_dict()`/`from_dict()` |
| `ApprovalRule` | Regra que casa com requests (`matches(request)`) |
| `ApprovalSystem` | Coordena regras, fila e decisões |

> `PlanManager` invoca o sistema de aprovação ao executar steps de risco, antes de chamar a tool.

## Sandboxes

> Existem dois sandboxes distintos no projeto, cada um com escopo próprio.

| Sandbox | Local | Escopo |
|---|---|---|
| `PluginSandbox` | `deile/plugins/sandbox.py` | Isola plugins de terceiros — implementação atual mantém instâncias em dicionário e expõe `isolate_plugin`, `execute_in_sandbox` |
| `safety_sandbox` (módulo) | `deile/evolution/safety_sandbox.py` | Ambiente seguro para o módulo de evolução (auto-modificação) testar mudanças em diretório temporário |

> Não existe classe global `SandboxExecutor`. Pedidos de sandbox em outros contextos devem usar uma das duas implementações acima ou criar um novo módulo dedicado — não inventar a partir de imports inexistentes.

## Comandos slash relacionados a segurança (em `deile/commands/builtin/`)

| Comando | Função |
|---|---|
| `permissions_command.py` | Gestão de regras |
| `sandbox_command.py` | Interação com sandbox |
| `approve_command.py` | Gestão de aprovações pendentes |
| `logs_command.py` | Exibição de logs/auditoria |

> A descrição funcional consolidada está em [`04-MODELO-COMPONENTES.md`](04-MODELO-COMPONENTES.md).

## Skills como fronteira de confiança (em `deile/commands/skill_loader.py`)

O sistema de skills (issue #41) descobre arquivos `.md` em duas pastas e expõe cada um como um comando `/<nome>` cujo corpo do arquivo vira **prompt enviado ao LLM**. Isso é um vetor de extensão por arquivo — qualquer pessoa que dropar um `.md` em uma das pastas registra um comando no agente. Tratar como superfície de confiança:

| Pasta | Origem | Confiança | Como mitigar |
|---|---|---|---|
| `~/.deile/skills/` | Per-usuário | Igual ao `$HOME` do usuário. Quem escreve em `$HOME` já tem todos os privilégios do usuário. | Nada extra: o atacante já venceu se chegou aqui. |
| `<projeto>/.deile/skills/` | Per-projeto, **commitada no repo** | Igual ao código do projeto. `git clone <repo-não-confiável> && python deile.py` carrega skills do autor do repo. | **Trate o conteúdo de `.deile/skills/*.md` como código auditável.** Faça code review desses arquivos como faria com Python. |

### Guard-rails implementados

- **Sem override de built-ins**: `SkillLoader.load_into_registry` consulta `registry.get_command(name)` antes de registrar e PULA qualquer skill cujo nome (ou alias) já esteja ocupado por um comando existente. Skills NÃO podem hijack `/help`, `/model`, `/cost`, `/permissions`, `/sandbox`, `/approve`, etc.
- **Validação estrita de frontmatter**: `name` e `description` no YAML devem ser strings; valores nulos/listas/dicts são rejeitados (cai no stem do arquivo / descrição padrão), e YAML malformado faz a skill ser pulada com warning loud.
- **Regex restritiva no nome**: `^[a-z0-9][a-z0-9\-]{0,63}$` — sem `..`, sem `/`, sem null bytes (impede skill name como vetor de path traversal).
- **Project sobrescreve user**: se uma mesma skill existe em ambas as pastas, a versão do projeto vence (intencional — projetos definem suas próprias workflows).

### O que o sistema NÃO faz (consciente)

- Skills **não passam por `PermissionManager`** antes de executar. O prompt vai direto para o LLM, e qualquer tool-use que o LLM proponha em resposta passa pelas verificações normais de permissão. Mas o prompt em si é executado sem `check_permission`. Isso é consistente com como built-in slash commands funcionam.
- Skills **não geram entrada de `AuditLogger`** específica. As tool calls que elas dispararem geram, mas a invocação `/skill-name` em si não é logada como evento auditável tipado. Se você precisar disso, é um TODO.
- Não há **sandbox** para o conteúdo do prompt. Um skill pode injetar instruções que tentem coagir o LLM a ignorar regras (prompt injection clássico). Confie no `.deile/skills/` apenas tanto quanto confia no código que executaria o LLM.

### Recomendações operacionais

- Em CI / projeto compartilhado: **trate `.deile/skills/` como diretório protegido** (CODEOWNERS, branch protection). Trate adições/mudanças com o mesmo rigor de PRs de código.
- Antes de rodar `python deile.py` em um repo clonado, faça `ls .deile/skills/ 2>/dev/null` para saber o que vai ser carregado.

## Regras inegociáveis

| Regra | Detalhe |
|---|---|
| Permissão antes da ação | Verificação **antes** de qualquer ação privilegiada |
| Sanitização | Antes de shell/SQL/filesystem |
| Audit tipado | Via `AuditEvent` — formato livre é proibido |
| Sem segredos em log | Não logar segredos nem corpos de request inteiros |
| Classificação de risco em tools | Tools que tocam shell ou rede declaram `SecurityLevel.DANGEROUS` ou `MODERATE` apropriado em `ToolSchema` |
| Aprovação em planos | Decisões de risco em planos passam por `ApprovalSystem` |
