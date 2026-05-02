# Fase Revisão Cética — Discord Adapter

> Outra pessoa lê o plano completo, lê o código, executa os ataques, e responde às perguntas. Foco: **a auditoria do `discord_bot/` legado (S1-S8, B1-B11, P1-P6, A1-A12) foi resolvida?**

## Roteiro

### 1. Leitura

| Etapa | Tempo |
|---|---|
| `00-PLAN.md` | 30min |
| Fases 01-04 | 90min |
| `05-FASE-E2E.md` | 30min |
| Diff implementado em `deile_bot/providers/discord/` | 90min |
| Diff implementado em `deile_bot/foundation/tools/` | 30min |

### 2. Checklist contra a auditoria

Marcar 🟢 (resolvido), 🟡 (mitigado parcialmente), 🔴 (continua).

| ID | Auditoria | Esperado |
|---|---|---|
| S1 | Token hardcoded em send_dm.py | 🟢 send_dm.py em `archive/`; tokens revogados |
| S2 | Token hardcoded em salve_tiago.py | 🟢 idem |
| S3 | Privilégio por display_name | 🟢 `PermissionGate.is_owner` usa `bot_user_id`; persona não menciona display_name |
| S4 | Jailbreak `set_modulo_regulador` | 🟢 persona Markdown não tem; system prompt sanitizado |
| S5 | memory.json sem PII filter | 🟡 `purge_*` + `/forget`; PII filter ativo? |
| S6 | Comandos d! salvos na memória | 🟡 nova pipeline persiste TUDO em SQLite; comando admin `/forget` cobre |
| S7 | nuke.py perigoso | 🟢 archived |
| S8 | memory.json 644 | 🟡 verificar `data/deile_bot.sqlite` perms |
| B1 | Inconsistência model id | 🟢 settings centralizados; `forced_model` global |
| B2 | Slash sync ausente | 🟢 `setup_hook` chama `tree.sync()` |
| B3 | d!help inexistente | 🟢 `HelpCog` auto-gerado |
| B4 | Race menção+comando | 🟢 nova pipeline trata mention via intent classifier; comando passa por slash |
| B5 | Race condition memory.json | 🟢 SQLite com WAL; `ConversationStore` atômico |
| B6 | `python -m discord_bot.bot` falso | 🟢 substituído por `python -m deile_bot.cli run --provider discord` |
| B7 | d!dado sem validar lados<=0 | 🟢 cog migrado, validação adicionada (verificar) |
| B8 | Truncamento >2000 chars desabilitado | 🟢 `OutputFormatter.split` cuida |
| B9 | unban quebrado por novo username system | 🟢 aceita user.id ou username; testar |
| B10/B11 | parse .env próprio + DISCORD_TOKEN exigido sempre | 🟢 `BotSettings` via pydantic; sem parse próprio |
| P1/P3 | memory.json monolítico | 🟢 SQLite indexado |
| P4 | Save síncrono no loop | 🟢 `aiosqlite` |
| P5 | Sem rate limit DeepSeek | 🟢 `RateLimiter` da foundation |
| P6 | Sem cool-down por usuário | 🟢 token bucket |
| A1 | System prompt em .py | 🟢 personas em Markdown |
| A2 | Bot isolado de deile/ | 🟢 bridge in-process registra tools, usa router multi-provider |
| A3 | Zero testes | 🟢 cobertura ≥85% (verificar) |
| A4 | requirements sem pin | 🟢 verificar |
| A5 | start.sh engole stderr | 🟢 substituído por `cli.py` com logs estruturados |
| A6 | Sem signal handler | 🟢 `Runtime.stop()` no SIGTERM (verificar handler em `cli.py`) |
| A7-A12 | Reatividade limitada | 🟢 `events_cog` cobre member_join, threads, edits, reactions |

### 3. Adversarial

| # | Ataque | Esperado |
|---|---|---|
| ADV-D1 | Bob muda nick para "elimar.ciss" tenta `/dlq list` | Negado |
| ADV-D2 | Alice manda 50 reactions 🤖 em 30s no mesmo canal | Cool-down corta após 1 |
| ADV-D3 | Alice manda 200 msgs em 1 min em #geral | Rate limit corta; sem CPU spike; sem ghosting |
| ADV-D4 | Mensagem com 50KB de texto | Truncada/rejeitada |
| ADV-D5 | Imagem 100MB anexada | Discord nem aceita; bot não trava |
| ADV-D6 | Forçar adapter.send_message a 500 falhas seguidas | DLQ enche; logs alertam |
| ADV-D7 | Reiniciar bot durante streaming chunks de uma resposta | Reconexão; resposta perdida documentada (não corrompe estado) |
| ADV-D8 | Token Discord inválido no startup | Erro claro, código de saída != 0 |
| ADV-D9 | Sem `DEEPSEEK_API_KEY` mas `provider=discord` | Bot conecta mas responde "agente sem provider configurado" |
| ADV-D10 | Reaction trigger 🤖 a uma mensagem do próprio bot | Ignora (já tratado no listener); audit não registra invocação inválida |
| ADV-D11 | Mensagem com 30 mentions diferentes ao bot | Tratada como 1 invocação (idempotência); não entra em loop |
| ADV-D12 | Tool `send_dm` com `bot_user_id` inexistente | `ToolResult.failure(reason="user_not_found")`; agente recebe e adapta |
| ADV-D13 | Owner executa `/forget --user owner_self` | Pedir confirmação dupla; se ok, executar |
| ADV-D14 | Editar mensagem com 5KB no formatter | Split correto, codeblocks preservados |
| ADV-D15 | `bot.user.id` mudar (recriação do app no Discord) | Identidade self atualizada na próxima `on_ready` |

### 4. Perguntas

1. **Memória persistente do agente vai crescer indefinidamente?** Onde está o teto e o trigger de purga?
2. **Se eu trocar o modelo padrão de DeepSeek para Anthropic, o que muda no bot?** Apenas settings ou precisa código?
3. **Como o bot descobre que está em desenvolvimento vs produção?** Settings, env, both?
4. **Streaming via message.edit pode ser visto como spam pelos usuários?** Há configuração para desligar?
5. **Quando alguém faz `/forget` do próprio histórico, o agente perde o "lembrar de Alice"?** É intencional?
6. **Tokens Discord expostos em git history (mesmo após rotacionar) — devemos rewrite?**
7. **Daily digest: e se o canal estiver vazio nas últimas 24h?** Bot manda nada? Manda "tudo calmo"? Configurável?
8. **`bot_context` carrega `adapter_ref` (objeto vivo). E se uma tool serializar isso pra log?** Risco de leak?

### 5. Saída

`06-REVISAO-RESULTADOS.md` com checklist preenchido, ataques rodados, perguntas respondidas. Bloqueadores 🔴 obrigatórios resolver antes de release.

## Critérios de aceitação

| # | Verificar |
|---|---|
| AC-1 | Checklist auditoria 100% 🟢 ou justificado |
| AC-2 | 15 ataques ADV rodados |
| AC-3 | 8 perguntas respondidas por escrito |
| AC-4 | Sem `🔴` no merge final |

## Estimativa

1 dia revisor + 1 dia réplica.
