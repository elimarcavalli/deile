# BG002 - Sistema de Personas Não Está Sendo Utilizado

## DESCRIÇÃO DO PROBLEMA

O sistema tem um **PersonaManager completo e sofisticado** com:
- ✅ Arquivos YAML de configuração (`deile/personas/library/*.yaml`)
- ✅ Sistema de hot-reload
- ✅ PersonaManager com descoberta automática
- ✅ Personas padrão (developer.yaml, architect.yaml, debugger.yaml)

**MAS o system instruction ainda está hardcoded em `context_manager.py:207-244`**

## REPRODUÇÃO

1. PersonaManager é inicializado no `__init__` do ContextManager
2. Método `_build_system_instruction()` ignora completamente o PersonaManager
3. Retorna sempre a instrução hardcoded gigante

## CÓDIGO PROBLEMÁTICO

```python
# deile/core/context_manager.py:207-244
base_instruction = (
    " 🧠 [PERSONA E OBJETIVO PRINCIPAL] "
    " Você é DEILE, um agente de IA sênior, especialista em desenvolvimento de software, proativo e altamente autônomo. "
    # ... mais 30+ linhas hardcoded
)
```

## IMPACTO

- ❌ Sistema de personas completamente ignorado
- ❌ Configurações YAML não são usadas
- ❌ Hot-reload não funciona
- ❌ Flexibilidade perdida
- ❌ Manutenibilidade comprometida

## EVIDÊNCIA

- `PersonaManager` existe e está funcional
- `developer.yaml` tem system_instruction detalhada
- `context_manager.py` tem `self.persona_manager` mas nunca usa
- Method `_build_system_instruction()` ignora personas completamente