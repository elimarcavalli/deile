---
name: plan
description: "Cria um plano de implementação estruturado antes de mutar código. Use ao iniciar qualquer tarefa não-trivial."
---

# Planejar Implementação

Antes de escrever código, estruture o trabalho em etapas claras e verifique
o alinhamento com os Critérios de Aceite da issue.

## Checklist

1. **Leia os ACs da issue** — identifique o que é testável e o que é comportamental.
2. **Analise o impacto** — quais arquivos precisam mudar? Há dependências?
3. **Escreva o plano** em tópicos ordenados (máx. 10 passos).
4. **Identifique os testes** — quais testes cobrem o que você vai mudar?
5. **Execute o plano** — passo a passo, marcando cada item como feito.
6. **Confronte ENTREGA vs ACs** antes de criar a PR.

## Formato do plano

```
## Plano — Issue #N

### Impacto
- Arquivos a criar: [lista]
- Arquivos a modificar: [lista]
- Testes impactados: [lista]

### Passos
1. [ ] ...
2. [ ] ...

### ACs vs Entrega
- AC #1: [VERDE/VERMELHO] — <evidência>
- AC #2: [VERDE/VERMELHO] — <evidência>
```
