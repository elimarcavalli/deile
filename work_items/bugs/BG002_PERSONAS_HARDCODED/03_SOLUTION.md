# BG002 - Solução

## ESTRATÉGIA DE CORREÇÃO

### 1. INICIALIZAR PersonaManager em DeileAgent

**Arquivo:** `deile/core/agent.py`
- Importar PersonaManager
- Instanciar e inicializar no `__init__()`
- Passar para ContextManager

### 2. MODIFICAR _build_system_instruction()

**Arquivo:** `deile/core/context_manager.py`
- Verificar se PersonaManager existe
- Obter persona ativa
- Usar system_instruction da persona
- Fallback para hardcoded se necessário

### 3. ATIVAR PERSONA PADRÃO

- Definir "developer" como persona padrão
- Ativar automaticamente na inicialização

## IMPLEMENTAÇÃO

### Passo 1: Modificar DeileAgent

```python
from ..personas.manager import PersonaManager

class DeileAgent:
    def __init__(self, config_path: Optional[str] = None):
        # ... código existente ...

        # Inicializa PersonaManager
        self.persona_manager = PersonaManager()
        await self.persona_manager.initialize()

        # Define persona padrão
        await self.persona_manager.switch_persona("developer")

        # Passa para ContextManager
        self.context_manager = ContextManager(
            persona_manager=self.persona_manager,
            # ... outros parâmetros ...
        )
```

### Passo 2: Modificar _build_system_instruction

```python
async def _build_system_instruction(self, parse_result, session, **kwargs) -> str:
    # Tenta usar PersonaManager primeiro
    if self.persona_manager:
        active_persona = self.persona_manager.get_active_persona()
        if active_persona and active_persona.config.system_instruction:
            # Adiciona contexto de arquivos
            file_context = await self._build_file_context(session, **kwargs)

            base_instruction = active_persona.config.system_instruction
            if file_context:
                base_instruction += f"\n\n📁 [ARQUIVOS DISPONÍVEIS NO PROJETO]\n{file_context}"

            return base_instruction

    # Fallback para hardcoded se PersonaManager não disponível
    return self._build_hardcoded_instruction(session, **kwargs)
```

### Passo 3: Manter Compatibilidade

```python
def _build_hardcoded_instruction(self, session, **kwargs) -> str:
    """Instrução hardcoded como fallback"""
    # Move código hardcoded atual para cá
    return base_instruction_hardcoded
```