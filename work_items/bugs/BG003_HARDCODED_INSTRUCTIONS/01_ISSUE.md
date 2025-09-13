# BG003 - Instruções Hardcoded No Sistema

## DESCRIÇÃO DO PROBLEMA

Apesar de termos PersonaManager funcional, ainda existem **instruções hardcoded gigantes** no método `_build_hardcoded_system_instruction()` em `context_manager.py:230-264`.

**REQUISITO:** Todas as instruções devem vir de arquivos MD, zero hardcode no código.

## INSTRUÇÕES HARDCODED ENCONTRADAS

### `context_manager.py:230-264`
```python
base_instruction = (
    " 🧠 [PERSONA E OBJETIVO PRINCIPAL] "
    " Você é DEILE, um agente de IA sênior, especialista em desenvolvimento de software..."
    " 🚀 [DIRETRIZES DE OPERAÇÃO AUTÔNOMA] "
    " 1. EXECUÇÃO DIRETA: Execute tarefas imediatamente..."
    # ... mais 30+ linhas hardcoded
)
```

## IMPACTO

- ❌ Instruções não são configuráveis
- ❌ Manutenção difícil (código misturado com instruções)
- ❌ Não segue padrão de arquivos externos
- ❌ Fallback usa hardcode quando deveria usar arquivo MD
- ❌ Inconsistente com a arquitetura de personas

## SOLUÇÃO REQUERIDA

1. **Criar arquivo MD** com as instruções padrão
2. **Sistema de carregamento** de instruções de arquivos MD
3. **Eliminar completamente** o hardcode
4. **Fallback para arquivo MD** ao invés de hardcode

## ESTRUTURA PROPOSTA

```
deile/
├── personas/
│   ├── library/
│   │   ├── developer.yaml
│   │   └── architect.yaml
│   └── instructions/
│       ├── default.md
│       ├── fallback.md
│       └── system_base.md
```