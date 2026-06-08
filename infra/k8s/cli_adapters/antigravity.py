#!/usr/bin/env python3
"""cli_adapters.antigravity — adapter do Antigravity ``agy`` (Tier 3, ⚠️ GATED).

**ESTE MÓDULO NÃO REGISTRA UM ADAPTER FUNCIONAL — É UM GATE DOCUMENTADO.**

O Antigravity (Google) está GATED por um spike obrigatório (plano §2.6 / Fase
E1) que precisa rodar ANTES de qualquer adapter/manifest. O spike NÃO foi
executado (Fase 5 é mock-level, E2E/spike deferidos) e, com o que se sabe hoje,
as pré-condições para um worker headless determinístico **não estão provadas**:

* **Closed-source.** Sem fonte para confirmar flags/comportamento contra o
  binário; a doc oficial é JS-only e as flags não são confirmadas literalmente.
* **Auth headless por API-key NÃO suportada no consumer** (issue oficial #78
  aberta) — não dá para contar com um simples ``GEMINI_API_KEY``.
* **Login padrão = Google OAuth com cred no keyring** — hostil a container
  ``readOnlyRootFilesystem`` sem DBus/keyring. Não se sabe (sem spike) se o
  ``agy`` lê cred de **arquivo** mountável.
* **Google-locked** (modelos não-Google só via harness Google) e ``--print`` sem
  conversation-ID por chamada (issue #7) — one-shot determinístico não provado.

**Decisão (registrada também em ``docs/system_design/DECISOES.md`` #51):** NÃO
implementar o adapter funcional enquanto o spike (Fase E1) não provar **pelo
menos uma** rota de auth headless viável no pod, em ordem de preferência:

1. **Vertex/Gemini Enterprise service-account JSON** (``auth_mode=env``:
   ``GOOGLE_APPLICATION_CREDENTIALS`` + ``GOOGLE_CLOUD_PROJECT`` via Secret) —
   robusta, não expira como OAuth consumer. **Rota preferida.**
2. **Google OAuth file mountável** (``auth_mode=oauth_file`` via
   ``deploy.py k8s antigravity-login``) — só se o spike provar que o ``agy`` lê
   cred de arquivo (não só keyring).
3. **OAuth keyring** — **inviável** em pod (sem DBus/keyring).

Enquanto nenhuma rota for confirmada, **Gemini é servido por outros caminhos já
existentes**: via OpenRouter (``openrouter/google/gemini-*`` no opencode/aider/
goose) ou via ``deile-worker`` (provider ``google`` nativo). Reavaliar quando a
issue #78 (API-key headless consumer) fechar.

**Por que este módulo existe mesmo sem registrar um adapter:** mantém a decisão
do gate versionada JUNTO do código da frota (e não só na doc), deixa o
:class:`OAuthSpec`/argv pré-modelados para quando o spike liberar (custo de
retomada baixo), e documenta explicitamente para o próximo operador POR QUE não
há ``antigravity-worker``. O registro de auto-discovery
(``cli_adapters/__init__.py``) **ignora este módulo** porque ele NÃO expõe um
``ADAPTER`` nem ``get_adapter()`` nem uma instância que satisfaça o Protocol —
apenas a CLASSE (não instanciada) + a flag :data:`ANTIGRAVITY_GATED`. Logo
``antigravity-worker`` **não** vira um dispatcher válido, **não** quebra o
``k8s up`` e **não** aparece no painel até o gate ser liberado.

----

Quando o spike (Fase E1) passar, a liberação é mecânica:

1. Ajustar :class:`_AntigravityAdapterDraft` conforme o ``agy --help`` OBSERVADO
   (não a doc) e a rota de auth que o spike confirmar.
2. Instanciar e exportar ``ADAPTER = _AntigravityAdapterDraft(...)`` (com
   ``default_port=8776``, §1.13) — só isso já o registra (auto-discovery) e o
   torna dispatcher/painel/manifest sem editar consumidor.
3. Trocar :data:`ANTIGRAVITY_GATED` para ``False`` e atualizar a DECISÃO #51.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .base import BaseCliAdapter, ModelInfo, OAuthSpec, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.antigravity")

#: Sentinela explícita: o worker antigravity está GATED (spike Fase E1 pendente).
#: Lida por testes/operador para afirmar que o gate está fechado; enquanto
#: ``True``, NENHUMA instância de adapter é exportada → o registro ignora este
#: módulo e ``antigravity-worker`` não é um dispatcher válido.
ANTIGRAVITY_GATED: bool = True

#: Porta reservada para quando o gate for liberado (§1.13).
ANTIGRAVITY_RESERVED_PORT: int = 8776

#: Catálogo estático pré-modelado (Google-locked). Só entra em uso quando o gate
#: liberar; mantido aqui para custo de retomada baixo.
_DRAFT_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gemini-3.1-pro",
        label="Gemini 3.1 Pro (Antigravity)",
        provider="google",
        notes="GATED — disponível só após spike Fase E1 confirmar auth headless",
    ),
]

#: OAuthSpec pré-modelado da rota (2) — Google OAuth file mountável. Só vira
#: ``oauth=`` real do adapter quando o spike confirmar que o ``agy`` lê cred de
#: arquivo. NÃO é referenciado por nenhum adapter ativo enquanto o gate fechado.
_DRAFT_OAUTH = OAuthSpec(
    cred_path="~/.gemini/oauth_creds.json",
    login_cmd=["agy", "auth", "login", "--device"],
    secret_name="antigravity-credentials",
    renewable=False,
)


class _AntigravityAdapterDraft(BaseCliAdapter):
    """Rascunho do adapter Antigravity — NÃO instanciado/registrado (gated).

    Modela o argv/parse esperados pela doc §2.6, mas é deliberadamente uma
    CLASSE não-instanciada: o registro de auto-discovery só reconhece INSTÂNCIAS
    que satisfaçam o Protocol (via ``ADAPTER``/``get_adapter()``/varredura de
    atributos), então esta classe pura nunca é registrada. Existe para que, ao
    liberar o gate, baste instanciá-la e exportá-la como ``ADAPTER``.

    As flags aqui refletem a DOC (§2.6), NÃO o ``agy --help`` observado — por isso
    permanecem rascunho até o spike. ``--dangerously-skip-permissions`` é
    explicitamente marcado como não-confirmado oficialmente.
    """

    def build_argv(
        self,
        *,
        brief_path: str,
        model: Optional[str],
        reasoning: Optional[str],
        workdir: str,
        resume: Optional[ResumeCtx],
        task_id: str = "",
    ) -> List[str]:
        """Rascunho do argv ``agy`` headless (a confirmar contra o binário)."""
        brief_text = self._read_brief(brief_path)
        argv: List[str] = ["agy", "-p", brief_text]
        if model:
            argv += ["-m", model]
        argv += [
            # ⚠️ §2.6: não-confirmado oficialmente — validar no spike.
            "--dangerously-skip-permissions",
            "--output-format", "json",
        ]
        return argv

    @staticmethod
    def _read_brief(brief_path: str) -> str:
        try:
            with open(brief_path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            logger.warning("não consegui ler o brief %r: %s", brief_path, exc)
            return (
                f"Leia o brief em {brief_path} e implemente exatamente o que ele "
                "descreve. Faça git add/commit/push das mudanças ao terminar."
            )

    def env_overlay(self, *, home: str) -> dict:
        """Rascunho do env (HOME + ~/.gemini); rota de auth definida pelo spike."""
        return {
            "HOME": home,
            "GEMINI_CONFIG_DIR": f"{home}/.gemini",
        }

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Rascunho de parse (a confirmar o shape real da saída no spike)."""
        tail = (stdout or stderr)[-2000:].strip()
        if rc != 0 and not tail:
            return WorkResult(
                ok=False,
                result_text=f"agy sem saída parseável (rc={rc})",
                error_code="NO_OUTPUT",
            )
        return WorkResult(ok=bool(tail), result_text=tail)

    def list_models(self) -> List[ModelInfo]:
        return list(_DRAFT_MODELS)


# NÃO exportar ``ADAPTER`` nem ``get_adapter`` enquanto ``ANTIGRAVITY_GATED`` for
# True — é exatamente o que mantém o módulo fora do registro. Quando o spike
# (Fase E1) liberar, descomentar/instanciar conforme o cabeçalho deste arquivo.

__all__ = [
    "ANTIGRAVITY_GATED",
    "ANTIGRAVITY_RESERVED_PORT",
]
