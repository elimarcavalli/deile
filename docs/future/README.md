# `docs/future/` — planos vivos

> Tudo aqui descreve **trabalho ainda não feito**. Cada subpasta é um plano completo, com fases agrupadas, fase de testes E2E e fase de revisão cética. Quando uma feature for implementada, mover/arquivar o plano correspondente — não apagar.

## Mapa de planos

| Pasta | Tema | Estado | Prioridade |
|---|---|---|---|
| [`deile/`](deile/) | Mudanças no agente DEILE para suportar bots como consumidores de primeira classe | planejamento | 🟡 segunda onda |
| [`deile-bot/foundation/`](deile-bot/foundation/) | Camada provider-agnóstica do `deile_bot/` (memória, identidade, capacidades, bridge com o agente) | planejamento | 🔴 **PRIMEIRO** |
| [`deile-bot/discord/`](deile-bot/discord/) | Provider adapter para Discord — primeiro bot real | planejamento | 🔴 **FOCO** |
| [`deile-bot/telegram/`](deile-bot/telegram/) | Provider adapter para Telegram | esboço | 🟢 futuro |
| [`deile-bot/whatsapp/`](deile-bot/whatsapp/) | Provider adapter para WhatsApp Cloud API (Meta) | esboço | 🟢 futuro |
| [`deile-bot/meta/`](deile-bot/meta/) | Provider adapters para Messenger e Instagram Direct (Meta Platform) | esboço | 🟢 futuro |
| [`multi_vibe.md`](multi_vibe.md) | Plano pré-existente — multi-vibe refactor | planejamento | n/a |

## Por que foundation primeiro

O Discord é o primeiro provider, mas **não é o único alvo**. Telegram, WhatsApp e Meta vêm em seguida. Tudo o que for "como o bot conversa, lembra, identifica usuário, decide responder, chama o agente DEILE, audita, mede" deve viver na foundation provider-agnóstica e ser reutilizado por **todos** os adapters. O que sobra para o adapter de cada provider é só o I/O específico daquele canal: gateway WebSocket vs webhook, escaping do markup, slash command vs inline keyboard vs quick reply, rate limit nativo da API daquele provider.

A regra é dura: **se duas implementações do mesmo conceito apareceram em dois adapters diferentes, é bug — sobe para a foundation.**

## Convenções dos planos

Cada subpasta segue o mesmo formato:

```
<plano>/
├── README.md                          # navegação local + estado
├── 00-PLAN.md                          # plano completo: motivação, decisões, escopo, riscos
├── 01-FASE-1-<nome>.md                 # fase 1
├── 02-FASE-2-<nome>.md                 # fase 2
├── ...
├── NN-FASE-E2E.md                      # cobertura E2E de TODAS as features após o plano completo
└── (NN+1)-FASE-REVISAO-CETICA.md       # revisão cética do plano implementado, por outra pessoa
```

Regras:

- **Agrupar features**, evitar fragmentação. Cada fase deve ser uma entrega coerente, mergeável e testável.
- **E2E só ao final** de todas as fases — uma única bateria que prova que a soma das fases entregou o que o plano prometeu.
- **Revisão cética é obrigatória** após a E2E. Outra pessoa lê o plano completo, lê a implementação, lê os testes E2E, e ataca: o que está faltando, o que está superficial, o que pode quebrar em produção, o que economizou onde não devia.
- **Cada fase tem critérios de aceitação verificáveis** (não "implementar X" sem como provar).
- **Prefixar arquivos com número** garante ordem visual no navegador de arquivos.

## Ordem de execução recomendada

```
deile-bot/foundation/  ──►  deile/  ──►  deile-bot/discord/  ──►  (telegram | whatsapp | meta)
        ↑                     ↑                  ↑                            ↑
   bloqueia tudo       bloqueia bridge   primeiro adapter           paralelizáveis depois
```

Foundation entrega contratos e serviços base. As mudanças no DEILE entregam os hooks que o bridge precisa (sessões persistentes, output formatters opcionais, contexto de provider). Discord é o primeiro adapter e o validador da foundation. Os outros providers vêm depois e podem ser tocados em paralelo.
