#!/usr/bin/env bash
# deile.sh — bootstrap and launch script for DEILE
#
# Verifica/instala Python, cria .venv se necessário, ativa o ambiente,
# pede chaves de API na primeira execução, instala dependências e
# inicia o agente. Idempotente: na segunda execução só ativa o venv e
# inicia o DEILE.
#
# Uso:
#   ./deile.sh              # modo interativo
#   ./deile.sh "pergunta"   # modo one-shot (forwarded para deile.py)

set -euo pipefail

# -----------------------------------------------------------------------------
# Cores e helpers de impressão
# -----------------------------------------------------------------------------
if [[ -t 1 ]]; then
    RED=$'\033[0;31m'
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[1;33m'
    BLUE=$'\033[0;34m'
    CYAN=$'\033[0;36m'
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    RESET=$'\033[0m'
else
    RED='' ; GREEN='' ; YELLOW='' ; BLUE='' ; CYAN='' ; BOLD='' ; DIM='' ; RESET=''
fi

info()  { printf "  %sℹ%s  %s\n"   "$CYAN"   "$RESET" "$*"; }
ok()    { printf "  %s✓%s  %s\n"   "$GREEN"  "$RESET" "$*"; }
warn()  { printf "  %s⚠%s  %s\n"   "$YELLOW" "$RESET" "$*"; }
err()   { printf "  %s✗%s  %s\n"   "$RED"    "$RESET" "$*" >&2; }
step()  { printf "\n%s▶ %s%s\n"    "$BOLD$BLUE" "$*" "$RESET"; }

banner() {
    clear
    cat <<EOF
${BOLD}${CYAN}
  ╔════════════════════════════════════════════════════╗
  ║                                                    ║
  ║    🤖   D E I L E   —   bootstrap installer        ║
  ║                                                    ║
  ║    Development Environment Intelligence            ║
  ║    & Learning Engine                               ║
  ║                                                    ║
  ╚════════════════════════════════════════════════════╝
${RESET}
EOF
}

# -----------------------------------------------------------------------------
# Sanidade: rodar a partir do diretório do próprio script
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f deile.py ]]; then
    banner
    err "deile.py não encontrado em $SCRIPT_DIR."
    err "Execute deile.sh a partir do diretório raiz do repositório DEILE."
    exit 1
fi

banner

# -----------------------------------------------------------------------------
# Detecção de Python
# -----------------------------------------------------------------------------
PY=""

detect_python() {
    if command -v python3 >/dev/null 2>&1; then
        PY=python3
        return 0
    fi
    if command -v python >/dev/null 2>&1; then
        if python -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' 2>/dev/null; then
            PY=python
            return 0
        fi
    fi
    return 1
}

check_python_version() {
    "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null
}

# -----------------------------------------------------------------------------
# Detecção de OS
# -----------------------------------------------------------------------------
detect_os() {
    case "$(uname -s)" in
        Darwin) echo macos ;;
        Linux)
            if [[ -f /etc/os-release ]]; then
                # shellcheck disable=SC1091
                . /etc/os-release
                case "${ID:-}" in
                    ubuntu|debian|linuxmint|pop|elementary) echo debian ;;
                    fedora|rhel|centos|rocky|almalinux)     echo fedora ;;
                    arch|manjaro|endeavouros|garuda)        echo arch ;;
                    alpine)                                 echo alpine ;;
                    *)
                        case "${ID_LIKE:-}" in
                            *debian*)         echo debian ;;
                            *fedora*|*rhel*)  echo fedora ;;
                            *arch*)           echo arch ;;
                            *)                echo unknown ;;
                        esac
                        ;;
                esac
            else
                echo unknown
            fi
            ;;
        *) echo unknown ;;
    esac
}

# -----------------------------------------------------------------------------
# Instalação de Python por OS
# -----------------------------------------------------------------------------
install_python() {
    local os=$1
    case "$os" in
        macos)
            if ! command -v brew >/dev/null 2>&1; then
                warn "Homebrew não encontrado — necessário para instalar Python no macOS."
                read -rp "  Instalar Homebrew agora? [s/N] " yn
                if [[ "$yn" =~ ^[sSyY]$ ]]; then
                    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
                    if [[ -x /opt/homebrew/bin/brew ]]; then
                        eval "$(/opt/homebrew/bin/brew shellenv)"
                    elif [[ -x /usr/local/bin/brew ]]; then
                        eval "$(/usr/local/bin/brew shellenv)"
                    fi
                else
                    err "Sem Homebrew não consigo instalar Python automaticamente."
                    err "Baixe em https://www.python.org/downloads/ e re-execute ./deile.sh."
                    exit 1
                fi
            fi
            info "Instalando Python 3 via Homebrew..."
            brew install python3
            ;;
        debian)
            info "Instalando Python 3 via apt (precisa de sudo)..."
            sudo apt-get update -y
            sudo apt-get install -y python3 python3-venv python3-pip
            ;;
        fedora)
            info "Instalando Python 3 via dnf (precisa de sudo)..."
            sudo dnf install -y python3 python3-pip
            ;;
        arch)
            info "Instalando Python 3 via pacman (precisa de sudo)..."
            sudo pacman -Sy --noconfirm python python-pip
            ;;
        alpine)
            info "Instalando Python 3 via apk (precisa de sudo)..."
            sudo apk add --no-cache python3 py3-pip
            ;;
        *)
            err "Sistema '$os' não suportado pelo instalador automático."
            err "Instale Python 3.9+ manualmente: https://www.python.org/downloads/"
            exit 1
            ;;
    esac
}

# -----------------------------------------------------------------------------
# Etapa 1 — garantir Python 3.9+
# -----------------------------------------------------------------------------
ensure_python() {
    step "Verificando Python"
    if detect_python; then
        if check_python_version; then
            ok "$($PY -V 2>&1) encontrado em $(command -v "$PY")"
            return 0
        else
            err "$PY encontrado, mas é < 3.9 ($($PY -V 2>&1)). DEILE requer 3.9+."
            err "Atualize seu Python ou use pyenv para instalar 3.9 ou superior."
            exit 1
        fi
    fi

    warn "Python 3 não encontrado neste sistema."
    local detected
    detected=$(detect_os)
    info "Sistema detectado: ${BOLD}${detected}${RESET}"

    local os="$detected"
    if [[ "$detected" == "unknown" ]]; then
        echo
        info "Sistemas suportados pelo instalador: macos | debian | fedora | arch | alpine"
        read -rp "  Informe seu sistema: " os
    else
        read -rp "  Confirma instalar Python para esse sistema? [S/n] " yn
        if [[ "$yn" =~ ^[nN]$ ]]; then
            read -rp "  Informe o sistema (macos | debian | fedora | arch | alpine): " os
        fi
    fi

    install_python "$os"

    if ! detect_python; then
        err "Python ainda não disponível após a instalação. Verifique manualmente."
        exit 1
    fi
    if ! check_python_version; then
        err "A versão instalada é < 3.9 ($($PY -V 2>&1))."
        exit 1
    fi
    ok "$($PY -V 2>&1) instalado"
}

# -----------------------------------------------------------------------------
# Etapa 2 — garantir .venv
# -----------------------------------------------------------------------------
ensure_venv() {
    step "Verificando ambiente virtual (.venv)"
    if [[ -d .venv && -x .venv/bin/python ]]; then
        ok ".venv já existe"
        return 0
    fi

    info "Criando .venv com $PY -m venv .venv..."
    local errfile
    errfile=$(mktemp)
    if ! "$PY" -m venv .venv 2> "$errfile"; then
        if grep -qiE "ensurepip|venv|pyvenv" "$errfile"; then
            warn "Falha ao criar .venv — provavelmente falta o módulo venv do sistema."
            local os
            os=$(detect_os)
            case "$os" in
                debian)
                    info "Tentando instalar python3-venv via apt..."
                    sudo apt-get install -y python3-venv
                    "$PY" -m venv .venv
                    ;;
                *)
                    cat "$errfile" >&2
                    err "Não consegui criar .venv. Instale o módulo venv para o seu Python."
                    rm -f "$errfile"
                    exit 1
                    ;;
            esac
        else
            cat "$errfile" >&2
            rm -f "$errfile"
            exit 1
        fi
    fi
    rm -f "$errfile"
    ok ".venv criado"
}

# -----------------------------------------------------------------------------
# Etapa 3 — sempre ativar o venv
# -----------------------------------------------------------------------------
activate_venv() {
    step "Ativando ambiente virtual"
    # shellcheck disable=SC1091
    source .venv/bin/activate
    PY=python
    ok "Ativo: $(command -v python)"
}

# -----------------------------------------------------------------------------
# Etapa 4 — garantir .env (com prompt seguro de chaves)
# -----------------------------------------------------------------------------
ensure_env() {
    step "Verificando .env"
    if [[ -f .env ]]; then
        ok ".env já existe"
        return 0
    fi

    warn ".env não encontrado — vamos configurar agora."
    echo
    printf "  %sChaves de API%s\n" "$BOLD" "$RESET"
    printf "  %s─────────────%s\n" "$DIM" "$RESET"
    printf "  Você precisa de %sPELO MENOS UMA%s chave entre os 4 providers.\n" "$BOLD" "$RESET"
    printf "  %sPressione ENTER (em branco) para pular qualquer chave.%s\n" "$DIM" "$RESET"
    printf "  %sA digitação fica oculta por segurança.%s\n" "$DIM" "$RESET"
    echo

    local anth oai dsk ggl
    read -rsp "  ANTHROPIC_API_KEY : " anth || true; echo
    read -rsp "  OPENAI_API_KEY    : " oai  || true; echo
    read -rsp "  DEEPSEEK_API_KEY  : " dsk  || true; echo
    read -rsp "  GOOGLE_API_KEY    : " ggl  || true; echo

    if [[ -z "$anth" && -z "$oai" && -z "$dsk" && -z "$ggl" ]]; then
        err "Você não informou nenhuma chave. DEILE precisa de pelo menos uma para subir."
        exit 1
    fi

    umask 077
    cat > .env <<EOF
# Gerado por deile.sh em $(date '+%Y-%m-%d %H:%M:%S')
# Você pode preencher chaves vazias depois para ativar mais providers.
ANTHROPIC_API_KEY=${anth}
OPENAI_API_KEY=${oai}
DEEPSEEK_API_KEY=${dsk}
GOOGLE_API_KEY=${ggl}
EOF
    chmod 600 .env

    sleep 1
    banner

    local count=0
    [[ -n "$anth" ]] && count=$((count+1))
    [[ -n "$oai"  ]] && count=$((count+1))
    [[ -n "$dsk"  ]] && count=$((count+1))
    [[ -n "$ggl"  ]] && count=$((count+1))
    ok ".env criado com ${count} chave(s) preenchida(s) — permissões 0600"
}

# -----------------------------------------------------------------------------
# Etapa 5 — instalar dependências (com cache via marker)
# -----------------------------------------------------------------------------
install_deps() {
    step "Instalando dependências"
    local marker=".venv/.deile-deps-installed"

    if [[ -f "$marker" && requirements.txt -ot "$marker" ]]; then
        ok "Dependências já instaladas (requirements.txt sem mudanças desde a última vez)"
        return 0
    fi

    info "Executando pip install -r requirements.txt (pode demorar na primeira vez)..."
    python -m pip install --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
    python -m pip install --disable-pip-version-check -r requirements.txt
    touch "$marker"
    ok "Dependências instaladas"
}

# -----------------------------------------------------------------------------
# Etapa 6 — iniciar DEILE
# -----------------------------------------------------------------------------
launch() {
    step "Iniciando DEILE"
    echo
    info "Dica: digite ${BOLD}/help${RESET} dentro do prompt para listar os comandos."
    echo
    exec python deile.py "$@"
}

# -----------------------------------------------------------------------------
# Pipeline
# -----------------------------------------------------------------------------
ensure_python
ensure_venv
activate_venv
ensure_env
install_deps
launch "$@"
