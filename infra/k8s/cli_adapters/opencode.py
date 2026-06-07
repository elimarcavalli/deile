#!/usr/bin/env python3
"""cli_adapters.opencode — adapter do OpenCode (worker piloto da frota — Tier 1).

OpenCode é um agente de coding agnóstico de provider, distribuído como binário
standalone. Headless via ``opencode run`` (sem TUI). Este adapter pluga o
OpenCode na frota multi-worker pelos cinco pontos do contrato
:class:`~cli_adapters.base.CliAdapter`; toda a maquinaria genérica (lease,
heartbeat, subprocess, gate de git, HTTP) vem do ``cli_worker_server`` +
``_worker_core``.

Decisões deste adapter (alinhadas ao plano §1.4/§1.7/§2.1 e validadas contra a
doc oficial do OpenCode via context7 — ``run`` flags, ``models`` output, schema
de ``permission`` e ``OPENCODE_CONFIG_CONTENT``):

* **Autonomia (§1.4):** ``--dangerously-skip-permissions`` no argv **+** config
  inline ``{"permission":{"*":"allow"}}`` via ``OPENCODE_CONFIG_CONTENT``. Sem
  TTY qualquer prompt de aprovação travaria o subprocess; os dois mecanismos
  combinados garantem auto-approve total (belt-and-suspenders — se a flag não
  existir na versão pinada, a config sozinha ainda libera).
* **Brief (§2.1):** anexado por ``-f <brief_path>`` (flag oficial ``--file``)
  + uma instrução posicional curta apontando pro anexo. ``stdin`` é não-oficial
  no OpenCode; ``-f`` é o caminho suportado.
* **Modelo:** ``-m provider/model`` (string livre nativa, ex.
  ``openrouter/anthropic/claude-3.7-sonnet``); ``None`` deixa o OpenCode usar o
  default da config/conta.
* **Saída (§1.6):** ``--format json`` → NDJSON de eventos (``step_start``,
  ``step_finish``, ``tool_use``, ``text``); :meth:`parse_output` lê o último
  evento textual como veredito. Exit-code não é confiável → o gate de
  commit/push do server decide o sucesso final.
* **list_models:** dinâmico via ``opencode models`` (uma linha ``provider/model``
  por modelo), com **catálogo curado de fallback** quando o comando falha/sem
  rede (o server cacheia o resultado com TTL).
* **Resume:** ``supports_resume=False`` (§2.1 lista opencode como fresh-only); o
  brief lê ``.deile-progress.md`` para contexto natural entre dispatches.
* **Auth (§1.11/§2.1):** ``env`` — ``OPENROUTER_API_KEY`` (uma chave → vários
  providers). Sem login, sem refresh-token.
* **Dirs graváveis (§1.7):** ``HOME`` + ``XDG_DATA_HOME``/``XDG_CONFIG_HOME`` +
  ``XDG_CACHE_HOME`` apontando para baixo de ``home`` (config inline evita
  arquivo). O workdir do repo é gravável por construção (montado pelo server).
* **Egress (§1.13):** ``openrouter.ai`` (LLM) + ``models.dev`` (catálogo que o
  ``opencode models`` consulta). As forges (github/gitlab) são adicionadas pela
  geração de NetworkPolicy de forma transversal.
* **git (§1.5):** ``brief_driven`` — o brief instrui ``git add/commit/push`` sob
  auto-approve; o server valida commit novo + push no gate pós-run.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import List, Optional

from .base import BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.opencode")

#: Config inline injetada via ``OPENCODE_CONFIG_CONTENT`` (escopo "local").
#: ``permission: {"*": "allow"}`` libera TODA tool (bash/edit/webfetch/...) sem
#: prompt — essencial sem TTY. Schema confirmado na doc oficial do OpenCode.
_AUTONOMY_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {"*": "allow"},
}

#: Timeout (s) do ``opencode models`` em :meth:`list_models` (toca models.dev).
_MODELS_CMD_TIMEOUT_S = 20

#: Catálogo curado de fallback quando ``opencode models`` falha/sem rede.
#:
#: Fonte: modelos OpenRouter de uso recorrente na frota (DeepSeek barato p/ o
#: grosso; Claude/Qwen/Gemini/GPT premium sob demanda). IDs no formato nativo do
#: OpenCode (``provider/model``, onde provider=``openrouter`` aqui). A lista
#: dinâmica (``opencode models``) prevalece quando disponível; este catálogo só
#: garante que o picker do painel nunca fica vazio.
_FALLBACK_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="openrouter/deepseek/deepseek-chat",
        label="DeepSeek Chat (OpenRouter)",
        provider="openrouter",
        notes="barato; default recomendado p/ o grosso do trabalho",
    ),
    ModelInfo(
        id="openrouter/anthropic/claude-3.7-sonnet",
        label="Claude 3.7 Sonnet (OpenRouter)",
        provider="openrouter",
        notes="premium; review crítico / arquitetura",
    ),
    ModelInfo(
        id="openrouter/qwen/qwen3-coder",
        label="Qwen3 Coder (OpenRouter)",
        provider="openrouter",
        notes="bom custo-benefício p/ implementação",
    ),
    ModelInfo(
        id="openrouter/google/gemini-2.5-pro",
        label="Gemini 2.5 Pro (OpenRouter)",
        provider="openrouter",
    ),
    ModelInfo(
        id="openrouter/openai/gpt-4.1",
        label="GPT-4.1 (OpenRouter)",
        provider="openrouter",
    ),
]


class OpenCodeAdapter(BaseCliAdapter):
    """Adapter do OpenCode CLI (worker piloto)."""

    def build_argv(
        self,
        *,
        brief_path: str,
        model: Optional[str],
        reasoning: Optional[str],
        workdir: str,
        resume: Optional[ResumeCtx],
    ) -> List[str]:
        """Monta o argv headless do ``opencode run``.

        Forma: ``opencode run --dir <workdir> [-m <model>] [--variant <r>]
        --dangerously-skip-permissions --format json -f <brief_path>
        "<instrução posicional>"``.

        ``resume`` é ignorado (``supports_resume=False`` → o server sempre passa
        ``None``). A instrução posicional é curta e neutra-de-CLI; o conteúdo
        real vem do anexo ``-f``.
        """
        argv: List[str] = ["opencode", "run", "--dir", workdir]
        if model:
            argv += ["-m", model]
        # supports_reasoning=False → reasoning sempre None aqui; guarda defensiva.
        if reasoning:
            argv += ["--variant", reasoning]
        argv += [
            "--dangerously-skip-permissions",
            "--format", "json",
            "-f", brief_path,
            "Implemente exatamente o que o brief anexado descreve. "
            "Faça git add/commit/push das mudanças ao terminar.",
        ]
        return argv

    def env_overlay(self, *, home: str) -> dict:
        """Env do subprocess: HOME/XDG graváveis + config de autonomia inline.

        ``OPENCODE_CONFIG_CONTENT`` injeta a config (escopo "local") sem tocar
        disco — compatível com ``readOnlyRootFilesystem``. Os XDG apontam para
        baixo de ``home`` (gravável) para data/config/cache do OpenCode. NÃO
        inclui ``auth_env_keys`` (essas vêm do Secret montado no Deployment).
        """
        return {
            "HOME": home,
            "XDG_DATA_HOME": f"{home}/.local/share",
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_CACHE_HOME": f"{home}/.cache",
            "OPENCODE_CONFIG_CONTENT": json.dumps(
                _AUTONOMY_CONFIG, separators=(",", ":"),
            ),
        }

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Interpreta o NDJSON de ``--format json`` num :class:`WorkResult`.

        OpenCode emite uma linha JSON por evento (``type`` ∈ ``step_start``,
        ``step_finish``, ``tool_use``, ``text``, ...). O veredito do agente é o
        último evento ``text``; quando há erro estruturado (``type`` contendo
        ``error``), ``ok=False`` com o texto do erro. Exit-code é informativo
        apenas — o sucesso final é decidido pelo gate de commit/push do server
        (§1.6); aqui ``ok`` reflete só a leitura da saída.

        Tolerante a linhas malformadas (parse best-effort, ignora o que não for
        JSON). Sem nenhum evento textual e ``rc != 0`` → ``ok=False`` com tail
        do stderr.
        """
        last_text = ""
        error_text = ""
        saw_event = False

        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            saw_event = True
            etype = str(event.get("type", ""))
            if "error" in etype.lower():
                error_text = self._event_text(event) or error_text
            elif etype == "text" or "text" in event:
                txt = self._event_text(event)
                if txt:
                    last_text = txt

        if error_text:
            return WorkResult(
                ok=False,
                result_text=error_text[:2000],
                error_code="CLI_REPORTED_ERROR",
            )

        if last_text:
            return WorkResult(ok=True, result_text=last_text[:2000])

        # Sem nenhum evento parseável: não confia no rc, mas precisa de veredito.
        if saw_event:
            # Houve eventos (step_*/tool_use) mas nenhum texto — assume execução
            # plausível; o gate de git do server confirma commit/push.
            return WorkResult(
                ok=True,
                result_text="opencode concluiu sem veredito textual explícito",
            )
        tail = (stderr or stdout)[-2000:].strip()
        return WorkResult(
            ok=False,
            result_text=tail or f"opencode sem saída parseável (rc={rc})",
            error_code="NO_OUTPUT",
        )

    @staticmethod
    def _event_text(event: dict) -> str:
        """Extrai o texto de um evento NDJSON, tolerante ao shape.

        OpenCode aninha o conteúdo de formas diferentes por versão; tenta as
        chaves conhecidas em ordem e cai num ``str`` do payload se nada casar.
        """
        for key in ("text", "message", "content", "data"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict):
                nested = val.get("text") or val.get("content")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return ""

    def list_models(self) -> List[ModelInfo]:
        """Modelos suportados — dinâmico via ``opencode models``, com fallback.

        Roda ``opencode models`` (uma linha ``provider/model`` por modelo) e
        parseia. Se o binário não está no PATH, o comando falha, dá timeout ou
        não retorna nenhuma linha válida → cai no :data:`_FALLBACK_MODELS`
        curado. O ``cli_worker_server`` cacheia o resultado (TTL) — este método
        pode tocar a rede (models.dev).
        """
        dynamic = self._list_models_dynamic()
        return dynamic if dynamic else list(_FALLBACK_MODELS)

    @staticmethod
    def _list_models_dynamic() -> List[ModelInfo]:
        """Tenta listar via ``opencode models``; ``[]`` em qualquer falha."""
        if shutil.which("opencode") is None:
            return []
        try:
            proc = subprocess.run(
                ["opencode", "models"],
                capture_output=True,
                text=True,
                timeout=_MODELS_CMD_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("`opencode models` falhou: %s", exc)
            return []

        models: List[ModelInfo] = []
        seen: set = set()
        for raw in proc.stdout.splitlines():
            mid = raw.strip()
            # Linhas válidas têm o formato provider/model; descarta ruído.
            if not mid or "/" not in mid or mid in seen:
                continue
            if any(ch.isspace() for ch in mid):
                continue
            seen.add(mid)
            provider = mid.split("/", 1)[0]
            models.append(ModelInfo(id=mid, provider=provider))
        return models


#: Instância exportada — descoberta pelo registro (``cli_adapters.ADAPTERS``).
ADAPTER = OpenCodeAdapter(
    kind="opencode",
    default_port=8771,
    auth_mode="env",
    supports_resume=False,
    supports_reasoning=False,
    git_strategy="brief_driven",
    auth_env_keys=["OPENROUTER_API_KEY"],
    egress_hosts=["openrouter.ai", "models.dev"],
    writable_dirs=["HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"],
    oauth=None,
)


__all__ = ["OpenCodeAdapter", "ADAPTER"]
