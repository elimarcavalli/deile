#!/usr/bin/env python3
"""cli_adapters.antigravity — adapter do Antigravity ``agy`` (Tier 3, ⚠️ GATED).

ESTE MÓDULO NÃO REGISTRA UM ADAPTER FUNCIONAL — é um gate documentado (DECISÃO #51).

O spike obrigatório (plano §2.6 / Fase E1) ainda não foi executado. As pré-condições
para um worker headless determinístico não estão provadas:

* **Closed-source** — flags do binário não confirmadas; doc oficial é JS-only.
* **Auth headless por API-key não suportada no consumer** (issue oficial #78 aberta).
* **Login padrão = Google OAuth com cred no keyring** — inviável em container
  ``readOnlyRootFilesystem`` sem DBus/keyring. Não confirmado se ``agy`` lê cred de
  arquivo mountável.
* **Google-locked** e ``--print`` sem conversation-ID por chamada (issue #7) —
  one-shot determinístico não provado.

Rotas de auth a provar no spike (ordem de preferência):

1. Vertex/Gemini Enterprise service-account JSON (``GOOGLE_APPLICATION_CREDENTIALS``
   + ``GOOGLE_CLOUD_PROJECT`` via Secret) — rota preferida; não expira.
2. Google OAuth file mountável via ``deploy.py k8s antigravity-login`` — só se o
   spike confirmar que o ``agy`` aceita arquivo (não só keyring).
3. OAuth keyring — inviável em pod.

Enquanto nenhuma rota for confirmada, Gemini é servido via OpenRouter
(``openrouter/google/gemini-*``) ou via ``deile-worker`` (provider ``google``).
Reavaliar quando issue #78 fechar.

Este módulo existe para manter o gate versionado junto do código (não só na doc) e
pré-modelar OAuthSpec/argv para retomada com custo baixo. O auto-discovery
(``cli_adapters/__init__.py``) ignora este módulo porque não exporta ``ADAPTER`` nem
``get_adapter()`` — ``antigravity-worker`` não é um dispatcher válido.

Liberação (quando spike Fase E1 passar):
1. Ajustar :class:`_AntigravityAdapterDraft` conforme ``agy --help`` OBSERVADO e a
   rota de auth confirmada.
2. Exportar ``ADAPTER = _AntigravityAdapterDraft(...)`` (``default_port=8776``, §1.13).
3. Setar :data:`ANTIGRAVITY_GATED` = ``False`` e atualizar DECISÃO #51.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .base import BaseCliAdapter, ModelInfo, OAuthSpec, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.antigravity")

#: Gate fechado (spike Fase E1 pendente). Enquanto True, nenhuma instância de
#: adapter é exportada → auto-discovery ignora o módulo.
ANTIGRAVITY_GATED: bool = True

#: Porta reservada para quando o gate for liberado (§1.13).
ANTIGRAVITY_RESERVED_PORT: int = 8776

#: Catálogo pré-modelado (Google-locked) — só entra em uso quando o gate liberar.
_DRAFT_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gemini-3.1-pro",
        label="Gemini 3.1 Pro (Antigravity)",
        provider="google",
        notes="GATED — disponível só após spike Fase E1 confirmar auth headless",
    ),
]

#: OAuthSpec pré-modelado (rota 2 — OAuth file). Só vira ``oauth=`` do adapter se
#: o spike confirmar que o ``agy`` lê cred de arquivo, não do keyring.
_DRAFT_OAUTH = OAuthSpec(
    cred_path="~/.gemini/oauth_creds.json",
    login_cmd=["agy", "auth", "login", "--device"],
    secret_name="antigravity-credentials",
    renewable=False,
)


class _AntigravityAdapterDraft(BaseCliAdapter):
    """Rascunho do adapter Antigravity — NÃO instanciado/registrado (gated).

    Argv/parse modelados pela doc §2.6, não pelo binário observado — permanecem
    rascunho até o spike. ``--dangerously-skip-permissions`` ainda não confirmado.
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
        """Rascunho do argv ``agy`` headless — flags a confirmar contra o binário."""
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
        """Rascunho do env; rota de auth definitiva definida pelo spike."""
        return {
            "HOME": home,
            "GEMINI_CONFIG_DIR": f"{home}/.gemini",
        }

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Rascunho de parse — shape real da saída a confirmar no spike."""
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


# Não exportar ADAPTER/get_adapter enquanto ANTIGRAVITY_GATED — mantém o módulo
# fora do registro. Instanciar conforme o cabeçalho quando o spike (Fase E1) liberar.

__all__ = [
    "ANTIGRAVITY_GATED",
    "ANTIGRAVITY_RESERVED_PORT",
]
