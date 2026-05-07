---
name: Intenção
about: Registre uma intenção, direção ou desejo de evolução para o DEILE
title: '[INTENT] '
labels: 'intent'
assignees: ''
---

## Resumo da Intenção
<!-- Descrição curta e objetiva do que gostaríamos de ver existir no DEILE. -->

## Classificação

<!-- Marque exatamente uma opção. -->

- [ ] **Exploratória** — ideia inicial ou hipótese; ainda sem solução definida
- [ ] **Direcional** — há uma direção clara, mas a forma exata de implementar continua em aberto
- [ ] **Estratégica** — intenção importante para a evolução do produto ou da arquitetura, com alto potencial de virar epic, feature ou conjunto de refactors

## Valor Esperado
<!-- Por que vale a pena ter isso? Que ganho esperado isso traz para o produto, usuário, contribuidor ou arquitetura? -->

## Problema, Lacuna ou Oportunidade
<!-- Que ausência, limitação, atrito ou oportunidade motivou esta intenção? -->

## O Que Gostaríamos de Ter
<!-- Descreva o resultado desejado em alto nível. Foque no "o quê" e no "por quê", não no desenho detalhado da solução. -->

## Sinais de Que Vale Refinar
<!-- Que evidências, sintomas, pedidos recorrentes ou cenários indicam que esta intenção merece virar algo mais concreto? -->

- [ ] Há um caso de uso real recorrente
- [ ] Há ganho claro de UX, DX, capacidade ou manutenção
- [ ] Há contexto suficiente para evoluir para feature, refactor ou investigação
- [ ] 

## Fora do Escopo Neste Momento
<!-- O que ainda NÃO está sendo decidido agora? Ex.: solução técnica, API final, cronograma, migração, UI definitiva. -->

## Próximo Passo Sugerido
<!-- Marque exatamente uma opção. -->

- [ ] Investigar melhor antes de especificar
- [ ] Converter em `feature_request`
- [ ] Converter em `refactor_proposal`
- [ ] Manter apenas como direção futura / backlog de ideias

## Workflow de Refinamento para Features
<!-- Use este fluxo quando a intenção já permitir derivar uma ou mais features. -->

- [ ] Identificar quantos blocos funcionais realmente distintos existem nesta intenção
- [ ] Agrupar em uma única `feature_request` tudo que fizer parte do mesmo fluxo, mesma entrega percebida pelo usuário ou mesma capacidade central
- [ ] Abrir múltiplas `feature_request` apenas quando houver features independentes, priorização separada, áreas diferentes do sistema ou possibilidade real de entrega em momentos diferentes
- [ ] Vincular cada `feature_request` derivada a esta `intent`
- [ ] Apenas se todas as `feature_request` criadas/referenciadas já tiverem sido abertas, esta `intent` pode ser fechada
- [ ] Se não houver clareza suficiente para separar features, manter esta `intent` como item exploratório até novo refinamento

## Features Derivadas
<!-- Liste as issues de feature abertas a partir desta intenção. Agrupe sempre que possível. -->

- [ ] `feature_request` #____ — 

## Regra de Fechamento
<!-- GitHub não fecha esta issue automaticamente apenas porque outra issue a referenciou. -->

- Para rastreabilidade, referencie esta `intent` nas `feature_request` derivadas com algo como: `Originada de #____`
- Para fechamento automático nativo do GitHub, use keywords como `Closes #____` em um PR ou commit que resolva esta intenção
- Se a `intent` servir apenas como item de triagem e já tiver sido totalmente desdobrada em features, o fechamento pode ser manual

## Contexto Adicional
<!-- Links para issues relacionadas, exemplos, referências, documentos de pilar ou qualquer informação útil para futura revisão. -->
