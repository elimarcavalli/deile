# 13 — Padrão de Documentação

> Template das **14 seções** para documentação de feature em `docs/<YYMMDD_HHMM>_FEATURE_TITLE.md`, gerada na Fase 6 do workflow ([`11-WORKFLOW-DESENVOLVIMENTO.md`](11-WORKFLOW-DESENVOLVIMENTO.md)).

## Escopo deste padrão

| Aspecto | Detalhe |
|---|---|
| Aplicar a | Feature documentation em `docs/` e entregáveis top-level (`README.md`, `CHANGELOG.md`) |
| **NÃO** aplicar a | `CLAUDE.md` e os documentos deste `docs/system_design/` (o primeiro é meta-instrução para Claude; o segundo é o próprio System Design) |

## Mapa das 14 seções

| # | Seção | Foco |
|---|---|---|
| 1 | Overview | Propósito da feature e problema que ela resolve |
| 2 | Architectural Decisions | Razão dos patterns, trade-offs, integração async/memória/segurança |
| 3 | Component Architecture | Componentes core e infraestrutura |
| 4 | Implementation Details | Estrutura de classes (esboço em árvore) |
| 5 | API Specification | Tabela de método × interface × params × retorno × exceções × async |
| 6 | Configuration Schema | Schema YAML da feature |
| 7 | Security Implementation | Permissão, sanitização, audit, risco |
| 8 | Testing Strategy | Unit, integration, security |
| 9 | Usage Examples | CLI e programático |
| 10 | Performance Characteristics | Complexidade, memória, caching, concorrência |
| 11 | Monitoring & Observability | Métricas, logging, health checks, alertas |
| 12 | Migration & Deployment | Retrocompat, dados, rollback, feature flag |
| 13 | Troubleshooting Guide | Problemas comuns, debug, FAQ |
| 14 | Future Considerations | Extensão, otimizações, limitações, débito técnico |

## Detalhamento por seção

### 1. Overview

| Item | Detalhe |
|---|---|
| Descrição | Propósito da feature e problema que ela resolve |
| Integração | Com as capacidades autônomas do DEILE |
| Impacto | No comportamento do sistema e na experiência do usuário |
| Significado | Arquitetural dentro do sistema |

### 2. Architectural Decisions

| Item | Detalhe |
|---|---|
| Patterns escolhidos | Razão (Registry, Mediator, Observer, etc.) |
| Trade-offs | Entre alternativas |
| Async/await | Considerações de concorrência |
| Memória | Estratégia de integração |
| Segurança | Implicações e requisitos de permissão |

### 3. Component Architecture

**Core Components**:

| Item | Detalhe |
|---|---|
| Módulos | Novos/modificados com responsabilidades |
| Registries | Integração com tool/command/parser/persona |
| Memória | Interações com camadas e fluxo de dados |
| LLM | Pontos de integração e uso de function calling |

**Infrastructure**:

| Item | Detalhe |
|---|---|
| Dependências externas | Integrações de API |
| Storage | SQLite, filesystem |
| Configuração | Mudanças necessárias |
| Performance | Estratégias de otimização |

### 4. Implementation Details

> Esboço em árvore (ASCII) — não cabe em tabela. Exemplo:

```
ComponentName
├── Properties
│   ├── name: str
│   ├── description: str
│   └── configuration: dict
├── Methods
│   ├── __init__()
│   ├── async execute()
│   └── async validate()
└── Integration Points
    ├── Registry registration
    ├── Memory hooks
    └── Event subscriptions
```

### 5. API Specification

> Para cada interface/endpoint nova:

| Method | Interface | Parameters | Return Type | Exceptions | Async |
|---|---|---|---|---|---|
| execute | Tool | ToolContext | ToolResult | ToolError | Yes |
| parse | Parser | str, ParseContext | ParseResult | ParseError | Yes |

### 6. Configuration Schema

```yaml
feature_name:
  enabled: bool
  settings:
    timeout: int  # seconds
    retry_count: int
    cache_ttl: int  # seconds
  security:
    required_permission: str
    risk_level: str  # low/medium/high
  memory:
    store_in_episodic: bool
    consolidation_interval: int
```

### 7. Security Implementation

| Item | Detalhe |
|---|---|
| Permissão | Requisitos e lógica de validação |
| Sanitização | Padrões de validação de input |
| Audit | Pontos de integração com logging |
| Risco | Avaliação e mitigação |
| Sandbox | Requisitos para operações perigosas |

### 8. Testing Strategy

**Unit Tests** (estrutura exemplo):

```python
async def test_component_basic_functionality():
    component = Component()
    result = await component.execute(test_input)
    assert result.success
    assert result.data == expected_output
```

**Integration / Security Tests**:

| Tipo | Cobertura |
|---|---|
| Integration | Workflow completo, registries, memória, permissões |
| Security | Edge cases de input, bypass de permissão, exhaustão de recursos, injection |

### 9. Usage Examples

**CLI**:

```bash
> analyze the codebase for security vulnerabilities
> create a comprehensive refactoring plan for the authentication module
> /run security_scan --depth deep --include-dependencies
```

**Programático**:

```python
tool = SecurityScanTool()
context = ToolContext(parsed_args={"path": "./src", "depth": "deep"})
result = await tool.execute(context)

agent = DeileAgent(...)
response = await agent.process_input("scan for vulnerabilities", session_id="...")
```

### 10. Performance Characteristics

| Item | Detalhe |
|---|---|
| Complexidade temporal | Operações principais |
| Memória | Padrões de uso e estratégias de otimização |
| Caching | Eficácia e invalidação |
| Concorrência | Capacidade e limites |
| Recursos | Consumo sob diferentes cargas |

### 11. Monitoring & Observability

| Item | Detalhe |
|---|---|
| Métricas | Tempo de execução, taxa de sucesso, uso de recursos |
| Logging | Padrões e mensagens importantes |
| Health check | Implementação |
| Profiling | Pontos de profiling |
| Alertas | Condições e thresholds |

### 12. Migration & Deployment

| Item | Detalhe |
|---|---|
| Retrocompat | Considerações |
| Migração de dados | Requisitos |
| Migração de configuração | Passos |
| Rollback | Procedimentos |
| Feature flag | Implementação se aplicável |

### 13. Troubleshooting Guide

| Item | Detalhe |
|---|---|
| Problemas comuns | Soluções |
| Modo debug | Para diagnóstico |
| Logs | Guia de interpretação |
| Tuning | Recomendações de performance |
| FAQ | Para problemas típicos |

### 14. Future Considerations

| Item | Detalhe |
|---|---|
| Extensão | Pontos identificados |
| Otimizações | Potenciais |
| Limitações | Conhecidas e workarounds |
| Roadmap | Possibilidades de integração |
| Débito técnico | Notas |
