#!/usr/bin/env bash
# self-fix.sh — pede ao próprio DEILE uma caça a bugs e inconsistências.
#
# Roda o agente em modo one-shot com um prompt direcionado a:
#   1. Varrer o pacote deile/ procurando bugs, riscos e regressões.
#   2. Classificar findings por severidade (CRÍTICO / ALTO / MÉDIO / BAIXO).
#   3. Gravar o relatório em docs/self-fix/<timestamp>-self-fix.md
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
OUT_DIR="docs/self-fix"
OUT_FILE="${OUT_DIR}/${TS}-self-fix.md"
mkdir -p "$OUT_DIR"

COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")

printf "\n%s▶ DEILE self-fix (bug hunt)%s\n" "$BOLD$BLUE" "$RESET"
printf "  branch : %s\n"  "$BRANCH"
printf "  commit : %s\n"  "$COMMIT"
printf "  saída  : %s\n\n" "$OUT_FILE"

PROMPT=$(cat <<EOF
Você está rodando DENTRO do próprio repositório do projeto DEILE — você é o agente caçando bugs no seu próprio código fonte. Branch atual: ${BRANCH}, commit: ${COMMIT}, data: ${TS}.

Sua missão é uma CAÇA A BUGS: identificar inconsistências, regressões silenciosas e riscos no código atual e produzir um relatório priorizado para revisão humana.

ETAPAS OBRIGATÓRIAS:

1. Use list_files (recursive=true, pattern="*.py") e read_file para varrer o pacote 'deile/'. Procure especialmente por:
   - Awaitables não awaited (chamada de coroutine sem 'await').
   - try/except que silenciam exceções (bare except, 'except Exception: pass', except com 'logger.warning' e nada mais).
   - Race conditions em estado compartilhado (singletons, registries, caches sem lock).
   - Validação de input ausente em fronteiras (CLI, file paths, parâmetros de tool vindos do LLM).
   - Vazamento de recursos (file handles abertos sem 'with', conexões DB, processos).
   - Discrepâncias entre o JSON Schema declarado por uma tool e o uso real dos parâmetros (parâmetros declarados mas não usados, ou usados mas não declarados, coerção implícita de tipo).
   - Código inalcançável ou regressões silenciosas (variável calculada e descartada, branch morto, return cedo).
   - Dependências entre módulos com import circular ou import lateral oculto.
   - Funções que retornam tipos diferentes em caminhos diferentes sem documentação.

2. Para cada finding, classifique severidade: CRÍTICO (corrompe estado / vaza dados) | ALTO (falha em runtime ou path comum) | MÉDIO (degrada UX/manutenção) | BAIXO (cosmético).

3. Use write_file para gravar UM ÚNICO documento Markdown EXATAMENTE em '${OUT_FILE}', estruturado:
   - # Self-fix report (data, branch, commit)
   - ## 1. Resumo executivo (contagem por severidade)
   - ## 2. Findings — uma seção por finding, com: ID (FIX-001, FIX-002, ...), título, severidade (badge), arquivo:linha, descrição do problema, reprodução (se aplicável), fix sugerido (sem patch — só descrição).
   - ## 3. Padrões recorrentes detectados
   - ## 4. Recomendações de tooling (linters, type checks, hooks que poderiam prevenir essa classe de bugs)

REGRAS:
- NÃO modifique código fonte. Apenas leitura + write_file no documento de saída.
- NÃO tente fixar nada. Reporte para revisão humana.
- Cada finding precisa apontar arquivos e linhas concretas.
- Não invente: se não tem certeza de que é bug, marque como SUSPEITO em vez de afirmar.
- Termine sua resposta confirmando o caminho do documento criado.
EOF
)

exec python deile.py "$PROMPT"
