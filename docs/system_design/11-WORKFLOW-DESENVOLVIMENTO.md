# 11 — Workflow de Desenvolvimento

> Fluxo operacional para qualquer mudança no `deile/`. Tier de escopo determina quais fases rodam. Templates concretos em [`12-PADROES-CODIGO.md`](12-PADROES-CODIGO.md). Princípios em [`03-PRINCIPIOS-ARQUITETURAIS.md`](03-PRINCIPIOS-ARQUITETURAIS.md).

## Tiers de escopo

| Tier | Quando se aplica | Fases que rodam | Gate de aprovação do usuário | Gate de geração de docs |
|---|---|---|---|---|
| **Trivial** | Typo, whitespace, ajuste de uma linha | nenhuma | não | não |
| **Small** | Fix em arquivo único; sem novo símbolo público; sem mudança de contrato público | 1, 3, 4, 5 | não | não |
| **Medium** | Novo símbolo público em um único subpacote; ou novo arquivo de teste | 1, 2 (resumido), 3, 4, 5, 7 | apenas se o usuário pedir | não |
| **Large** | Nova tool/comando/parser; ≥2 subpacotes; nova feature; refactor cross-module | **1–7 (full)** | **sim — esperar antes da Fase 3** | **sim — esperar confirmação de teste do usuário** |

### Regras gerais

| Regra | Detalhe |
|---|---|
| Em dúvida entre dois tiers | Escolha o **maior** |
| Escopo crescer no meio | **Reescalone** o tier e rode retroativamente as fases obrigatórias antes de declarar a tarefa pronta |
| Documento de referência | As fases abaixo são escritas para o tier Large; cada cabeçalho `Fase N` está anotado com o tier mínimo |
| Adicionar um CLI worker à frota (Decisão #51) | **Não é refactor cross-module:** escrever **um** adapter `infra/k8s/cli_adapters/<kind>.py` + testes — nenhum consumidor é reescrito (registro auto-descoberto dirige resolver/painel/manifests). Template em [`12-PADROES-CODIGO.md`](12-PADROES-CODIGO.md) (CLI Adapter Development) |

## Fase 1 — Análise de Intenção e Entendimento _(Small+)_

| # | Ação |
|---|---|
| 1 | Parsear o pedido do usuário pelo `IntentAnalyzer` para identificar o objetivo |
| 2 | Se a confidence estiver abaixo do threshold, fazer perguntas até clareza |
| 3 | Decidir: tool single ou orquestração multi-step? |
| 4 | Considerar implicações de segurança e permissões necessárias |
| 5 | Declarar entendimento do intent + abordagem proposta para validação |

## Fase 2 — Design Arquitetural e Planejamento _(Medium+ — resumido em Medium, plano completo + gate em Large)_

> Antes de qualquer implementação, apresentar plano com:

| Aspecto | Detalhe |
|---|---|
| Component analysis | Módulos afetados (tools, parsers, commands, personas) |
| Dependency mapping | Novas deps e pontos de integração |
| Interface contracts | Interfaces novas/modificadas com tipos |
| Registry updates | Tools, comandos, parsers a registrar |
| Memory impact | Qual camada (working/episodic/semantic/procedural) |
| Security assessment | Permissões e consideração de auditoria |
| Performance analysis | Async, caching, recursos |
| Test strategy | Unit, integration, security |

| Tier | Comportamento |
|---|---|
| Large | Aguardar aprovação do usuário antes de prosseguir para Fase 3 |
| Medium | Apresentar e seguir, exceto se o usuário pedir revisão |

## Fase 3 — Implementação seguindo Clean Architecture _(Small+)_

| Diretriz | Detalhe |
|---|---|
| Estrutura | Hexagonal com separação clara de camadas |
| Async/await | Para toda I/O |
| Validação | Pydantic v2 para dados e contratos |
| Componentes extensíveis | Registry Pattern |
| SOLID | Especialmente Single Responsibility |
| Erros | Subclasses específicas de `DEILEError` |
| Logging | Para debug e auditoria |
| Segurança | Validações em todas as fronteiras |

## Fase 4 — Testes e Validação _(Small+)_

> Antes de apresentar a implementação, verificar:

| Verificação | O que checar |
|---|---|
| Type Safety | Modelos Pydantic e type hints |
| Async Patterns | Uso correto de async/await sem bloqueio |
| Error Scenarios | Edge cases, null inputs, falhas |
| Security Checks | Permissões e sanitização |
| Memory Management | Cleanup adequado |
| Performance | Sem operações bloqueantes em contexto async |
| Integration Points | Compatibilidade com registries |
| Documentation | Docstrings nos públicos |

> Refinar a implementação com base nessa revisão.

## Fase 5 — Entrega e Instruções de Teste _(Small+)_

| # | Ação |
|---|---|
| 1 | Apresentar implementação final com paths |
| 2 | Comandos pytest com asserts de exemplo |
| 3 | Cenários de teste de integração demonstrando a feature (Medium+) |
| 4 | Exemplo de uso pela CLI (Medium+) |
| 5 | Documentar requisitos de configuração novos |

| Tier | Encerramento |
|---|---|
| Large | Encerrar pedindo testes ao usuário, declarando que a documentação só será gerada após confirmação. **Não prosseguir** para Fase 6 sem ser solicitado |
| Small / Medium | Declarar pronto e parar; Fase 6 não roda |

## Fase 6 — Geração de Documentação _(Apenas Large)_

| # | Ação |
|---|---|
| 1 | **NÃO gerar documentação** até o usuário confirmar testes bem-sucedidos |
| 2 | Após confirmação, pedir um título conciso da feature |
| 3 | Gerar documentação completa seguindo [`13-PADRAO-DOCUMENTACAO.md`](13-PADRAO-DOCUMENTACAO.md) |
| 4 | Incluir decisões arquiteturais, detalhes de implementação, exemplos de uso |
| 5 | Nome de arquivo proposto: `docs/YYMMDD_HHMM_FEATURE_TITLE.md` |

## Fase 7 — Checklist de Integração _(Medium+)_

> Após a implementação completa (e, em Large, após Fase 6):

- [ ] Atualizar registries relevantes (`tool_registry`, `command_registry`, `parser_registry`).
- [ ] Adicionar entradas de configuração se necessário.
- [ ] Atualizar `intent_patterns.yaml` se aplicável.
- [ ] Estender suítes de teste com novos casos.
- [ ] Atualizar `README.md` se a feature é user-facing.
- [ ] Atualizar instruções de persona se o comportamento muda.
- [ ] Verificar que hot-reload continua funcionando.
- [ ] Confirmar que audit logging captura as novas operações.

## Export OTLP-traces dos eventos dispatch.*/git.*/forge.* (extensão da Decisão #39)

> Implementado em `deile/observability/dispatch_schema.py` + `deile/observability/dispatch_export.py` — Decisão #48. Ver também [`DECISOES.md #48`](DECISOES.md).

### Topologia de spans

```
root span: deile.dispatch  (task_id, session_id, model, schema_version, role, pod)
│
├── span event: dispatch.received       (quando o dispatch_logger abre o ciclo)
├── span event: dispatch.model_resolved (model selecionado)
├── span event: dispatch.progress       (mensagem de progresso, turn_index)
├── span event: dispatch.tool_burst     (tool_name, count)
├── span event: dispatch.completed      → set_status(OK) + end()
│   └── ou: dispatch.failed             → set_status(ERROR) + end()
│
├── child span: git.commit   (commit_sha, branch, files_changed)
├── child span: git.push     (branch, remote)
├── child span: forge.pr_open   (pr_number, title, base_branch)
└── child span: forge.pr_review (pr_number, event, review_sha)
```

### Ponto de integração

Os `emit_*` em `dispatch_export.py` são o **hook único** — o `dispatch_logger` chamará cada função no evento correspondente. Antes de #435 mergear, os `emit_*` ficam prontos mas sem chamador em produção.

```python
from deile.observability import (
    emit_dispatch_received,
    emit_dispatch_completed,
    emit_git_commit,
    emit_forge_pr_review,
)

# No dispatch_logger (quando #435 mergear):
emit_dispatch_received(task_id=tid, session_id=sid, model=m)
# ... ciclo ...
emit_forge_pr_review(task_id=tid, pr_number=n, event="APPROVE", review_sha=sha)
emit_dispatch_completed(task_id=tid, turns=t, tokens_in=i, tokens_out=o)
```

### Query Grafana Tempo (exemplo)

```logql
{span_name="deile.dispatch"} | json | task_id=`<tid>`
```

Para ver toda a árvore (root + child spans) de um dispatch:

```
TraceQL: { span.task_id = "<task_id>" }
```

### Redact automático

`_redact_value(v)` mascara qualquer atributo cujo valor contenha padrões sensíveis:

| Padrão | Substituído por |
|---|---|
| `ghp_...` | `ghp_***` |
| `glpat-...` | `glpat-***` |
| `gldt-...` | `gldt-***` |
| `glsoat-...` | `glsoat-***` |
| `Bearer <token>` | `Bearer ***` |
| `sk-...` | `sk-***` |
| `AKIA...` | `AKIA***` |
| base64 > 40 chars | `<redacted-base64>` |

### Config e fallback no-op

Segue exatamente o mesmo contrato da Decisão #39:

| Condição | Comportamento |
|---|---|
| `DEILE_OTLP_ENDPOINT` vazio | 0 spans emitidos (SDK init pulado) |
| SDK `opentelemetry-sdk` não instalado | 0 spans (fallback no-op silencioso) |
| Exporter raise | drop counter incrementa; log `dispatch.otlp_drop` ≤1×/60s |

---

## Exemptions (sem fases obrigatórias)

| Caso | Exemption |
|---|---|
| Typos, whitespace, ajustes cosméticos de uma linha | Sim |
| Renomeação de variável estritamente local | Sim |
| Perguntas read-only não-arquiteturais | Sim |
| Rodar testes, lint, formatadores ou comandos `git` read-only | Sim |
| Editar `.env`, lockfiles ou artefatos auto-gerados | Sim |

> Em dúvida, **não é exemption** — rode o protocolo.
