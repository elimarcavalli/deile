# Multi-Vibe System — Plano de Refatoração

> **Status:** plano (martelo batido em 2026-05-01; pendente revisão final).
> **Escopo:** refatorar a persona monolítica em arquitetura de **4 camadas** (System / Vibe / User memory / Project memory), introduzir **15 vibes** (estados temperamentais switcháveis), persistência em 2 locais (`<CWD>/.deile/settings.json` e `~/.deile/settings.json`), slash command `/vibe`, e **3 paths fixos de DEILE.md** (system / user / project — sem walk).
> **Não-objetivos:** mudar provedores/roteamento de modelo, mexer no `fallback.md`, alterar tools/registry.

## 0. Changelog do plano

| Rev | Mudança |
|---|---|
| v1 | Proposta inicial: 3 layers (Core + Vibe + Project), walk descendo de CWD com caps. |
| v2 | Walk descendo era ruim (carrega DEILE.md de subpastas irrelevantes). Substituído por **3 paths fixos** estilo `CLAUDE.md`. Layer User adicionada (`~/.deile/DEILE.md`). |
| v3 *(atual)* | Revisão final: stale references corrigidas (referências a `_core.md` que viraram `DEILE.md`, à walk recursivo que sumiu, à `collect_project_deile_md` que não existe mais), §5 unificado em `instruction_loader.load_md_path()`, default vibe consolidado em `api_config.yaml`, PersonaManager esclarecido como camada de compat. |

---

## 1. Estado atual (referência rápida)

- `personas/instructions/developer.md` (~200 linhas) é monolítico: identidade + DoD + ceticismo + path discipline + tools + tom (pitbull-light) — tudo num arquivo só.
- `PersonaManager` carrega 1 persona ativa via YAML (`personas.persona_configs`); switch é por arquivo inteiro.
- `ContextManager._build_system_instruction()` chama `active_persona.build_system_instruction()` → carrega o `.md` da persona.
- Não existe slash command `/persona` ou `/vibe`. Não existe auto-load de markdown da raiz do projeto. Não existe persistência de "humor" entre sessões.
- `fallback.md` é mini-mirror para emergência (PersonaManager falhou) — defesa em profundidade.

---

## 2. Princípios de design

Aplicados às melhores práticas de prompt engineering e agentes:

1. **Hierarquia em 4 camadas:** identidade → temperamento → preferências do usuário → contexto do projeto. Mais específico vem **depois** no prompt (peso maior em conflito factual).
2. **Separação rigorosa de propósito:** invariante NUNCA mora na vibe; temperamento NUNCA mora no Core; preferências subjetivas NUNCA moram no system core. Single Responsibility por arquivo.
3. **Identidade contínua:** DEILE permanece DEILE através de qualquer vibe. O Core estabelece identidade; vibes são lentes de tom.
4. **Soft-mode safety:** vibes "perigosas" (`god`, `shadow`) reafirmam explicitamente que invariantes do Core continuam ativas. Tom não é passe-livre.
5. **Failsafe em duas camadas:** (a) vibe ausente/corrompida → cai pra default (`executor`); (b) system core ausente/corrompido → cai pra `fallback.md`. Nunca roda "instruction-less".
6. **Discoverability:** `/vibe` sem argumento lista todas com descrição curta + marcador da ativa.
7. **Sem cap rígido em V1:** User/Project DEILE.md são single-files manuais; humano raramente escreve 10KB de markdown. V1 não trunca. Se ficar problema empírico, adicionar cap em V1.5.
8. **Estado em 2 escopos:** vibe ativa persiste em `<CWD>/.deile/settings.json` (project, escrita default) e/ou `~/.deile/settings.json` (user-global, escrita só via `/vibe global` em V1.5). Read order: project → user → hardcoded `executor`.
9. **Estrita autonomia do usuário:** override automático de vibe baseado em conteúdo do prompt = NÃO existe. Trocar vibe é decisão consciente do humano.
10. **Defesa em profundidade:** `fallback.md` permanece intocado e independente. Se o sistema novo falhar inteiro, `fallback.md` vira o system prompt sozinho.

---

## 3. Arquitetura — 4 camadas, 3 caminhos fixos de DEILE.md

Espelha o padrão do `CLAUDE.md` do Claude Code: 3 níveis de hierarquia (system / user / project), cada um em um path conhecido. Sem walk, sem discovery dinâmica.

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 1 — SYSTEM (ships com DEILE; SEMPRE ativa)            │
│   Path: deile/personas/instructions/DEILE.md                │
│   Conteúdo: identidade DEILE + DoD + anti-alucinação +      │
│     path discipline (8 regras) + protocolo de erro +        │
│     cascata de deps + tabela de tools + formatação.         │
│   Tom: neutro/profissional. Zero emoji. Zero "modo".        │
│   Tamanho-alvo: ~150 linhas. Versionada com o código.       │
├─────────────────────────────────────────────────────────────┤
│ Layer 2 — VIBE (runtime; exatamente 1 ativa)                │
│   Path: deile/personas/instructions/vibe_<nome>.md          │
│   Conteúdo: SÓ tom, postura ante ambiguidade, verbosidade,  │
│     nível de pushback, estilo de comunicação.               │
│   Tamanho-alvo: 30–60 linhas cada.                          │
│   Default: vibe_executor.md (pitbull-light + ceticismo)     │
│   Total V1: 15 vibes (catálogo na §6).                      │
├─────────────────────────────────────────────────────────────┤
│ Layer 3 — USER (máquina; opcional, único arquivo)           │
│   Path: ~/.deile/DEILE.md                                   │
│   Conteúdo: preferências do dev em QUALQUER projeto desta   │
│     máquina (ex: "responder em PT-BR", "preferir pytest").  │
│   Sem walk. Sem schema. Markdown livre.                     │
├─────────────────────────────────────────────────────────────┤
│ Layer 4 — PROJECT (CWD; opcional, único arquivo)            │
│   Path: <working_directory>/DEILE.md                        │
│   Conteúdo: convenções/gotchas DESTE projeto.               │
│   Sem walk descendo, sem walk subindo. Só o do CWD.         │
│   Sem schema. Markdown livre.                               │
└─────────────────────────────────────────────────────────────┘
```

### 3.1 Ordem no system prompt

```
[ === DEILE — SYSTEM CORE === ]
<deile/personas/instructions/DEILE.md>

[ === DEILE — ACTIVE VIBE: <nome> === ]
<deile/personas/instructions/vibe_<active>.md>

[ === DEILE — USER MEMORY === ]              (omitido se ~/.deile/DEILE.md não existir)
<~/.deile/DEILE.md>

[ === DEILE — PROJECT MEMORY === ]           (omitido se <CWD>/DEILE.md não existir)
<<CWD>/DEILE.md>

[ === DEILE — FILE CONTEXT === ]
<output do _build_file_context()>
```

Delimitadores `[ === ... === ]` ajudam o LLM a parsear estrutura.

### 3.2 Por que essa ordem?

- **System primeiro:** identidade + invariantes. O LLM lê QUEM ele é antes de COMO agir.
- **Vibe segundo:** temperamento modifica execução, mas nunca contraria identidade.
- **User terceiro:** preferências da máquina (PT-BR, pytest, etc.) — overrides defaults gerais.
- **Project quarto:** convenções desta árvore — overrides user em conflito factual (mais específico vence).
- **File context separado:** dados frescos, não instruções.

### 3.2.1 Conflitos esperados (e quem ganha)

| Conflito | Quem ganha | Razão |
|---|---|---|
| Core diz X, vibe diz não-X | **Core** | Invariantes não são negociáveis; vibe que contradiz é bug do autor da vibe. Teste verifica isso (§13.1). |
| User diz "responder em PT-BR", project diz "responder em EN" | **Project** | Mais específico (mais tarde no prompt). |
| Vibe pitbull tem tom agressivo, user diz "be calm" | **Vibe** | Vibe é runtime user choice — sobrescreve user-global. Contraditório? Não: o usuário escolheu pitbull *agora*; user-global é default; trocar vibe é override consciente. |
| Project DEILE.md tenta forçar tom oposto à vibe ativa | **Vibe** *(V1)* | Project memory é pra **fatos/regras objetivas** ("use pytest", "branch develop"), não pra tom. Se o project quiser forçar tom, deve forçar vibe via setting (V2). |

> ⚠️ **Convenção forte:** User/Project DEILE.md são pra **fatos**, vibes são pra **tom**. Misturar quebra precedência. Documentar nos templates de DEILE.md.

### 3.3 Por que NÃO há walk

Walk descendo de CWD carrega DEILE.md de pastas potencialmente irrelevantes (ex: `tests/fixtures/DEILE.md` aparecendo em todo prompt mesmo trabalhando em `frontend/`). Walk subindo de CWD funciona pra repos onde DEILE roda em subpastas, mas adiciona complexidade que o V1 não precisa.

**Convenção V1:** UM `DEILE.md` por nível, em path conhecido. Se o usuário precisa de regras por subpasta, escreve no DEILE.md do projeto na forma `## Regras para frontend/` etc. Simplicidade > flexibilidade prematura.

Pós-V1 pode-se adicionar walk subindo (estilo CLAUDE.md project memory) se ficar claro que vale a pena.

### 3.4 Relação com `PersonaManager` (deprecation suave)

`PersonaManager` permanece no código por compatibilidade com referências em `export_command.py` e `context_command.py`, mas **não é mais o caminho oficial** de assembly do system instruction.

- ContextManager bypassa PersonaManager: lê vibe direto de `vibe_state` + `vibe_loader`.
- PersonaManager continua existindo, com `_load_available_personas()` adaptado pra descobrir vibes via filesystem (§12.2). Funções como `switch_persona()` continuam funcionando (delegam pra `vibe_state.set_active_vibe`).
- `get_active_persona()` retorna stub que carrega a vibe ativa — preserva contratos.
- Em V2, considerar deprecar PersonaManager inteiro e expor `vibe_state`/`vibe_loader` como API canônica.

---

## 4. Formato dos arquivos

### 4.1 `deile/personas/instructions/DEILE.md` (Layer 1 — System Core)

Markdown puro. Sem frontmatter (é único, especial). Conteúdo extraído da `developer.md` atual, removendo tom/estilo.

**Inclui:**
- Identidade ("Você é DEILE…") em tom neutro
- DoD (tabela atual)
- Regra anti-alucinação
- Protocolo de erro / cascata de diagnóstico
- Fidelidade ao escopo
- Protocolo de dependências
- Path discipline (regras 1–8 atuais)
- Padrões anti-erro
- Tabela de tools (quando usar cada uma)
- Loop de execução padrão
- Formatação obrigatória de tool outputs
- Identidade quando perguntado

**Não inclui:**
- "Modo pitbull", "modo cético sênior — 5 portões"
- Emojis recreativos
- Estilo de comunicação ("descontraído quando informal" etc.)

### 4.2 `vibe_<nome>.md`

YAML frontmatter (metadados pra UX) + corpo (instruções de tom).

```markdown
---
display_name: Pitbull
description: Implacável. Zero hesitação, zero "posso?". Pede→faz→entrega.
emoji: 🔥
tier_safety: standard   # "standard" | "soft" (apenas god e shadow são soft)
---

## Tom: implacável

[corpo livre — instruções de comportamento, exemplos, anti-padrões…]
```

**Regras para o corpo:**
- ❌ Não duplicar conteúdo do Core (ex: "lembre-se do DoD" — Core já diz).
- ❌ Não contradizer Core (ex: "ignore validação por velocidade" — proibido).
- ✅ Instruir tom, postura, verbosidade, nível de pushback.
- ✅ Para vibes soft (`god`, `shadow`): incluir 1 parágrafo reafirmando que invariantes do Core continuam ativas, mesmo neste modo.

> **Default vibe não mora no frontmatter.** O default é uma decisão de configuração (`default_vibe: executor` em `deile/config/api_config.yaml`) — fonte única de verdade. Frontmatter da vibe descreve só a vibe em si, sem "elegibilidade pra default".

**Frontmatter parsing:** parsing manual (~30 linhas, sem dep nova). Não introduz `python-frontmatter` no `requirements.txt`.

**Edge case — vibe MD vazia ou sem frontmatter:** `load_vibe_metadata()` retorna `None`; vibe não aparece no `/vibe`. `load_vibe_content()` retorna `None`; assembly cai pra `executor`. Nenhum crash.

### 4.3 `~/.deile/DEILE.md` (Layer 3 — User Memory) e `<CWD>/DEILE.md` (Layer 4 — Project Memory)

Markdown livre. Sem frontmatter exigido. Sem schema. Mesmo formato nas duas camadas — diferem só pelo escopo.

**User memory** (`~/.deile/DEILE.md`) — vale em todos os projetos. Exemplo:
```markdown
# Preferências do dev

- Responda em PT-BR.
- Prefira pytest a unittest.
- Use type hints sempre.
- Não rode comandos destrutivos sem confirmar.
```

**Project memory** (`<CWD>/DEILE.md`) — vale só neste projeto. Exemplo:
```markdown
# Projeto X — Notas pro DEILE

## Convenções
- Branch padrão: `develop`, não `main`.
- Use `npm run lint` antes de commit.

## Gotchas
- Migrations são responsabilidade do operador (não rode `alembic upgrade`).
- Subpasta `frontend/`: convenções específicas — usar React Hooks, não classes.
```

Em conflito factual, **project vence user** (mais específico). Isso é convenção — o LLM lê os dois e segue o último.

---

## 5. Algoritmo de assembly do system instruction

Toda leitura passa pelo `instruction_loader.load_md_path(path)` (novo método unificado, mtime cache). 4 layers fixas, nenhuma walk.

```python
SYSTEM_CORE_PATH = Path(__file__).parent.parent / "personas" / "instructions" / "DEILE.md"
USER_MEMORY_PATH = Path.home() / ".deile" / "DEILE.md"


async def _build_system_instruction(parse_result, session, **kwargs) -> str:
    cwd = (session.working_directory if session and hasattr(session, 'working_directory')
           else kwargs.get('working_directory', os.getcwd()))

    parts = []

    # Layer 1 — System Core (sempre)
    system_core = self.instruction_loader.load_md_path(SYSTEM_CORE_PATH)
    if not system_core:
        # Defesa em profundidade: DEILE.md (system) ausente/corrompido → fallback.md
        return await self._build_fallback_system_instruction(session, **kwargs)
    parts.append(_with_delimiter("DEILE — SYSTEM CORE", system_core))

    # Layer 2 — Vibe ativa (sempre 1)
    active_vibe = vibe_state.get_active_vibe(cwd)
    vibe_content = (
        vibe_loader.load_vibe_content(active_vibe)
        or vibe_loader.load_vibe_content("executor")
    )
    if vibe_content:
        parts.append(_with_delimiter(f"DEILE — ACTIVE VIBE: {active_vibe}", vibe_content))
    # Se nem 'executor' carregar (filesystem corrompido), seguimos sem vibe.
    # System core sozinho ainda dá direção; defesa em profundidade.

    # Layer 3 — User Memory (~/.deile/DEILE.md, opcional)
    user_md = self.instruction_loader.load_md_path(USER_MEMORY_PATH)
    if user_md:
        parts.append(_with_delimiter("DEILE — USER MEMORY", user_md))

    # Layer 4 — Project Memory (<CWD>/DEILE.md, opcional)
    project_md = self.instruction_loader.load_md_path(Path(cwd) / "DEILE.md")
    if project_md:
        parts.append(_with_delimiter("DEILE — PROJECT MEMORY", project_md))

    # File context (já existe — dados frescos, não instruções)
    file_ctx = await self._build_file_context(session, **kwargs)
    if file_ctx:
        parts.append(_with_delimiter("DEILE — FILE CONTEXT", file_ctx))

    return "\n\n".join(parts)
```

### 5.1 `instruction_loader.load_md_path(path: Path) -> str | None`

Método novo no `instruction_loader` (extensão do existente `load_instruction(name)`).

```python
def load_md_path(self, absolute_path: Path) -> Optional[str]:
    """Carrega markdown de path absoluto com mtime-based cache.

    Returns None se path não existe, não é arquivo, ou erro de I/O.
    """
    try:
        if not absolute_path.is_file():
            return None
        mtime = absolute_path.stat().st_mtime
        key = str(absolute_path)
        if self._cache.get(key) and self._file_mtimes.get(key) == mtime:
            return self._cache[key]
        content = absolute_path.read_text(encoding="utf-8").strip()
        self._cache[key] = content
        self._file_mtimes[key] = mtime
        return content
    except OSError:
        return None
```

Substitui `_load_optional_md` standalone — caching uniforme + reuso do mecanismo existente.

### 5.2 Cache — comportamento por arquivo

| Arquivo | Cache key | Invalidação |
|---|---|---|
| `personas/instructions/DEILE.md` | absolute path | mtime change |
| `personas/instructions/vibe_*.md` | absolute path | mtime change OR `/vibe` switch (refresh) |
| `~/.deile/DEILE.md` | absolute path | mtime change |
| `<CWD>/DEILE.md` | absolute path | mtime change |
| `.deile/settings.json` | (não usado) | lido a cada startup; não cacheado em runtime |

I/O é negligível ante latência LLM — sem otimização prematura.

### 5.3 Defesa em profundidade — escada de degradação

```
1. Tudo OK → assembly de 4 layers (+ file context).
2. System core (DEILE.md) ausente/corrompido → cai pra fallback.md.
3. Vibe ativa não carrega → tenta 'executor'.
4. 'executor' também falha → assembly só com System core (sem vibe).
5. fallback.md também não carrega → instruction_loader retorna emergency hardcoded string.
```

DEILE nunca roda sem alguma instrução. Cada degrau é silencioso (warning log) — usuário não vê crash.

---

## 6. Catálogo de Vibes V1 (15 modos)

| Filename | Display | Descrição | Tier | Default |
|---|---|---|---|---|
| `vibe_executor.md` | Executor | Pitbull-light. Vai e faz, mas passa pelos 5 portões do cético em tarefas ambíguas/arquiteturais. Tom firme sem ser agressivo. | standard | **✓** |
| `vibe_pitbull.md` | Pitbull | Implacável, zero hesitação, máxima urgência. Pula portões do cético — pede→faz→entrega, e ponto. | standard | |
| `vibe_cetico.md` | Cético | Trava antes do código. Esgota perguntas, alternativas, edge cases antes de mover um dedo. Pushback alto. | standard | |
| `vibe_arquiteto.md` | Arquiteto | Long-term. SOLID, escala, trade-offs, "como isso fica em 2 anos?". Toda decisão tem rationale documentado. | standard | |
| `vibe_devil.md` | Devil | Advogado do diabo. Destrói toda ideia com contra-argumentos, edge cases, riscos de segurança/escala. | standard | |
| `vibe_steelman.md` | Steelman | Reforça TODA ideia do usuário com o melhor argumento possível. Sem destruir cedo. | standard | |
| `vibe_god.md` | God *(soft)* | Brainstorming criativo, certeza inabalável, finge onisciência. **Mantém DoD/anti-alucinação intactos.** | soft | |
| `vibe_proativo.md` | Proativo | Vigilante. Avisa de TODO esquecido, CI quebrado, requirements desatualizado, migration faltando. | standard | |
| `vibe_passivo.md` | Passivo | Só obedece. Zero iniciativa, zero "já que tô aqui também...". Silencioso até chamar. | standard | |
| `vibe_zen.md` | Zen | Calmo, didático, paciente. Antídoto ao pitbull. Explica como pra um Jr., sem pressa. | standard | |
| `vibe_bomhumor.md` | Bom Humor | Piadas, emojis, trocadilhos. Alivia tasks longas. | standard | |
| `vibe_prestativo.md` | Prestativo | Concierge 5 estrelas. Oferece alternativas, mostra caminhos, faz check-in. | standard | |
| `vibe_hacker.md` | Hacker | Rápido, sujo, funcional. MVP em minutos. **DoD continua exigindo exit 0**, mas sem polish. | standard | |
| `vibe_monitor.md` | Monitor | Não escreve, audita. Logs, métricas, anti-padrões, dívida técnica, vulnerabilidades. | standard | |
| `vibe_shadow.md` | Shadow *(soft)* | Autonomia máxima. Encadeia tarefas sem check-in. **Mantém DoD/anti-alucinação intactos** e reporta milestones. | soft | |

**Tiers:**
- `standard`: sem requisitos extras de safety.
- `soft`: precisa incluir parágrafo explícito reafirmando que invariantes do Core seguem ativas (DoD, anti-alucinação, path discipline).

---

## 7. Persistência (`.deile/settings.json`)

### 7.1 Localização — duas camadas

DEILE persiste vibe em **dois locais**, lidos em cascata (project sobrescreve user):

| Camada | Path | Propósito |
|---|---|---|
| **User-global** | `~/.deile/settings.json` | Default do usuário pra qualquer projeto novo. |
| **Project** | `<working_directory>/.deile/settings.json` | Override por projeto. Tem precedência sobre user-global. |

**Read order (na startup):**
1. Tenta carregar `<CWD>/.deile/settings.json`. Se válido, usa.
2. Senão, tenta `~/.deile/settings.json`. Se válido, usa.
3. Senão, default hardcoded: `executor`.

**Write order (no `/vibe <nome>`):**
- Default: escreve em `<CWD>/.deile/settings.json` (project) — comportamento esperado quando o usuário troca vibe na sessão.
- Para tocar global: `/vibe global <nome>` (V1.5; documentado no roadmap, fora do MVP).

> Recomendação: adicionar `.deile/` ao `.gitignore` do projeto. User-global em `~/.deile/` nunca entra em repo.

### 7.2 Schema

Idêntico nas duas camadas:

```json
{
  "version": 1,
  "active_vibe": "executor",
  "updated_at": "2026-05-01T14:32:08Z"
}
```

### 7.3 Operações

| Evento | Ação |
|---|---|
| Startup | Lê project → user-global → default. Não cria arquivo ainda. |
| `/vibe <nome>` | Valida nome → atualiza estado em memória → escreve em `<CWD>/.deile/settings.json` (cria diretório se preciso). |
| `/vibe reset` | active_vibe = `executor`, escreve em project. |
| `/vibe global <nome>` (V1.5) | Escreve em `~/.deile/settings.json`. Mensagem clara distinguindo "global vs project". |
| Arquivo corrompido (qualquer camada) | Log warning, ignora aquela camada, prossegue na cascata. NÃO sobrescreve (preserva estado humano pra debug). |

### 7.4 Implementação

Novo módulo: `deile/config/vibe_state.py`. Funções puras (não singleton):

```python
def get_active_vibe(cwd: Path | str) -> str:
    """Read project → user-global → default."""

def set_active_vibe(cwd: Path | str, vibe_id: str, *, scope: str = "project") -> None:
    """scope='project' (default) writes to <cwd>/.deile/. scope='user' writes to ~/.deile/."""

def reset(cwd: Path | str, *, scope: str = "project") -> None:
    """Equivalent to set_active_vibe(cwd, 'executor', scope=scope)."""

def read_state(path: Path) -> dict | None:
    """Load + validate one settings.json. Returns None se ausente/corrupto."""
```

Escrita atômica (write-temp + `os.replace`) pra evitar corrupção em concorrência.

---

## 8. Slash command `/vibe` — UX completa

### 8.1 Forma

```
/vibe                  # lista todas + marcador da ativa
/vibe <nome>           # troca + persiste em <CWD>/.deile/settings.json
/vibe reset            # volta pra default (executor) e persiste
/vibe info <nome>      # descrição detalhada de uma vibe (V1, trivial)
```

V1.5 adiciona `/vibe global <nome>` (escreve em `~/.deile/settings.json` em vez de project).

### 8.2 Output esperado

**`/vibe`:**
```
🎭 Vibes disponíveis (ativa marcada com ●):

● executor   Pitbull-light + ceticismo. Default.
  pitbull    🔥 Implacável, zero hesitação.
  cetico     Trava antes do código.
  arquiteto  Long-term, SOLID, escala.
  devil      Advogado do diabo.
  steelman   Reforça toda ideia.
  god        🌟 [soft] Brainstorming criativo.
  proativo   Vigilante (CI, deps, TODOs).
  passivo    Só obedece, zero iniciativa.
  zen        🧘 Calmo, didático, paciente.
  bomhumor   😄 Piadas, emojis, trocadilhos.
  prestativo Concierge 5 estrelas.
  hacker     ⚡ Rápido, sujo, funcional.
  monitor    🔍 Não escreve, audita.
  shadow     👤 [soft] Autonomia máxima.

Use /vibe <nome> pra trocar.
```

**`/vibe pitbull`:**
```
🎭 Vibe trocada: executor → pitbull 🔥
   Implacável, zero hesitação. Persistido em .deile/settings.json.
```

**`/vibe foo`:**
```
❌ Vibe 'foo' não existe. Use /vibe pra listar.
```

**`/vibe info pitbull`:**
```
🎭 pitbull (Pitbull) 🔥
Tier: standard
Implacável. Zero hesitação, zero "posso?". Pede→faz→entrega.

Pula os 5 portões do cético — sai escrevendo o primeiro chute.
Mantém DoD/anti-alucinação/path discipline (Core invariants intactos).
Tom: máxima urgência, emojis 😤🔥 inclusos, "manda missão!".
```

(carrega frontmatter + primeiros parágrafos do corpo da vibe MD)

### 8.3 Implementação

Novo arquivo: `deile/commands/builtin/vibe_command.py`. Estrutura igual aos outros (`SlashCommand` subclass). Acessa `vibe_state` (read/write) e `vibe_loader` (lista).

### 8.4 Discovery

Vibes são **discovered from filesystem**, não de YAML config:

```python
def list_available_vibes() -> List[VibeInfo]:
    instructions_dir = Path(...)/"personas"/"instructions"
    return [
        load_vibe_metadata(p)
        for p in instructions_dir.glob("vibe_*.md")
    ]
```

Cada arquivo `vibe_*.md` automaticamente vira uma vibe disponível. Adicionar uma vibe nova = drop um arquivo. Sem registry boilerplate.

---

## 9. Welcome screen

Atualizar `deile/ui/console_ui.py` (já modificado em git status — coordenar).

### 9.1 Antes
```
║ Provider  DeepSeek                  ║
║ Model     deepseek-v4-pro           ║
```

### 9.2 Depois
```
║ Provider  DeepSeek                  ║
║ Model     deepseek-v4-pro           ║
║ Vibe      🔥 pitbull (implacável)   ║
```

Mostra: emoji + nome + 1ª frase da description (do frontmatter).

---

## 10. Hot-reload e cache

Centralizado em `instruction_loader.load_md_path(path)` — mesma estratégia mtime pra todos os arquivos.

| Arquivo | Reload trigger | Notas |
|---|---|---|
| `personas/instructions/DEILE.md` | mtime change | System core; cache por absolute path. |
| `personas/instructions/vibe_*.md` | mtime change OR `/vibe` switch (invalida cache da vibe trocada) | Cache por absolute path. |
| `~/.deile/DEILE.md` | mtime change | Cheap re-read; cacheado entre rebuilds. |
| `<CWD>/DEILE.md` | mtime change | Cheap re-read; cacheado entre rebuilds. |
| `<CWD>/.deile/settings.json` | lido a cada startup; releitura em `/vibe` switch (sanity check) | Não cacheado em runtime. |
| `~/.deile/settings.json` | mesmo | Não cacheado em runtime. |

Razão: prompt files são pequenos (KBs), custo de I/O é negligível ante latência do LLM (segundos). Otimização prematura adiciona bug.

**Cross-process safety:** se o usuário editar `<CWD>/DEILE.md` em outro editor, o próximo `_build_system_instruction()` pega via mtime check. Sem polling, sem watcher.

---

## 11. Lista de mudanças no filesystem

### 11.1 Criar

```
deile/personas/instructions/DEILE.md                 # system core (extraído do developer.md atual)
deile/personas/instructions/vibe_executor.md         # default — pitbull-light + ceticismo
deile/personas/instructions/vibe_pitbull.md
deile/personas/instructions/vibe_cetico.md
deile/personas/instructions/vibe_arquiteto.md
deile/personas/instructions/vibe_devil.md
deile/personas/instructions/vibe_steelman.md
deile/personas/instructions/vibe_god.md              # soft
deile/personas/instructions/vibe_proativo.md
deile/personas/instructions/vibe_passivo.md
deile/personas/instructions/vibe_zen.md
deile/personas/instructions/vibe_bomhumor.md
deile/personas/instructions/vibe_prestativo.md
deile/personas/instructions/vibe_hacker.md
deile/personas/instructions/vibe_monitor.md
deile/personas/instructions/vibe_shadow.md           # soft

deile/config/vibe_state.py                           # read/write .deile/settings.json
deile/personas/vibe_loader.py                        # discovery + frontmatter parser
deile/commands/builtin/vibe_command.py               # /vibe slash command

deile/tests/test_vibe_state.py                       # persist round-trip, corrupt JSON, etc.
deile/tests/test_vibe_loader.py                      # discovery, frontmatter, missing
deile/tests/test_layered_assembly.py                 # ordem, delimitadores, layers opcionais
deile/tests/test_vibe_command.py                     # /vibe, /vibe foo, /vibe reset
```

### 11.2 Alterar

```
deile/core/context_manager.py                        # _build_system_instruction → 4 layers
deile/personas/manager.py                            # discovery filesystem-driven via vibe_loader
deile/personas/instruction_loader.py                 # adicionar load_md_path(absolute_path)
deile/ui/console_ui.py                               # welcome mostra vibe ativa
deile/config/api_config.yaml                         # adicionar default_vibe: executor
.gitignore                                           # adicionar .deile/ (com nota explicativa)
```

**`api_config.yaml` — entrada nova:**
```yaml
# Vibe padrão quando .deile/settings.json não existe (project nem user).
default_vibe: executor
```

`vibe_state.get_active_vibe(cwd)` lê esta chave como fallback final, antes do hardcoded `"executor"`.

### 11.3 Remover

```
deile/personas/instructions/developer.md             # substituído por DEILE.md + vibe_executor.md
```

### 11.4 NÃO tocar

```
deile/personas/instructions/fallback.md              # defesa em profundidade — fica intocado
```

---

## 12. Lista de mudanças no código

### 12.1 `deile/core/context_manager.py`

Refatorar `_build_system_instruction` para o assembly de 4 layers descrito em §5. Toda leitura via `self.instruction_loader.load_md_path(absolute_path)` — caching uniforme. Ver §5 (algoritmo) e §5.1 (`load_md_path`) para o código completo.

Não há mais walk recursivo. 3 paths fixos + vibe ativa.

### 12.2 `deile/personas/manager.py`

Mudar `_load_available_personas`:
- Antes: lia `personas.persona_configs` do YAML.
- Depois: descobre `vibe_*.md` no filesystem via `vibe_loader.list_available_vibes()`.
- Mantém compatibilidade: cada vibe vira um `BasePersona` minimal (`_create_minimal_persona`).
- `default_persona_id` → `vibe_state.get_active_vibe(cwd)` ou `"executor"` se ausente.

### 12.3 `deile/personas/vibe_loader.py` (novo)

```python
@dataclass(frozen=True)
class VibeInfo:
    id: str               # "pitbull"
    display_name: str     # "Pitbull"
    description: str      # "Implacável..."
    emoji: str            # "🔥"
    tier_safety: str      # "standard" | "soft"
    is_default: bool

def list_available_vibes() -> list[VibeInfo]: ...
def load_vibe_content(vibe_id: str) -> str | None: ...
def load_vibe_metadata(vibe_id: str) -> VibeInfo | None: ...
```

Frontmatter parsing: `python-frontmatter` ou parsing manual (pra evitar dep nova). Manual é uns 30 linhas.

**Sem `collect_project_deile_md`** — não há walk. User/Project memory são lidos diretamente de paths fixos (`~/.deile/DEILE.md`, `<CWD>/DEILE.md`) via `instruction_loader.load_md_path()` no `context_manager`.

### 12.4 `deile/config/vibe_state.py` (novo)

Assinaturas alinhadas com §7.4 — `scope` parameter pra V1.5.

```python
SETTINGS_REL = Path(".deile") / "settings.json"

def get_active_vibe(cwd: Path | str) -> str:
    """Read project → user-global → api_config.default_vibe → 'executor'.

    Cascata silenciosa: corrupção em qualquer camada loga warning e segue.
    """

def set_active_vibe(cwd: Path | str, vibe_id: str, *, scope: str = "project") -> None:
    """Persist active vibe.

    scope='project' (default) → <cwd>/.deile/settings.json
    scope='user' (V1.5) → ~/.deile/settings.json

    Em V1, scope='user' levanta NotImplementedError ao ser chamado.
    """

def reset(cwd: Path | str, *, scope: str = "project") -> None:
    """Equivalent to set_active_vibe(cwd, 'executor', scope=scope)."""

def read_state(path: Path) -> dict | None:
    """Load + validate one settings.json. Returns None se ausente/corrupto."""
```

**Atomic write:** usa `tempfile.NamedTemporaryFile(dir=settings_dir)` + `os.replace(tmp, final)` pra evitar corrupção em concorrência ou crash mid-write.

### 12.5 `deile/commands/builtin/vibe_command.py` (novo)

`SlashCommand` subclass. **Sem alias `mood`** — evita ambiguidade de naming. `/vibe` é o comando único oficial.

Métodos privados:
- `_render_list()` — formata tabela com marcador ● (output em §8.2)
- `_switch(name)` — valida nome, chama `vibe_state.set_active_vibe`, retorna mensagem
- `_reset()` — wrapper de `_switch("executor")`
- `_render_info(name)` — frontmatter + primeiros parágrafos do corpo (output em §8.2)

### 12.6 `deile/ui/console_ui.py`

Welcome banner adiciona linha:
```python
active_vibe = vibe_state.get_active_vibe(cwd)
vibe_meta = vibe_loader.load_vibe_metadata(active_vibe)
emoji = vibe_meta.emoji if vibe_meta else ""
display = vibe_meta.display_name if vibe_meta else active_vibe
short_desc = vibe_meta.description.split(".")[0] if vibe_meta else ""
```

Renderiza: `║ Vibe      {emoji} {display} ({short_desc})  ║`

---

## 13. Estratégia de testes

### 13.1 Unitários (sem API)

- **`test_vibe_state.py`** (~10 testes):
  - round-trip set/get
  - read cascata: project → user → api_config → 'executor'
  - corrupt project settings (silencioso, fallback pra user)
  - corrupt user settings (silencioso, fallback pra api_config)
  - missing tudo → 'executor'
  - atomic write (mock filesystem, mata processo no meio do write, verifica não corrompe)
  - `set_active_vibe(scope='user')` em V1 levanta `NotImplementedError`
  - `reset()` volta pra default
  - cria `.deile/` se não existe
  - permission error em escrita (loga, não levanta)

- **`test_vibe_loader.py`** (~10 testes):
  - `list_available_vibes()` com filesystem temporário (3 vibes mockadas)
  - frontmatter válido → metadata correta
  - frontmatter inválido (YAML quebrado) → vibe não aparece + warning
  - frontmatter ausente → vibe não aparece
  - `load_vibe_content("missing")` → None
  - `load_vibe_content("vibe_with_empty_body")` → "" ou None (decidir)
  - tier_safety detection (standard vs soft)
  - emoji unicode preservation
  - cache mtime invalidation

- **`test_layered_assembly.py`** (~12 testes):
  - 4 layers presentes → ordem correta dos delimitadores
  - vibe inválida → cai pra executor (com log)
  - executor também ausente → assembly só com System core
  - System core ausente → cai pra `_build_fallback_system_instruction`
  - User memory ausente → omitido sem warning
  - Project memory ausente → omitido sem warning
  - User+Project ambos presentes → user antes de project
  - File context append no final
  - delimitadores com `[ === ... === ]` literal
  - vibe name vaza no delimiter ("ACTIVE VIBE: pitbull")

- **`test_vibe_command.py`** (~7 testes):
  - `/vibe` lista todas + marcador ● na ativa
  - `/vibe pitbull` troca + persiste em project
  - `/vibe foo` → erro com lista de válidas
  - `/vibe reset` → executor + persiste
  - `/vibe info pitbull` → frontmatter + corpo
  - `/vibe info foo` → erro
  - `/vibe global pitbull` em V1 → mensagem "ainda não implementado em V1"

### 13.2 Smoke (sem API)

- **`test_developer_md_removed.py`** — `assert not Path("deile/personas/instructions/developer.md").exists()` (regression guard).
- **`test_system_core_invariants_present.py`** — confirma `personas/instructions/DEILE.md` contém marcadores literais: "DEFINITION OF DONE", "anti-alucinação", "Path discipline".
- **`test_soft_vibes_safety_paragraph.py`** — para `vibe_god.md` e `vibe_shadow.md`, confirma presença de palavras-chave: "DoD" / "Core" / "invariantes" / "anti-alucinação". Garante que o tier `soft` reafirma safety.
- **`test_vibe_md_no_core_contradiction.py`** — para cada `vibe_*.md`, grep palavras vermelhas: "ignore validação", "pula DoD", "sem testar". Falha se encontrar.

### 13.3 Empíricos (com API, em `deile/tests/might/multi-vibe/`)

Run manual, custos reais. Provider: **deepseek-v4-flash** (cheapest, ~$0.005/turno).

- 1 turno por vibe representativa (pitbull, cetico, zen, monitor) confirmando que tom muda visivelmente entre elas com mesmo prompt.

### 13.4 Cobertura

Suite atual: 532 verdes. Adicionar ~43 testes novos (10+10+12+7+4 = 43). Manter `--cov-fail-under=80`.

---

## 14. Riscos e mitigações

| Risco | Mitigação |
|---|---|
| Vibe MD diverge do Core (contradição) | Documentar regra em §4.2; adicionar test que faz grep de palavras-chave do Core (DoD, anti-alucinação) e falha se vibe contradiz. |
| `god`/`shadow` viram safety bypass | Tier `soft` exige parágrafo reafirmando invariantes; teste verifica presença desse parágrafo. |
| User/Project DEILE.md infla prompt | Sem walk → no máx 2 arquivos extras. Cap soft (~5 KB cada) com aviso de truncate; em V1 sem cap rígido (markdown manual do humano raramente é grande). |
| Walk recursivo lento em mounts esquisitos | **Não há walk.** 3 paths fixos. Não aplicável. |
| `.deile/settings.json` corrompido por concorrência | Escrita atômica (write-temp + rename). |
| 15 vibes pra manter | Cada vibe é 30–60 linhas; baixo overhead. Mas atenção: não inflar V1 com vibes que ninguém usa. Monitorar uso pós-launch. |
| LLMs fracos (gemini-2.5-flash-lite) ignoram vibes | Já documentado em conversa anterior. Fora de escopo deste plano; será tratado em refator separado da cascade. |
| Migração quebra sessões em curso | Plan irrelevant: não há sessões persistidas entre versões. Cold start. |

---

## 15. Roadmap pós-V1

- **V1.5: `/vibe global <nome>`** — escreve em `~/.deile/settings.json` em vez de project. UX clara distinguindo escopo. *(Leitura já está suportada no V1; só falta o write path.)*
- **Walk subindo (project memory recursive)** estilo CLAUDE.md — caso DEILE rode em subpasta de um repo, walk UP coletando todos os `DEILE.md` até o root do projeto. V1 só lê o do CWD; suficiente pra início.
- **Subtree memory lazy** — quando agente lê arquivo em subpasta, carrega o `DEILE.md` daquela subpasta on-demand. Espelha Claude Code subtree memory.
- Auto-sugestão de vibe pelo LLM ("isso parece tarefa pra `cetico`, quer trocar?"). Requer telemetria + safeguards.
- Vibes compostas (`/vibe stack pitbull+monitor`) — execução pitbull com lente de auditoria. Complexo, V2+.
- Vibe expressando pricing/tier preference (ex: hacker força tier_3 cheap models). Mistura concerns; analisar.
- `/vibe diff <a> <b>` — mostra diferença textual entre duas vibes.
- Telemetria de uso por vibe (qual a mais usada, padrões de switch).

---

## 16. Decisões assumidas (confirme antes da implementação)

### 16.1 Confirmadas pelo usuário

- **Default vibe:** `executor`.
- **Granularidade V1:** todas as 15 vibes.
- **Camadas DEILE.md em 3 paths fixos:** system (`deile/personas/instructions/DEILE.md`), user (`~/.deile/DEILE.md`), project (`<CWD>/DEILE.md`). **Sem walk** descendo nem subindo. *(Espelha o padrão `CLAUDE.md`.)*
- **Persistência em 2 camadas:** `<CWD>/.deile/settings.json` (project, default de escrita) + `~/.deile/settings.json` (user global, fallback de leitura).
- **Override automático:** estrito; vibe só muda por comando do humano.
- **`god` e `shadow`:** soft — mantêm Core ativo.
- **Welcome screen:** mostra vibe ativa.

### 16.2 Recomendações minhas (ainda passíveis de ajuste)

1. **Frontmatter YAML em `vibe_*.md`** pra UX bonita do `/vibe`. *(Sem isso, listagem fica seca.)*
2. **`fallback.md` permanece intocado** como camada de defesa. *(Não refatorar.)*
3. **`.deile/` no `.gitignore` do projeto.** *(User-global `~/.deile/` é local da máquina, sem risco de vazar em repo.)*
4. **Discovery 100% filesystem-driven.** Drop de `personas.persona_configs` no YAML. *(Adicionar vibe = drop um arquivo.)*
5. **Ordem de assembly:** System Core → Vibe → User → Project → File context. *(Vibe antes de User/Project pra que tom fique perto da identidade; Project por último por ser mais específico.)*
6. **`/vibe global <nome>` é V1.5**, não MVP. *(MVP só escreve em project; user-global é leitura passiva — você cria `~/.deile/DEILE.md` e `~/.deile/settings.json` à mão se quiser.)*
7. **Walk recursivo subindo de CWD** (estilo CLAUDE.md project memory) é roadmap pós-V1, **não V1**. V1 lê só `<CWD>/DEILE.md`.
8. **Sem alias `mood` pro slash command.** Só `/vibe`. *(Evita confusão e ambiguidade nos docs.)*
9. **`default_vibe` em `api_config.yaml`**, não no frontmatter da vibe. *(Fonte única de verdade.)*
10. **Frontmatter parser manual** (~30 linhas), sem `python-frontmatter` como dep nova. *(Evita inflar `requirements.txt` por algo trivial.)*
11. **PersonaManager mantido como camada de compat**, não removido. *(Compatibilidade com `export_command.py` e `context_command.py`. Deprecação em V2.)*
12. **Atomic write pro settings.json** (tempfile + os.replace). *(Defensivo contra crash mid-write.)*

Se concordar com 1–12, implemento exatamente como descrito. Se quiser ajustar algum, marca aqui antes de eu codar.

---

## 17. Estimativa

- **Tier:** Large (toca PersonaManager, ContextManager, instruction_loader, adiciona slash command, 16 arquivos novos de instrução, 3 módulos novos de código, 4+4 arquivos de teste).
- **Esforço:** 6–10 horas se sequencial; 4–6 se paralelizar instruções e código.

### Ordem de implementação (cada passo termina com `pytest` 100% verde antes do próximo)

| # | Passo | Verifica |
|---|---|---|
| 1 | Extrair `personas/instructions/DEILE.md` (system core) do `developer.md`. Criar `vibe_executor.md`. **Não** deletar `developer.md` ainda. | grep manual: invariantes em DEILE.md, tom em vibe_executor.md. |
| 2 | `instruction_loader.load_md_path()` + `vibe_loader.py` + `vibe_state.py`. | `test_vibe_state.py`, `test_vibe_loader.py`. |
| 3 | `context_manager.py` refactor (4 layers + degradation). `personas/manager.py` adapt to filesystem discovery. | `test_layered_assembly.py`. Smoke local: rode DEILE, mande "olá". Verifica nada quebrou. |
| 4 | **Agora** delete `developer.md`. Adicionar regression test (`test_developer_md_removed.py`). | Pytest verde sem developer.md. |
| 5 | Criar as 14 vibes restantes (pitbull, cetico, arquiteto, devil, steelman, god, proativo, passivo, zen, bomhumor, prestativo, hacker, monitor, shadow). Cada uma 30-60 linhas. | `test_soft_vibes_safety_paragraph.py`, `test_vibe_md_no_core_contradiction.py`. |
| 6 | `vibe_command.py` + `test_vibe_command.py`. | Pytest + smoke local: `/vibe`, `/vibe pitbull`, `/vibe foo`. |
| 7 | `console_ui.py` welcome line. Adicionar `default_vibe: executor` em `api_config.yaml`. Adicionar `.deile/` em `.gitignore`. | Smoke local: rode DEILE, vê linha "Vibe X" no banner. |
| 8 | Smoke empírico (`deile/tests/might/multi-vibe/`) com **deepseek-v4-flash**: pitbull vs cetico vs zen no mesmo prompt. Confirma tom muda. | Logs `.log` salvos pra inspeção visual. |
| 9 | PR final. Title: `feat: multi-vibe system + layered system instruction (4 layers)`. Body com link pra este doc. | — |

### Riscos de execução por passo

- **Passo 3** é o ponto mais arriscado: refactor do path crítico. Smoke local antes de avançar, não pular.
- **Passo 5** é o mais demorado mas low-risk (só conteúdo de prompt, sem código).
- **Passo 8** custa tokens reais (~$0.05 total).
