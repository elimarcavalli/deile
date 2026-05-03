# Fase Revisão Cética — `deile_bot/foundation/`

> Esta fase é executada **por outra pessoa** (não quem implementou). Ler o plano completo, ler o código entregue, rodar os testes E2E, e atacar a entrega como se fosse adversário do release.

## Quem revisa

- **Não pode ser** quem escreveu mais que 30% das linhas das fases 1-3.
- **Ideal**: alguém familiar com `deile/` mas não com este plano. Olhar fresco.
- **Auxiliar**: 1h de pareamento com quem implementou para tirar dúvidas factuais (não para defender decisões).

## Roteiro (em ordem)

### 1. Leitura do plano completo

| Etapa | Tempo | Foco |
|---|---|---|
| `00-PLAN.md` | 30min | Decisões D1-D15 fazem sentido? Alguma alternativa descartada parece melhor agora? |
| Fases 01-03 | 60min | Os entregáveis cobrem o plano? Algum item ficou implícito sem fase dona? |
| `04-FASE-E2E.md` | 20min | Cenários cobrem os critérios de "feito" da seção 9 do `00-PLAN.md`? |

Anotar em rascunho: discrepâncias entre plano e implementação, decisões parecem datadas, lacunas.

### 2. Leitura do código

Prioridades nesta ordem:

1. **`foundation/exceptions.py`** — todas as exceções planejadas existem? Hierarquia correta?
2. **`foundation/envelope.py`** — frozen funcionando? Validações em `__post_init__` cobrem casos esquisitos (`message_id == ""`, `sent_at` naive, `provider` desconhecido)?
3. **`foundation/conversation_store.py`** — schema bate com `V001__init.sql`? Migrations idempotentes? WAL mode realmente ativo (`PRAGMA journal_mode`)?
4. **`foundation/identity.py`** — `bot_user_id` ULID estável? Mudança de `display_name` preserva id?
5. **`foundation/permissions.py`** — ordem de avaliação batida com plano (`blocklist > owners > per_action > allowlist`)?
6. **`foundation/rate_limit.py`** — token bucket realmente refilla? Semáforo global libera após `acquire`?
7. **`foundation/agent_bridge.py`** — timeout obrigatório? Captura de exceção genérica? Sessão por `bot_user_id` funciona ou caiu para `oneshot_cli_session`?
8. **`foundation/pipeline.py`** — fluxo do `IngressPipeline` segue exatamente os 16 passos do plano fase 3? Alguma chamada fora de ordem? Algum `audit.log` faltando?
9. **`providers/base.py`** — ABC tem `CapabilityNotSupported` por default em métodos não-universais?

### 3. Ataques de adversário

Tentar cada um destes ataques **localmente, em testes ad-hoc**, **antes** de verificar se há proteção. Se a proteção falhar, é bug.

| # | Ataque | Esperado |
|---|---|---|
| ADV-1 | Mandar envelope com `message_id == ""` | `__post_init__` levanta |
| ADV-2 | Mandar envelope com `sent_at` sem timezone | `__post_init__` levanta |
| ADV-3 | Trocar display_name de "alice" para "elimar.ciss" e tentar comando admin | `permissions.is_owner` retorna False (não confunde por nome) |
| ADV-4 | Mandar 1000 envelopes em paralelo do mesmo usuário | `rate_limited` corta; sem CPU spike >85% |
| ADV-5 | Mandar 100 envelopes em paralelo de 100 usuários distintos | `global_concurrent` enfileira; sem deadlock |
| ADV-6 | Mandar envelope com `text == ""` | Pipeline decide não-responder, não invoca agente |
| ADV-7 | Mandar envelope com `text` de 100KB | Truncado/rejeitado conforme política; sem OOM |
| ADV-8 | Forçar `bridge.invoke` a lançar `RuntimeError` aleatório | `AgentInvocationError`; `agent_failed` no audit; fallback enviado |
| ADV-9 | `FakeAdapter.send_message` retorna após 60s | `tenacity` cancela ou não? Documentar comportamento |
| ADV-10 | Drop e recriar SQLite no meio da operação | `ConversationStore` re-init não corrompe; pipeline em vôo falha graciosamente |
| ADV-11 | `BotSettings` com `intent_classifier="invalid"` | Carga de settings levanta validation error explícito |
| ADV-12 | Persona inexistente nas regras | Fallback para `default`; warning no log |
| ADV-13 | `extra_system_prompt` com `</system>` injetado por usuário (prompt injection no bot) | Foundation **deve** sanitizar — verificar se há proteção; se não há, é bug **alto** |
| ADV-14 | Mandar envelope cujo `provider_user_id` muda de tipo (str → int e vice-versa) entre chamadas | `IdentityResolver` normaliza para str; sem duplicar `bot_user` |
| ADV-15 | DLQ replay com adapter que ainda falha | Não fica em loop infinito; respeita `attempts` máximo |

### 4. Auditoria de lacunas

Marcar com **🔴 / 🟠 / 🟡 / 🟢** cada item:

- [ ] Princípios F1-F8 de `00-PLAN.md` §2 todos atendidos?
- [ ] Decisões D1-D15 todas implementadas (ou justificada a divergência por escrito)?
- [ ] Pacote `deile_bot/providers/*` realmente vazio de imports em `deile_bot/foundation/*`? (verificar via `grep -r "from deile_bot.providers" deile_bot/foundation/`)
- [ ] `pytest --cov=deile_bot/foundation` ≥ 85%?
- [ ] Schema SQLite tem `PRAGMA journal_mode=WAL` ativo? (verificar com `sqlite3 ... .databases` ou similar)
- [ ] Logs estruturados (JSON-friendly) em todos os caminhos de erro?
- [ ] Audit log inspecionável via SQL?
- [ ] Métricas exportáveis (snapshot serializa como JSON)?
- [ ] DLQ tem `purge`?
- [ ] Toda função pública tem docstring com Args/Returns/Raises?
- [ ] `FakeProviderAdapter` realmente em `_testing.py` e exportado para uso de outros pacotes?
- [ ] `pytest deile_bot/tests/e2e/ -v -m e2e` verde com custo < $0.05?
- [ ] Documentação inline coerente com o plano? (sem código fazendo coisa que o plano não previu)
- [ ] CLAUDE.md / docs/system_design atualizados se a foundation introduziu novos padrões?

### 5. Perguntas obrigatórias

Responder por escrito:

1. **Se um adapter Telegram fosse implementado amanhã, o que ele precisaria duplicar da foundation?** (Resposta esperada: nada além de normalizer + formatter + adapter de transport.)
2. **Onde é mais fácil introduzir prompt injection no bot?** Diga o caminho específico e como mitigar.
3. **Qual é o ponto único de falha que derruba todo o pipeline?** Diga e proponha mitigação.
4. **Se o `agent_bridge.invoke` ficar lento (mediana 30s), qual é o impacto observável?** Memória? Backlog? UX?
5. **Se a base SQLite for corrompida, qual é o roteiro de recuperação?** Faltou documentar?
6. **Quem opera o bot em produção tem dashboard/métricas/alertas para o quê?** Liste o mínimo viável.
7. **Há algo no plano que ficou superficial e merece um sub-plano próprio?** Liste.

### 6. Saída da revisão

Documento `05-REVISAO-RESULTADOS.md` (criado pela pessoa revisora) com:

- ✅ Itens aprovados.
- 🔴 Bloqueadores (não pode mergear; precisa de fix).
- 🟠 Não-bloqueadores que viram issues separadas.
- 🟡 Sugestões para evolução futura.
- 🟢 Coisas surpreendentemente boas (registrar para reaproveitar em outros planos).
- Resposta às 7 perguntas da seção 5.

Quem implementou tem 1 ciclo de réplica. Após isso, decisão é da pessoa revisora.

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | Todos os ataques ADV-1 a ADV-15 executados, com evidência (saída de teste, log, screenshot) |
| AC-2 | Auditoria de lacunas concluída, todos os itens marcados |
| AC-3 | 7 perguntas respondidas por escrito |
| AC-4 | `05-REVISAO-RESULTADOS.md` mergeado |
| AC-5 | Bloqueadores 🔴 endereçados antes de declarar foundation pronta |

## Estimativa de esforço

1 dia da pessoa revisora + 0,5 dia de réplica de quem implementou.
