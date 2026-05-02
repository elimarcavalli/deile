#!/usr/bin/env bash
# self-improve.sh — pede ao próprio DEILE uma análise arquitetural do código.
#
# Roda o agente em modo one-shot com um prompt direcionado a:
#   1. Mapear o pacote deile/ usando list_files / read_file.
#   2. Identificar melhorias arquiteturais alinhadas a melhores práticas.
#   3. Gravar o relatório em docs/self-improve/<timestamp>-self-improvement.md
#
# Pré-requisito: ter rodado ./deile.sh ao menos uma vez (cria .venv e .env).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -t 1 ]]; then
    GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'
    BLUE=$'\033[0;34m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; BLUE=''; BOLD=''; RESET=''
fi

if [[ ! -f deile.py ]]; then
    printf "%s✗%s deile.py não encontrado em %s\n" "$RED" "$RESET" "$SCRIPT_DIR" >&2
    exit 1
fi

if [[ ! -d .venv ]]; then
    printf "%s✗%s .venv não existe — rode %s./deile.sh%s primeiro.\n" "$RED" "$RESET" "$BOLD" "$RESET" >&2
    exit 1
fi

if [[ ! -f .env ]]; then
    printf "%s✗%s .env não existe — rode %s./deile.sh%s primeiro para configurar suas chaves de API.\n" "$RED" "$RESET" "$BOLD" "$RESET" >&2
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

TS=$(date '+%Y-%m-%d-%H%M%S')
OUT_DIR="docs/self-improve"
OUT_FILE="${OUT_DIR}/${TS}-self-improvement.md"
mkdir -p "$OUT_DIR"

COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")

printf "\n%s▶ DEILE self-improvement%s\n" "$BOLD$BLUE" "$RESET"
printf "  branch : %s\n"  "$BRANCH"
printf "  commit : %s\n"  "$COMMIT"
printf "  saída  : %s\n\n" "$OUT_FILE"

PROMPT=$(cat <<EOF
Você está rodando DENTRO do próprio repositório do projeto DEILE — você é o agente analisando o seu próprio código fonte. Branch atual: ${BRANCH}, commit: ${COMMIT}, data: ${TS}.

Sua missão é produzir uma ANÁLISE ARQUITETURAL CRÍTICA do código e identificar melhorias essenciais para alinhar o projeto com melhores práticas de engenharia de software.

ETAPAS OBRIGATÓRIAS:

1. Use list_files (recursive=true, pattern="*.py") e read_file para mapear o pacote 'deile/'. Foque nos subpacotes: core/, core/models/, tools/, commands/, events/, memory/, security/, storage/, orchestration/, ui/, config/, parsers/, personas/, plugins/, evolution/, infrastructure/.

2. Em cada área, avalie:
   - Aderência aos princípios do projeto (registries para artefatos extensíveis, async-correctness, hexagonal/separação de camadas, segurança, observabilidade).
   - Code smells: deep nesting, métodos longos (>50 linhas), acoplamento alto, duplicação, god classes, abstrações vazadas.
   - Caminhos críticos sem testes (use find_in_files para correlacionar fontes com testes em deile/tests/).
   - Oportunidades de refatoração com alto impacto/baixo risco.
   - Inconsistências entre módulos similares (ex.: providers que divergem na forma de tratar erros).

3. Use write_file para gravar UM ÚNICO documento Markdown EXATAMENTE no caminho '${OUT_FILE}', estruturado nesta ordem:
   - # Self-improvement report (com data, branch e commit no cabeçalho)
   - ## 1. Escopo e metodologia
   - ## 2. Mapa arquitetural resumido (1 parágrafo por subpacote relevante)
   - ## 3. Achados priorizados — três tabelas: P0 (essencial), P1 (importante), P2 (nice-to-have). Cada linha: título | problema | solução proposta | arquivos afetados (com paths) | esforço S/M/L | impacto esperado.
   - ## 4. Quick wins (mudanças <100 linhas que destravam manutenção)
   - ## 5. Padrões recorrentes detectados
   - ## 6. Próximos passos recomendados (ordem sugerida de execução)

REGRAS:
- NÃO modifique NENHUM arquivo do código fonte. Apenas leitura + write_file no documento de saída.
- NÃO execute testes (pytest, ruff, etc).
- Foque em melhorias ARQUITETURAIS — typos, formatação e cosméticos NÃO entram.
- Seja específico: cite arquivos e linhas concretas em cada achado.
- Não invente: se não tem certeza, abra o arquivo antes de afirmar.
- Termine sua resposta confirmando o caminho do documento criado.
EOF
)

exec python deile.py "$PROMPT"
