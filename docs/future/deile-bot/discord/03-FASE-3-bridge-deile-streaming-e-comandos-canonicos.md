# Fase 3 — Bridge agente DEILE + streaming + comandos canônicos

> Aqui o bot deixa de ser um chat-respondedor e vira a face Discord do agente DEILE: `/deile`, mention, reaction 🤖, sessões persistentes por usuário, streaming progressivo via `message.edit`, `/capabilities` introspectivo.

## Pré-requisitos

- Fases 1 e 2 mergeadas.
- DEILE hooks (plano `deile/`) fases 1, 2, 3 mergeadas. **Bloqueante.**
- Branch: `feat/discord-bridge-and-streaming`.

## Entregáveis

### 3.1. `InProcessAgentBridge` ativado

`build_agent_bridge(foundation_settings)` retorna `InProcessAgentBridge` por default.

`InProcessAgentBridge.invoke(inv)`:

```python
async def invoke(self, inv: AgentInvocation) -> AgentResponse:
    agent = await self._agent()                          # singleton lazy
    session = await agent.get_or_create_session(
        session_id=f"bot_session_{inv.bot_user_id}",
        working_directory=str(self._workdir),
        persisted=True,
    )
    try:
        response: StructuredResponse = await asyncio.wait_for(
            agent.process_input_structured(
                user_input=inv.inbound_text,
                session_id=session.session_id,
                extra_system_prompt=inv.extra_system_prompt,
                bot_context=inv.bot_context,
            ),
            timeout=inv.timeout_seconds,
        )
    except asyncio.TimeoutError as e:
        raise AgentInvocationTimeout(str(e)) from e
    except Exception as e:
        raise AgentInvocationError(str(e)) from e

    return AgentResponse(
        text=response.text,
        markup=response.markup,
        tool_calls=response.tool_calls,
        elapsed_ms=response.elapsed_ms,
        model_used=response.model_used,
        truncated=False,
    )
```

`bot_context` injetado em `IngressPipeline.handle` antes de chamar bridge:

```python
inv = AgentInvocation(
    ...,
    bot_context={
        "provider": "discord",
        "scope": env.channel.scope.value,
        "channel": {"id": env.channel.provider_channel_id, "name": env.channel.name},
        "invoker_user": user,
        "adapter_ref": adapter,
        "permissions": self.permissions,
        "identity": self.identity,
    },
)
```

> O `adapter_ref` é o objeto vivo. `BotTool._adapter()` extrai daí.

### 3.2. Streaming via `message.edit`

`EgressPipeline.send_response_streaming` (variante nova):

```python
async def send_response_streaming(
    self, adapter: DiscordAdapter, env: MessageEnvelope,
    stream: AsyncIterator[StreamChunk], persona: str,
) -> None:
    formatter = self.formatters["discord"]
    placeholder_id = await adapter.send_message(env.channel, "💭 *pensando…*", reply_to=env.message_id)
    buffer = StringBuffer()
    last_edit = monotonic()
    debounce = adapter.settings.message_edit_debounce_ms / 1000

    async for chunk in stream:
        if chunk.kind == "text":
            buffer.append(chunk.payload["text"])
        elif chunk.kind == "tool_call_started":
            buffer.append_meta(f"\n*[chamando: `{chunk.payload['tool_name']}`]*\n")
        elif chunk.kind == "tool_call_finished":
            buffer.append_meta(f"\n*[ok: `{chunk.payload['tool_name']}` em {chunk.payload['elapsed_ms']}ms]*\n")
        elif chunk.kind == "done":
            full_text = formatter.render(chunk.payload["markup"])
            await adapter.edit_message(env.channel, placeholder_id, full_text[:2000])
            for extra in formatter.split(full_text)[1:]:
                await adapter.send_message(env.channel, extra)
            return
        elif chunk.kind == "error":
            await adapter.edit_message(env.channel, placeholder_id, f"❌ erro: {chunk.payload['message']}")
            return

        if monotonic() - last_edit >= debounce:
            await adapter.edit_message(env.channel, placeholder_id, buffer.preview()[:2000])
            last_edit = monotonic()
```

Decisão por canal: se mensagem prevista < 200 chars (heurística) ou stream já entregou `done` antes do debounce, não usa streaming visível — só envia.

### 3.3. Comandos canônicos para invocar o agente

Cog `agent_cog.py`:

```python
class AgentCog(commands.Cog):
    @commands.hybrid_command(name="deile", description="Pergunta ao agente DEILE")
    @app_commands.describe(prompt="Sua pergunta ou instrução")
    async def deile(self, ctx, *, prompt: str):
        """O cog em si NÃO chama o agente — emite um envelope sintético no pipeline."""
        env = self.normalizer.synthetic_envelope_from_command(ctx, prompt)
        # Marca que essa msg DEVE ser respondida (bypass do intent classifier)
        env_with_force = replace(env, raw={**env.raw, "force_respond": True})
        await self.runtime.ingress.handle(env_with_force, self.adapter)
```

`IntentClassifier` heurístico checa `env.raw.get("force_respond")` e devolve `True` direto.

### 3.4. Mention trigger

Já existia (foundation `HeuristicIntentClassifier` cobre mention/reply em DM). Confirmar fluxo: `@bot ajuda` → ingress → bridge → resposta com streaming.

### 3.5. Reaction trigger 🤖

Cog `reaction_cog.py`:

```python
class ReactionCog(commands.Cog):
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != self.adapter.settings.reaction_trigger_emoji: return
        if payload.user_id == self.bot.user.id: return  # ignora própria
        ch = self.bot.get_channel(payload.channel_id)
        target = await ch.fetch_message(payload.message_id)
        # Constrói envelope sintético: o user que reagiu está perguntando "responda a esta mensagem"
        env = self.normalizer.synthetic_envelope_from_reaction(payload, target)
        env_with_force = replace(env, raw={**env.raw, "force_respond": True})
        await self.runtime.ingress.handle(env_with_force, self.adapter)
```

Rate limit: 1 trigger por usuário por canal por 30s (config).

### 3.6. `/capabilities` completo

Cog `capabilities_cog.py`:

```python
@commands.hybrid_command(name="capabilities", description="O que o DEILE consegue fazer aqui")
async def capabilities(self, ctx):
    snap = await self.runtime.capability_catalog.snapshot(self.adapter, self.runtime.agent_meta)
    formatter = self.runtime.formatters["discord"]
    text = self.runtime.capability_catalog.render_for_user(snap, formatter)
    for chunk in formatter.split(text):
        await ctx.send(chunk)
```

Conteúdo: lista cogs/handlers, lista tools (com descrição curta), lista modelos disponíveis, persona ativa neste canal, configurações relevantes (sem expor secrets).

### 3.7. Sessão por usuário

Pipeline já chama `bridge.invoke` com `bot_user_id` — bridge cria sessão `bot_session_<bot_user_id>`. Se o usuário falar em vários canais, vê a mesma memória do agente. Se quiser memória por canal, settings podem alternar para `session_strategy: per_channel | per_user | per_user_channel`.

### 3.8. Tools no DEILE registradas via bridge

`InProcessAgentBridge._agent()` registra as `BotTool`s no `tool_registry` do agente apenas se `bot_context` está sendo usado. Em invocations sem bot, tools não estão visíveis ao agente.

```python
class InProcessAgentBridge:
    async def _agent(self) -> DeileAgent:
        if self._agent_inst is None:
            self._agent_inst = await self._build_agent()
            for tool_cls in BOT_TOOL_CLASSES:
                self._agent_inst.tool_registry.register(tool_cls())
        return self._agent_inst
```

### 3.9. Testes desta fase

| Caso | Cobertura |
|---|---|
| `InProcessAgentBridge.invoke` com mock do agent → AgentResponse correto | Unit |
| `InProcessAgentBridge.invoke` timeout → `AgentInvocationTimeout` | Unit |
| `send_response_streaming` consome stream chunks → editorial debounced no FakeAdapter | Unit |
| `AgentCog.deile` produz envelope sintético; pipeline ingere; resposta sai | Integration |
| `ReactionCog` → reagir 🤖 dispara processamento; reagir outro emoji não dispara | Integration |
| `CapabilitiesCog` → snapshot inclui tools registradas | Integration |
| Sessão `bot_session_<id>` persiste entre 2 invocations consecutivas (memória LLM) | E2E (slow) |
| Smoke real: `/deile diga um fato sobre python` em DM → resposta progressiva visível | Manual |

## Critérios de aceitação

| # | Verificar |
|---|---|
| AC-1 | `/deile`, `@bot`, reaction 🤖 — todos invocam o agente |
| AC-2 | Streaming visível em mensagens longas (manual) |
| AC-3 | `/capabilities` lista tools, modelos, persona |
| AC-4 | Sessão DEILE persiste — usuário lembra entre turnos e entre restarts |
| AC-5 | `BotTool` send_dm: owner consegue, não-owner não |
| AC-6 | Quando bridge falha, fallback "agente indisponível" entregue ao usuário |
| AC-7 | Métricas mostram `bot_agent_invocations_total` incrementando |

## Pontos de atenção

- **Rate limit do Discord**: `message.edit` tem 5/2s por canal. Debounce default 800ms já cabe; se você streamar 10 chunks em 1s, vai estourar. Buffer interno + timer.
- **Streaming de tool_calls**: opcional exibir como meta-text no buffer; se persona pede "silêncio sobre tools", suprimir.
- **`bot_context` em sessão persistente**: pode poluir `session.context_data` se sobrescrito a cada turno; preferir armazenar em local efêmero do agente que limpa após `process_input`. Ajustar no plano DEILE fase 2 se necessário.
- **`force_respond`**: a "saída de emergência" do intent classifier. Documentar bem para não virar atalho usado indevidamente em outros fluxos.

## Estimativa

4 dias. Streaming + edge cases do Discord (rate limit, edits) consome a maior parte.
