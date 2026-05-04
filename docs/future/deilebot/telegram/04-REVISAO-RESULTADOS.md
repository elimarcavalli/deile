# Telegram — Revisão Cética: Resultados (M4a)

## Estado

🟢 **PARTIAL — pronto para live test**.

## O que foi entregue

- `deilebot/providers/telegram/{adapter,settings,formatter,normalizer}.py`
- `TELEGRAM_CAPABILITIES`: edit + react + DM + inline_keyboards + slash + typing + profile fetch; max 4096 chars; sem conversation window.
- Adapter usa `python-telegram-bot>=20` Application com polling default; webhook opt-in via `use_webhook=True`. Token via `pydantic.SecretStr`.
- Formatter MarkdownV2 com `escape_markdown_v2` aplicado em todo PLAIN/HEADING/BULLET/etc.
- Normalizer: `chat.type` → `ChannelScope` (private/group/supergroup/channel); reply_to_message → `ReplyContext`; photo/document → `Attachment`.

## O que falta para release de produção

- Cogs/handlers para BotCommands sync (`/start`, `/deile`, `/help`, `/capabilities`, `/forget`).
- Inline keyboard rendering em `OutputFormatter.split` (atualmente PlainText fallback).
- Streaming via `editMessageText` debounced.
- Live E2E em servidor de testes Telegram com 1 chat privado + 1 grupo.

## Auditoria F1-F12

F1 (async I/O) 🟢, F2 (provider_user_id) 🟢, F3 (markup) 🟢, F4 (capability flags) 🟢,
F5 (SQLite) 🟢 (foundation), F6 (foundation não importa providers) 🟢, F7 🟢, F8 🟢,
F9 🟢, F10 🟢, F11 (SecretStr) 🟢, F12 (hot-reload) 🟡 (foundation level).

## Próximos passos

- M4a fase 2 (cogs + inline keyboards) — não-bloqueador para release inicial.
