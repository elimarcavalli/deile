# Mapa de arquivos grandes (≥ 1000 LOC) — base para refactor e cleanup

> Gerado em 2026-06-04 a partir do `origin/main` (pós `style(lint)` ruff). Fonte das métricas de linha: `radon raw`. Fonte do impacto (fan-in): resolução de imports por AST (absolutos + relativos) sobre os 809 arquivos `.py` de `deile/` (0 falhas de parse).

**Escopo:** todos os arquivos `.py` de `deile/` com **LOC total ≥ 1000**: **17** arquivos (**11 de fonte** + **6 de teste**).

## Legenda das colunas

| Coluna | Significado |
|---|---|
| **Total LOC** | Todas as linhas físicas do arquivo (inclui comentários, docstrings e blank). |
| **Código (SLOC)** | *Net weight* — linhas de código-fonte reais (exclui blank/comment/docstring). `radon.sloc`. |
| **Comentários** | Linhas de comentário `#` (inclui inline). `radon.comments`. |
| **Docstrings** | Linhas dentro de strings multilinha / docstrings. `radon.multi`. |
| **Blank** | Linhas em branco. `radon.blank`. |
| **Coment+Doc %** | `(Comentários + Docstrings) / Total LOC`. Sinaliza arquivos *pesados de prosa*. |
| **Fan-in (src)** | Nº de módulos de **produção** (não-teste) que importam este arquivo = blast radius real. |
| **Fan-in (test)** | Nº de arquivos de **teste** que o importam = churn de teste ao refatorar. |

---

## Tabela 1 — Crítico primeiro (ordenado por Total LOC ↓)

> Os maiores arquivos do código. Quanto maior, maior o risco de violação de SRP e mais difícil de navegar/revisar.

| # | Arquivo | Tipo | Total LOC | Código (SLOC) | Comentários | Docstrings | Blank | Coment+Doc % |
|---|---|---|--:|--:|--:|--:|--:|--:|
| 1 | `deile/orchestration/pipeline/stages.py` | fonte | 3404 | 2221 | 442 | 466 | 325 | 26.7% |
| 2 | `deile/tests/infrastructure/test_claude_worker_server.py` | teste | 2890 | 2070 | 175 | 135 | 475 | 10.7% |
| 3 | `deile/tests/infra/test_panel_data.py` | teste | 2749 | 2132 | 206 | 56 | 350 | 9.5% |
| 4 | `deile/core/agent.py` | fonte | 2087 | 1469 | 245 | 116 | 258 | 17.3% |
| 5 | `deile/config/settings.py` | fonte | 1619 | 975 | 285 | 203 | 204 | 30.1% |
| 6 | `deile/orchestration/pipeline/implementer.py` | fonte | 1580 | 957 | 239 | 259 | 145 | 31.5% |
| 7 | `deile/tests/ui/test_streaming_renderer.py` | teste | 1490 | 1116 | 100 | 139 | 134 | 16.0% |
| 8 | `deile/orchestration/forge/gitlab_forge.py` | fonte | 1374 | 962 | 105 | 191 | 114 | 21.5% |
| 9 | `deile/tools/file_tools.py` | fonte | 1353 | 972 | 153 | 48 | 173 | 14.9% |
| 10 | `deile/core/models/gemini_provider.py` | fonte | 1293 | 822 | 186 | 120 | 167 | 23.7% |
| 11 | `deile/tests/test_install_functions.py` | teste | 1241 | 749 | 134 | 54 | 247 | 15.1% |
| 12 | `deile/orchestration/forge/github_forge.py` | fonte | 1209 | 890 | 83 | 131 | 104 | 17.7% |
| 13 | `deile/tests/orchestration/pipeline/test_monitor.py` | teste | 1206 | 909 | 96 | 52 | 132 | 12.3% |
| 14 | `deile/cli.py` | fonte | 1199 | 790 | 161 | 106 | 163 | 22.3% |
| 15 | `deile/ui/streaming_renderer.py` | fonte | 1099 | 641 | 257 | 98 | 101 | 32.3% |
| 16 | `deile/tests/orchestration/pipeline/test_gaps_132.py` | teste | 1060 | 779 | 79 | 26 | 174 | 9.9% |
| 17 | `deile/orchestration/plan_manager.py` | fonte | 1019 | 714 | 81 | 24 | 172 | 10.3% |

---

## Tabela 2 — Peso de comentários/docstrings (ordenado por Comentários+Docstrings ↓)

> Candidatos a **enxugamento de comentários**. Regra do projeto: *código bem escrito > código poluído de comentários*; comentário não é prova, o código é. Docstrings de API ficam; comentários redundantes saem.

| # | Arquivo | Comentários (#) | Docstrings | Total coment. | % do arquivo | Total LOC |
|---|---|--:|--:|--:|--:|--:|
| 1 | `deile/orchestration/pipeline/stages.py` | 442 | 466 | 908 | 26.7% | 3404 |
| 2 | `deile/orchestration/pipeline/implementer.py` | 239 | 259 | 498 | 31.5% | 1580 |
| 3 | `deile/config/settings.py` | 285 | 203 | 488 | 30.1% | 1619 |
| 4 | `deile/core/agent.py` | 245 | 116 | 361 | 17.3% | 2087 |
| 5 | `deile/ui/streaming_renderer.py` | 257 | 98 | 355 | 32.3% | 1099 |
| 6 | `deile/tests/infrastructure/test_claude_worker_server.py` | 175 | 135 | 310 | 10.7% | 2890 |
| 7 | `deile/core/models/gemini_provider.py` | 186 | 120 | 306 | 23.7% | 1293 |
| 8 | `deile/orchestration/forge/gitlab_forge.py` | 105 | 191 | 296 | 21.5% | 1374 |
| 9 | `deile/cli.py` | 161 | 106 | 267 | 22.3% | 1199 |
| 10 | `deile/tests/infra/test_panel_data.py` | 206 | 56 | 262 | 9.5% | 2749 |
| 11 | `deile/tests/ui/test_streaming_renderer.py` | 100 | 139 | 239 | 16.0% | 1490 |
| 12 | `deile/orchestration/forge/github_forge.py` | 83 | 131 | 214 | 17.7% | 1209 |
| 13 | `deile/tools/file_tools.py` | 153 | 48 | 201 | 14.9% | 1353 |
| 14 | `deile/tests/test_install_functions.py` | 134 | 54 | 188 | 15.1% | 1241 |
| 15 | `deile/tests/orchestration/pipeline/test_monitor.py` | 96 | 52 | 148 | 12.3% | 1206 |
| 16 | `deile/tests/orchestration/pipeline/test_gaps_132.py` | 79 | 26 | 105 | 9.9% | 1060 |
| 17 | `deile/orchestration/plan_manager.py` | 81 | 24 | 105 | 10.3% | 1019 |

---

## Tabela 3 — Ordem de refactor: **menor impacto primeiro** (fonte; fan-in src ↑)

> *Map all impact before proposing.* Começar onde o blast radius é menor reduz risco de regressão. Fan-in src = quantos módulos de produção quebram se a API pública deste arquivo mudar. `Hot-path` = no caminho do runtime autônomo (pipeline/forge/core agent/models) — exige cautela extra mesmo com fan-in baixo.

| # | Arquivo | Total LOC | Fan-in (src) | Fan-in (test) | Impacto | Hot-path | Primeira ação sugerida |
|---|---|--:|--:|--:|---|:--:|---|
| 1 | `deile/orchestration/pipeline/stages.py` | 3404 | 1 | 12 | Baixo | 🔥 | Enxugar comentários/docstrings redundantes + extrair por SRP |
| 2 | `deile/orchestration/forge/gitlab_forge.py` | 1374 | 1 | 2 | Baixo | 🔥 | Quebrar por responsabilidade (SRP); extrair grupos coesos de funções |
| 3 | `deile/tools/file_tools.py` | 1353 | 1 | 6 | Baixo | · | Quebrar por responsabilidade (SRP); extrair grupos coesos de funções |
| 4 | `deile/cli.py` | 1199 | 1 | 11 | Baixo | · | Quebrar por responsabilidade (SRP); extrair grupos coesos de funções |
| 5 | `deile/ui/streaming_renderer.py` | 1099 | 1 | 2 | Baixo | · | Enxugar comentários/docstrings redundantes + extrair por SRP |
| 6 | `deile/orchestration/pipeline/implementer.py` | 1580 | 2 | 19 | Baixo | 🔥 | Enxugar comentários/docstrings redundantes + extrair por SRP |
| 7 | `deile/orchestration/forge/github_forge.py` | 1209 | 2 | 5 | Baixo | 🔥 | Quebrar por responsabilidade (SRP); extrair grupos coesos de funções |
| 8 | `deile/core/models/gemini_provider.py` | 1293 | 3 | 9 | Baixo | 🔥 | Quebrar por responsabilidade (SRP); extrair grupos coesos de funções |
| 9 | `deile/core/agent.py` | 2087 | 9 | 29 | Médio | 🔥 | Quebrar por responsabilidade (SRP); extrair grupos coesos de funções |
| 10 | `deile/orchestration/plan_manager.py` | 1019 | 11 | 4 | Alto | · | Extrair atrás de fachada estável; mover helpers sem quebrar API pública |
| 11 | `deile/config/settings.py` | 1619 | 35 | 44 | Alto | · | Enxugar comentários/docstrings redundantes + extrair por SRP |

### Arquivos de teste ≥ 1000 LOC (impacto ~zero — split por feature quando conveniente)

| Arquivo | Total LOC | Código (SLOC) |
|---|--:|--:|
| `deile/tests/infrastructure/test_claude_worker_server.py` | 2890 | 2070 |
| `deile/tests/infra/test_panel_data.py` | 2749 | 2132 |
| `deile/tests/ui/test_streaming_renderer.py` | 1490 | 1116 |
| `deile/tests/test_install_functions.py` | 1241 | 749 |
| `deile/tests/orchestration/pipeline/test_monitor.py` | 1206 | 909 |
| `deile/tests/orchestration/pipeline/test_gaps_132.py` | 1060 | 779 |

---

## Metodologia e limitações

- **Linhas:** `radon raw -j deile/` (ferramenta padrão; determinística).
- **Fan-in:** AST de cada `.py`; `import a.b`, `from a.b import c` (gera aresta para `a.b` **e** `a.b.c`) e relativos (`from .x`, `from ..y`) resolvidos para módulo absoluto. Conta arquivos importadores distintos, excluindo o próprio.
- **Não capturado:** imports dinâmicos (`importlib.import_module`, `__import__`), descoberta por registry/auto-discovery e referências em strings/YAML. Para módulos plugados por registry o fan-in **subestima** o acoplamento real — validar antes de refatorar os hot-path.
- **`Coment+Doc %` alto** não é defeito por si: em parsers/schemas docstring é documentação legítima. Cruzar com a regra do projeto antes de cortar.
- Métricas tiradas **após** o `style(lint)` ruff, refletindo o estado do `main`.

## Como usar este doc

1. **Cleanup de comentários:** Tabela 2, de cima pra baixo.
2. **Refactor estrutural:** Tabela 3, de cima pra baixo (menor impacto primeiro). Cada item vira uma issue isolada.
3. **Prioridade de severidade:** Tabela 1 dá o tamanho bruto (o quão urgente é quebrar o monólito).
4. Reexecutar o gerador após cada refactor para acompanhar a queda das métricas.
