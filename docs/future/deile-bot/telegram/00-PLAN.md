# 00 — Plano completo: `deile_bot/providers/telegram/`

## 1. Motivação

Telegram tem alcance global, comunidades grandes, e API limpa. Suporta polling (long-poll) e webhooks. O bot DEILE no Telegram é o segundo provider — entrega quase tudo do Discord com 1/3 do esforço por reusar foundation + lições do plano Discord.

## 2. Decisões

| # | Decisão | Motivo |
|---|---|---|
| T1 | Lib: `python-telegram-bot` ≥ 20 (async) | Padrão de fato; bem mantido |
| T2 | Transporte default: long-polling (`Application.run_polling`). Webhook opcional para produção via `Application.run_webhook` no `webhook_server` compartilhado | Long-poll simplifica dev |
| T3 | Identidade: `provider="telegram"`, `provider_user_id=str(update.effective_user.id)` | Foundation pattern |
| T4 | Markup: render via MarkdownV2 com escaping rigoroso (caracteres reservados: `_*[]()~\`>#+-=\|{}.!`) | Evita parse errors |
| T5 | Inline keyboards opt-in (capability `can_inline_keyboards=True`) | Telegram-native UX |
| T6 | BotCommands sincronizados via `Application.bot.set_my_commands(...)` no startup | Auto-complete no app |
| T7 | DM = chat com `chat.type == "private"`. Grupos = `group/supergroup`. Topics em supergroups → `ChannelScope.THREAD` | Mapeamento foundation |
| T8 | Edição de mensagem: `Message.edit_text` (suporte) | Streaming via edit progressivo (debounce maior — Telegram limita 30 edits/s) |
| T9 | Reactions: API recente (Bot API 7.0+); usar `Message.set_reaction` | Capability `can_react=True` |
| T10 | Foto/áudio/vídeo/documento via `send_photo/audio/video/document`; foundation `Attachment.kind` mapeado direto | |

## 3. Capabilities

```python
TELEGRAM_CAPABILITIES = ProviderCapabilities(
    can_edit_message=True,
    can_react=True,
    can_send_dm=True,                  # = enviar mensagem direta para chat privado
    can_threads=True,                  # topics em supergroups
    can_polls=True,                    # send_poll
    can_inline_keyboards=True,
    can_slash_commands=True,           # BotCommands + /commands
    can_voice_messages=True,           # send_voice
    can_send_typing=True,              # send_chat_action(typing)
    can_fetch_user_profile=True,       # get_chat (limitado)
    has_conversation_window=False,
    max_message_chars=4096,
    max_attachments_per_message=10,    # media group
    supported_attachment_kinds=frozenset({IMAGE, VIDEO, AUDIO, FILE, STICKER}),
)
```

## 4. Escopo

In: adapter, normalizer, formatter (MarkdownV2), settings, BotCommands, polling+webhook, inline keyboards opt-in.
Out: Telegram Premium features (reactions custom, etc.), Telegram Stars (pagamentos), Mini Apps, Live Locations.

## 5. Riscos

| Risco | Mitigação |
|---|---|
| MarkdownV2 escaping inconsistente | Testes golden de formatter; whitelist de caracteres; fallback para HTML mode |
| Polling vs webhook em produção (escala) | Documentar trade-off; webhook recomendado para >100 chats |
| Rate limit Telegram (30 msg/s global, 1 msg/s por chat) | RateLimiter da foundation já cobre; ajustar limites |
| Topics em supergroups exigem feature flag no chat | Detecção e degradar para resposta no chat principal |

## 6. Mapa de fases

| Fase | Conteúdo | Esforço |
|---|---|---|
| 01 | Adapter, normalizer, formatter, settings, BotCommands, polling | 3 dias |
| 02 | Webhook server, inline keyboards opt-in, polls, deep linking, edit/streaming | 2 dias |
| E2E | Bateria contra bot Telegram de teste | 1 dia |
| Revisão | Cética | 0.5 dia |

Total: ~7 dias.

## 7. Critérios de "feito"

1. Bot conecta via polling, recebe mensagens em DM e grupo, responde via DEILE.
2. BotCommands aparecem no auto-complete do Telegram.
3. Inline keyboards funcionam (ex.: `/persona escolher` mostra botões).
4. Foundation E2E passa contra adapter Telegram (substitui FakeAdapter).
5. Streaming via edit funciona para respostas longas.
6. Webhook server roda como alternativa documentada.

## 8. Dependências externas

- `python-telegram-bot >= 20`
- Foundation completa
- Hooks DEILE (sessões, extra_system_prompt, streaming)
