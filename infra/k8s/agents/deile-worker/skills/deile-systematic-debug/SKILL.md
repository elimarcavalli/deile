---
name: deile-systematic-debug
description: "Debugging sistemático para o deile-worker: isola a causa raiz antes de propor fix, com evidências em código."
---

# Debug Sistemático no Contexto DEILE

Use esta skill ao encontrar comportamento inesperado em qualquer componente DEILE.
O objetivo é chegar à causa raiz com evidências em código antes de propor qualquer fix.

## Princípio Central

**Comentário não é prova, o código é.** Cada afirmação sobre como algo funciona
deve citar `arquivo:linha`. Hipóteses sem citação são suposições.

## Checklist

1. **Reproduza o problema** — qual é o input exato e o output observado?
2. **Delimite o escopo** — qual componente (`deile/core/`, `deile/orchestration/`, etc.) está envolvido?
3. **Forme hipóteses** — liste 2-3 causas possíveis, ordenadas por probabilidade.
4. **Refute ou confirme** cada hipótese lendo o código (não suponha):
   - Leia a função suspeita completa
   - Trace o fluxo de chamada (quem chama quem)
   - Verifique os testes existentes que cobrem esse caminho
5. **Identifique a causa raiz** com `arquivo:linha` explícito.
6. **Proponha o fix** mínimo que resolve a causa raiz (não a sintoma).
7. **Escreva o teste** que reproduz o bug antes de aplicar o fix.

## Atalhos DEILE

- `deile/config/settings.py` — ponto único de configuração; verifique o valor efetivo antes de debugar comportamento de config
- `deile/core/agent.py` — loop central do agente; trace via `logging.DEBUG`
- `infra/k8s/wrapper.py` — bootstrap do pod; primeiro lugar para problemas de inicialização
- Logs: `/home/deile/logs/` (deile-worker) ou `kubectl logs <pod>` para stdout

## Anti-padrões a evitar

- Adicionar `try/except` sem entender o erro
- "Funciona na minha máquina" sem verificar env vars / ConfigMap
- Fix no sintoma sem identificar causa raiz
- Mudar código sem escrever teste que reproduz o bug
