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

## Regras inegociáveis

| Regra | Detalhe |
|---|---|
| Permissão antes da ação | Verificação **antes** de qualquer ação privilegiada |
| Sanitização | Antes de shell/SQL/filesystem |
| Audit tipado | Via `AuditEvent` — formato livre é proibido |
| Sem segredos em log | Não logar segredos nem corpos de request inteiros |
| Classificação de risco em tools | Tools que tocam shell ou rede declaram `SecurityLevel.DANGEROUS` ou `MODERATE` apropriado em `ToolSchema` |
| Aprovação em planos | Decisões de risco em planos passam por `ApprovalSystem` |
