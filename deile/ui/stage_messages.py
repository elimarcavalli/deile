"""Deterministic Portuguese message library for all DEILE round-trip scenarios.

All 58 blocking round-trip points emit structured feedback via STAGE or PROGRESS
events. This module maps each scenario to a deterministic, contextual, Portuguese
message following the pattern:

    [Verbo direto em pt-BR] [objeto técnico real]... [contexto opcional]

Temporal cascade levels:
  - ``initial`` — shown immediately (< 3s)
  - ``after_3s`` — shown after 3s (reinforcement)
  - ``after_10s`` — shown after 10s (acknowledgement of slowness)
  - ``after_30s`` — shown after 30s (assume real delay)

The spinner never changes text before 3s (prevents flicker).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class StageMessages:
    """Immutable set of cascade messages for a single scenario."""
    initial: str
    after_3s: Optional[str] = None
    after_10s: Optional[str] = None
    after_30s: Optional[str] = None

    def at_level(self, level: str = "initial") -> str:
        """Return the message at the given cascade level, falling back
        to the nearest earlier level."""
        order = ("initial", "after_3s", "after_10s", "after_30s")
        best = self.initial
        for lv in order:
            msg = getattr(self, lv, None)
            if msg:
                best = msg
            if lv == level:
                return best
        return best


# ──────────────────────────────────────────────────────────────────────
# Message library — 58 scenarios grouped by phase
# ──────────────────────────────────────────────────────────────────────

STAGE_MESSAGES: Dict[str, StageMessages] = {}

# ----------------------------------------------------------------------
# Startup / Bootstrap (4 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["startup_bootstrap"] = StageMessages(
    initial="Acordando DEILE v{version}...",
    after_3s="Carregando providers...",
    after_10s="Validando chaves de API...",
)

STAGE_MESSAGES["startup_autocomplete"] = StageMessages(
    initial="Mapeando workspace... ({count} arquivos)",
    after_3s="Indexando projeto... ({count} arquivos)",
    after_10s="Workspace é grande mesmo... ({count} arquivos)",
    after_30s="Ainda mapeando... ({count} arquivos encontrados)",
)

STAGE_MESSAGES["startup_health_check"] = StageMessages(
    initial="Verificando saúde dos providers...",
    after_3s="Aguardando heartbeat...",
    after_10s="Provider lento no heartbeat — tentando fallback...",
)

STAGE_MESSAGES["startup_session"] = StageMessages(
    initial="Preparando sessão...",
    after_3s="Carregando histórico...",
)

STAGE_MESSAGES["budget_guard"] = StageMessages(
    initial="Conferindo orçamento...",
    after_3s="Verificando uso de tokens...",
)

STAGE_MESSAGES["workspace_scan_context"] = StageMessages(
    initial="Varrendo workspace... ({count} arquivos)",
    after_3s="Workspace grande... ({count} arquivos)",
    after_10s="Repo bem grande... ({count} arquivos)",
)

STAGE_MESSAGES["gemini_tool_aware"] = StageMessages(
    initial="Gemini pensando com ferramentas...",
    after_3s="Gemini formulando rodada...",
    after_10s="Gemini está caprichando, paciência...",
)

STAGE_MESSAGES["legacy_mode"] = StageMessages(
    initial="Processando sua solicitação...",
    after_3s="Ainda processando...",
    after_10s="Tá demorando, mas tá vindo...",
)

STAGE_MESSAGES["workflow_detect"] = StageMessages(
    initial="Avaliando se workflow é necessário...",
)

# ----------------------------------------------------------------------
# Slash Commands (12 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["slash_generic"] = StageMessages(
    initial="Executando /{cmd}...",
    after_3s="/{cmd} processando...",
    after_10s="/{cmd} levou mais tempo que o normal...",
)

STAGE_MESSAGES["slash_plan"] = StageMessages(
    initial="Montando seu plano...",
    after_3s="Estruturando etapas...",
    after_10s="Plano grande, organizando...",
)

STAGE_MESSAGES["slash_run"] = StageMessages(
    initial="Rodando plano...",
    after_3s="Etapa em execução...",
    after_10s="Plano longo, seguindo...",
)

STAGE_MESSAGES["slash_sandbox"] = StageMessages(
    initial="Preparando sandbox Docker...",
    after_3s="Construindo imagem...",
    after_10s="Container ainda subindo... (pode ser pull de imagem)",
    after_30s="Build do Docker é coisa séria, paciência...",
)

STAGE_MESSAGES["slash_model_list"] = StageMessages(
    initial="Inventariando modelos disponíveis...",
    after_3s="Consultando catálogo de modelos...",
)

STAGE_MESSAGES["slash_model_use"] = StageMessages(
    initial="Trocando de modelo...",
    after_3s="Validando disponibilidade...",
)

STAGE_MESSAGES["slash_status"] = StageMessages(
    initial="Pingando provedores...",
    after_3s="Testando conectividade...",
)

STAGE_MESSAGES["slash_clear"] = StageMessages(
    initial="Parando planos ativos...",
    after_3s="Limpando estado...",
)

STAGE_MESSAGES["slash_cost"] = StageMessages(
    initial="Calculando custo da sessão...",
    after_3s="Consultando repositório de uso...",
)

STAGE_MESSAGES["slash_memory"] = StageMessages(
    initial="Acessando memórias...",
    after_3s="Consolidando memórias recentes...",
    after_10s="Memória bem cheia, vai um tempinho...",
)

STAGE_MESSAGES["slash_help"] = StageMessages(
    initial="Buscando ajuda...",
)

STAGE_MESSAGES["slash_logs"] = StageMessages(
    initial="Buscando logs...",
)

STAGE_MESSAGES["slash_diff"] = StageMessages(
    initial="Calculando diff...",
)

STAGE_MESSAGES["slash_permissions"] = StageMessages(
    initial="Lendo permissões...",
)

STAGE_MESSAGES["slash_config"] = StageMessages(
    initial="Lendo config...",
)

STAGE_MESSAGES["slash_tools"] = StageMessages(
    initial="Inventariando tools...",
)

STAGE_MESSAGES["slash_compact"] = StageMessages(
    initial="Compactando histórico...",
)

STAGE_MESSAGES["slash_patch_generate"] = StageMessages(
    initial="Lendo plano para gerar patches...",
    after_3s="Calculando diffs...",
)

STAGE_MESSAGES["slash_patch_apply"] = StageMessages(
    initial="Validando patches...",
    after_3s="Aplicando alterações...",
    after_10s="Backup criado, aplicando...",
)

STAGE_MESSAGES["slash_context"] = StageMessages(
    initial="Analisando contexto da sessão...",
    after_3s="Calculando uso de tokens no contexto...",
)

STAGE_MESSAGES["slash_apply"] = StageMessages(
    initial="Aplicando patch...",
    after_3s="Validando diff antes de aplicar...",
)

# ----------------------------------------------------------------------
# Agent Pre-Stream Pipeline (7 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["parse_input"] = StageMessages(
    initial="Interpretando sua mensagem...",
    after_3s="Analisando comandos e referências...",
)

STAGE_MESSAGES["proactive_tools"] = StageMessages(
    initial="Executando ferramentas proativas...",
    after_3s="Rodando análise de contexto...",
)

STAGE_MESSAGES["check_workflow"] = StageMessages(
    initial="Verificando necessidade de workflow...",
    after_3s="Analisando complexidade da intenção...",
)

STAGE_MESSAGES["build_context"] = StageMessages(
    initial="Construindo contexto...",
    after_3s="Montando janela de contexto...",
)

STAGE_MESSAGES["analyze_intent"] = StageMessages(
    initial="Analisando complexidade...",
    after_3s="Classificando tier da tarefa...",
)

STAGE_MESSAGES["select_provider"] = StageMessages(
    initial="Escolhendo provider...",
    after_3s="Avaliando saúde dos providers...",
)

STAGE_MESSAGES["connect_model"] = StageMessages(
    initial="Conectando com {model}...",
    after_3s="Estabelecendo conexão com {model}...",
    after_10s="Conexão com {model} demorando...",
    after_30s="Ainda conectando com {model}...",
)

# ----------------------------------------------------------------------
# Tool-Loop Iterations (5 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["await_first_token"] = StageMessages(
    initial="Pensando...",
    after_3s="Formulando resposta...",
    after_10s="Levou mais tempo, mas tá vindo...",
    after_30s="Ainda processando... aguarde",
)

STAGE_MESSAGES["await_next_response"] = StageMessages(
    initial="Processando rodada {iteration}...",
    after_3s="Rodada {iteration}: analisando resultado...",
    after_10s="Rodada {iteration}: levou mais tempo que o normal...",
)

STAGE_MESSAGES["tool_executing"] = StageMessages(
    initial="Executando {tool}...",
    after_3s="{tool} ainda rodando...",
    after_10s="{tool} é uma operação longa, calma...",
    after_30s="{tool} complexa — pode demorar mesmo",
)

STAGE_MESSAGES["tool_result_processing"] = StageMessages(
    initial="Processando resultado de {tool}...",
)

STAGE_MESSAGES["max_iterations"] = StageMessages(
    initial="Atingiu limite de iterações ({max}) — finalizando...",
)

# ----------------------------------------------------------------------
# Validation Gate (2 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["validation_check"] = StageMessages(
    initial="Verificando se ficou tudo certo...",
)

STAGE_MESSAGES["validation_retry"] = StageMessages(
    initial="Refinando resposta...",
    after_3s="Reformulando resposta...",
    after_10s="Validação puxada, mas seguindo...",
    after_30s="Validação complexa — múltiplos ajustes pendentes...",
)

STAGE_MESSAGES["validation_promise"] = StageMessages(
    initial="Verificando promessa de ação...",
    after_3s="Reavaliando resposta...",
)

# ----------------------------------------------------------------------
# Autonomous & Workflow Paths (3 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["autonomous_process"] = StageMessages(
    initial="Processando requisição autônoma...",
    after_3s="Analisando estado da sessão...",
)

STAGE_MESSAGES["workflow_execute"] = StageMessages(
    initial="Executando workflow ({steps} steps)...",
    after_3s="Workflow step {current}/{total}...",
    after_10s="Workflow com múltiplas etapas — aguarde...",
)

STAGE_MESSAGES["workflow_security"] = StageMessages(
    initial="Verificando segurança do plano...",
    after_3s="Aprovando steps críticos...",
)

# ----------------------------------------------------------------------
# Tools — Execution (4 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["tool_bash"] = StageMessages(
    initial="Executando comando: {cmd}...",
    after_3s="{cmd} em execução...",
    after_10s="{cmd} é um comando longo, aguardando output...",
    after_30s="{cmd} operação pesada — stdout acumulando",
)

STAGE_MESSAGES["tool_pip_install"] = StageMessages(
    initial="Instalando {package}...",
    after_3s="pip install {package} — baixando dependências...",
    after_10s="{package} com dependências pesadas, aguardando build...",
    after_30s="Build de wheel para {package} — isso pode demorar",
)

STAGE_MESSAGES["tool_run_tests"] = StageMessages(
    initial="Rodando testes: {target}...",
    after_3s="Test runner executando... ({count} testes encontrados)",
    after_10s="Suite de testes longa — {count} testes...",
    after_30s="Muitos testes ({count}) — ainda executando...",
)

STAGE_MESSAGES["tool_process"] = StageMessages(
    initial="Monitorando processo: {pid}...",
    after_3s="Processo {pid} ativo... ({cpu}% CPU)",
)

# ----------------------------------------------------------------------
# Tools — File System (5 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["tool_read_file_large"] = StageMessages(
    initial="Lendo {file}... ({size})",
    after_3s="Arquivo grande — {file} ({size})...",
)

STAGE_MESSAGES["tool_write_file"] = StageMessages(
    initial="Escrevendo {file}...",
    after_3s="Salvando {file} em disco...",
)

STAGE_MESSAGES["tool_find_files"] = StageMessages(
    initial="Procurando em {path}...",
    after_3s="{matches} matches em {scanned} arquivos escaneados...",
    after_10s="Scan profundo em {path} — {scanned} arquivos...",
)

STAGE_MESSAGES["tool_archive"] = StageMessages(
    initial="Compactando {target}...",
    after_3s="Arquivando arquivos...",
)

STAGE_MESSAGES["tool_lint"] = StageMessages(
    initial="Linting {file}...",
    after_3s="Analisando código com linter...",
)

# ----------------------------------------------------------------------
# Tools — Network (2 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["tool_http"] = StageMessages(
    initial="Chamando {method} {url}...",
    after_3s="Aguardando resposta de {url}...",
    after_10s="{url} lento na resposta — timeout alto...",
    after_30s="{url} não respondeu ainda — verifique conectividade",
)

STAGE_MESSAGES["tool_web_fetch"] = StageMessages(
    initial="Buscando conteúdo de {url}...",
    after_3s="Renderizando página...",
    after_10s="Conteúdo grande — extraindo...",
)

# ----------------------------------------------------------------------
# Tools — Git (3 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["tool_git_status"] = StageMessages(
    initial="Verificando estado do repositório...",
)

STAGE_MESSAGES["tool_git_diff"] = StageMessages(
    initial="Computando diff...",
    after_3s="Diff grande — calculando...",
)

STAGE_MESSAGES["tool_git_commit"] = StageMessages(
    initial="Criando commit...",
    after_3s="Commit com hooks — executando pre-commit...",
)

# ----------------------------------------------------------------------
# Provider / Model (3 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["provider_fallback"] = StageMessages(
    initial="Provider falhou — tentando fallback...",
    after_3s="Cascateando para fallback...",
)

STAGE_MESSAGES["provider_retry"] = StageMessages(
    initial="Retentativa {attempt}/{max}...",
    after_3s="Tentativa {attempt} ainda processando...",
)

STAGE_MESSAGES["provider_circuit_open"] = StageMessages(
    initial="Provider em curto-circuito — usando fallback...",
)

# ----------------------------------------------------------------------
# Memory & Context (4 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["memory_store"] = StageMessages(
    initial="Salvando contexto na memória...",
    after_3s="Consolidando memórias do turno...",
)

STAGE_MESSAGES["memory_retrieve"] = StageMessages(
    initial="Buscando memórias relevantes...",
    after_3s="Consultando camada semântica...",
)

STAGE_MESSAGES["context_compress"] = StageMessages(
    initial="Compactando contexto...",
    after_3s="Comprimindo histórico para caber na janela...",
)

STAGE_MESSAGES["history_truncate"] = StageMessages(
    initial="Truncando histórico... ({tokens} tokens)",
)

# ----------------------------------------------------------------------
# Session / Misc (4 scenarios)
# ----------------------------------------------------------------------

STAGE_MESSAGES["session_create"] = StageMessages(
    initial="Criando sessão em {path}...",
)

STAGE_MESSAGES["session_resume"] = StageMessages(
    initial="Retomando sessão anterior...",
    after_3s="Carregando checkpoint...",
)

STAGE_MESSAGES["shutdown"] = StageMessages(
    initial="Finalizando DEILE...",
)

STAGE_MESSAGES["default_wait"] = StageMessages(
    initial="Pensando...",
    after_3s="Processando...",
    after_10s="Ainda processando...",
    after_30s="Isso está demorando mais que o normal...",
)


def get_stage_message(key: str, level: str = "initial", **ctx) -> str:
    """Return a deterministic, context-filled Portuguese message.

    Args:
        key: Scenario identifier from ``STAGE_MESSAGES``.
        level: Cascade level — ``"initial"``, ``"after_3s"``, ``"after_10s"``, ``"after_30s"``.
        **ctx: Format-string context (provider, tool, file, cmd, count, etc.).

    Returns:
        Formatted message, or the key itself if unknown.
    """
    entry = STAGE_MESSAGES.get(key)
    if entry is None:
        if ctx:
            return key.format(**ctx)
        return key
    raw = entry.at_level(level)
    try:
        return raw.format(**ctx)
    except KeyError:
        return raw


def has_stage_messages(key: str) -> bool:
    """Check if a scenario key is registered in the library."""
    return key in STAGE_MESSAGES


def list_scenario_keys() -> tuple:
    """Return all registered scenario keys for introspection."""
    return tuple(sorted(STAGE_MESSAGES.keys()))
