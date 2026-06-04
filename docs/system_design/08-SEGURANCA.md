# 08 — Segurança

> Permissões, audit logging, scanner de segredos, sistema de aprovação. Implementação dispersa em `deile/security/`, `deile/orchestration/approval_system.py` e `deile/plugins/sandbox.py` (skeleton — não isola).

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
| `AuditEventType` (enum) | Tipos: `PERMISSION_DENIED`, `SECRET_DETECTED`, `TOOL_EXECUTION`, `PLAN_EXECUTION`, `SECURITY_POLICY_CHANGED`, etc. |
| `SeverityLevel` (enum) | Severidade |
| `AuditEvent` (dataclass) | Evento tipado com `timestamp`, `event_type`, `severity`, `details`, etc. |
| `AuditLogger` | Persiste e indexa eventos; singleton via `get_audit_logger()` |

### Emissores de `AuditEventType.SECURITY_POLICY_CHANGED`

| Local | Quando emite |
|---|---|
| `deile/commands/settings_manager.py:set_setting` / `set_preference` | Toda escrita em `.deile/settings.json` (allowed/denied/invalid/refused_secret). `details` contém `key_path`, `scope`, fingerprint SHA-256 truncado do valor antigo e novo (16 hex chars). Chaves que casam com `_SECRET_KEY_PATTERNS` (`token`, `key`, `secret`, `password`, `api_`) viram `"<redacted>"` em vez de hash — valor cru nunca vai para o log nem para `details.error`. Issue #125. |
| `deile/commands/settings_manager.py:add_skills_path` / `remove_skills_path` | Cada modificação de `skills_paths`. `details.path_fingerprint` carrega SHA-256 truncado do path. |

> O resource string segue `settings:<scope>:<detail>` (e.g. `settings:global:logging.level`). A regra default `settings_write_default` (`deile/security/permissions.py:_load_default_rules`) é **fail-closed** (`PermissionLevel.READ` = nega `write`) desde a issue #125. Operadores que querem habilitar escrita de settings devem adicionar uma regra explícita ao `config/permissions.yaml`:
>
> ```yaml
> permission_rules:
>   - id: settings_write_interactive
>     name: Settings Write (Interactive)
>     description: Allow operator-initiated settings writes
>     resource_type: file
>     resource_pattern: '^settings:(global|project):.*$'
>     tool_names: [settings_manager]
>     permission_level: write
>     priority: 40   # menor que 50 para vencer a regra default
> ```
>
> Sem essa regra, `set_setting`, `set_preference`, `add_skills_path` e `remove_skills_path` retornam `False`, emitem `AuditEvent(result="denied")` e não tocam o disco.

### Métodos tipados em `AuditLogger`

Sempre chame via instância (`get_audit_logger().log_*(...)`) — não há atalho module-level.

| Método | Quando usar |
|---|---|
| `AuditLogger.log_permission_check(tool_name, resource, action, allowed, **kwargs)` | Decisões de permissão |
| `AuditLogger.log_secret_detection(file_path, secret_type, line_number, confidence, redacted=True)` | Detecção de segredo |
| `AuditLogger.log_tool_execution(tool_name, resource, success, **kwargs)` | Execução de tool |
| `AuditLogger.log_plan_execution(plan_id, action, result, step_count=0, duration_ms=0, **kwargs)` | Execução de plano |
| `AuditLogger.log_approval_event(plan_id, step_id, approval_action, tool_name, risk_level, **kwargs)` | Decisão de aprovação |
| `AuditLogger.log_cron_fire(entry_id, name, schedule, payload_hash)` | Disparo bem-sucedido de cron |
| `AuditLogger.log_cron_skipped(entry_id, name, reason)` | Cron pulado (sem callback, etc.) |

## Scanner de segredos (em `deile/security/secrets_scanner.py`)

| Símbolo | Papel |
|---|---|
| `SecretType` (enum) | Categorias de segredo |
| `SecretMatch` (dataclass) | Match individual com posição, redação |
| `SecretsScanner` | Varredura em strings/arquivos com padrões conhecidos |
| Tool de uso visível pelo LLM | `deile/tools/secrets_tool.py` |

### Padrões cobertos

Além das chaves de cloud/SCM tradicionais (AWS, GitHub, Slack, RSA…), o scanner detecta tokens da integração com o daemon `deilebot`:

| Token | Pattern (resumo) |
|---|---|
| `DEILE_BOT_AUTH_TOKEN` / `DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN` | `DEILE_BOT(_CONTROL_PLANE)?_AUTH_TOKEN\s*=\s*[A-Za-z0-9_-]{16,}` |
| Token de bot Discord | `(?:DISCORD\|DEILE_BOT_DISCORD)_TOKEN\s*=\s*xxx.yyy.zzz` (3 segmentos `.`-separados) |
| **GitHub** (decisão #41) | `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` (PATs/auth tokens), `github_pat_…` (fine-grained PATs) |
| **GitLab** (decisão #41) | `glpat-` (personal), `gldt-` (deploy), `glptt-` (project trigger), `glsoat-` (agent OAuth), `GITLAB_TOKEN=` / `GL_TOKEN=` / `CI_JOB_TOKEN=`, catch-all defensivo `gl[a-z]+-` |

### Auth de forges (issue #297)

A pipeline e o worker autenticam contra GitHub e GitLab via **tokens estáticos** lidos em `/run/secrets/deile/` por `infra/k8s/wrapper.py` no bootstrap. Postura simétrica para ambos:

| Forge | Env var origem | Materializa em | Strip pós-bootstrap |
|---|---|---|---|
| GitHub | `GITHUB_TOKEN` | `~/.git-credentials` (linha `oauth2:<tok>@<host>`) + `~/.config/gh/hosts.yml` | sim — removido de `os.environ` antes do agente subir |
| GitLab | `GITLAB_TOKEN` (alias `GL_TOKEN`) | `~/.git-credentials` (linha `oauth2:<tok>@<host>`) + `~/.config/glab-cli/config.yml` | sim — removido de `os.environ` antes do agente subir |

`wrapper._setup_forge_credentials()` é a função única — `_setup_git_credentials` e `_setup_gh_auth` são wrappers retro-compatíveis que delegam a ela. Subprocessos (`bash_tool`, `python_execute`) **nunca** veem os tokens em `/proc/self/environ`. Operação dual (DEILE servindo GH e GL simultaneamente) é só popular os dois Secrets — todo o resto é transparente.

## Mensageria proativa (deile → deilebot)

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
| `PluginSandbox` | `deile/plugins/sandbox.py` | **Skeleton — não isola nada hoje.** Apenas guarda instâncias em um dicionário; `PluginManager` nem invoca esta classe. Plugins carregados rodam com privilégios totais do processo DEILE. Veja issue #54. |

> Não existe classe global `SandboxExecutor` nem sandbox para o módulo de evolução. Pedidos de sandbox em outros contextos devem criar um módulo dedicado — não inventar a partir de imports inexistentes. Plugins de terceiros devem ser auditados como código.

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
