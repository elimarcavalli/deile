# BG002 - Plano de Testes

## CASOS DE TESTE

### TESTE 1: Inicialização do PersonaManager
**Objetivo:** Verificar se PersonaManager é inicializado corretamente

**Passos:**
1. Executar `python deile.py`
2. Verificar logs de inicialização
3. Verificar se mensagem "Agent initialized successfully with PersonaManager" aparece

**Resultado Esperado:**
- PersonaManager carrega personas de `/deile/personas/library/*.yaml`
- Persona "developer" é ativada por padrão
- System instruction vem do `developer.yaml`

### TESTE 2: Validação do System Instruction
**Objetivo:** Confirmar que system instruction vem da persona, não hardcoded

**Passos:**
1. Inicializar DEILE
2. Fazer uma pergunta simples
3. Verificar logs: "Using persona 'Developer' system instruction"

**Resultado Esperado:**
- Log: `Using persona 'Developer' system instruction`
- NÃO deve aparecer: `Using hardcoded system instruction`

### TESTE 3: Conteúdo da Persona Developer
**Objetivo:** Verificar se comportamento reflete a persona

**Comando:** "Quem você é e qual sua especialidade?"

**Resultado Esperado:**
- Resposta deve mencionar ser "Developer"
- Deve mencionar especialidades em Python, APIs
- Comportamento técnico conforme `developer.yaml`

### TESTE 4: Fallback para Hardcoded
**Objetivo:** Testar fallback quando PersonaManager falha

**Simulação:** Quebrar PersonaManager temporariamente

**Resultado Esperado:**
- Log: `Using hardcoded system instruction (PersonaManager not available)`
- Sistema continua funcionando

## CRITÉRIOS DE SUCESSO

✅ **PersonaManager inicializado**
✅ **Persona "developer" ativa**
✅ **System instruction vem de developer.yaml**
✅ **Logs confirmam uso de personas**
✅ **Comportamento reflete persona developer**