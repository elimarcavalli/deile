"""
DEILE.md Hierarchical Loader — Core > Usuário > CWD

Implementa a leitura hierárquica de arquivos DEILE.md conforme a Issue #62:
  1. core/DEILE.md      — regras absolutas do pacote (não negociáveis)
  2. ~/.deile/DEILE.md   — preferências pessoais do usuário
  3. ./DEILE.md          — convenções do projeto atual

A ordem é fixa e imutável. As regras do Core nunca podem ser contraditas
pelas camadas inferiores.
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── Path resolution ────────────────────────────────────────────────

def _core_deile_md_path() -> Path:
    """Resolve o caminho do core/DEILE.md dentro do pacote DEILE."""
    # O arquivo vive em deile/personas/instructions/core/DEILE.md
    # relativo a este módulo: ../../personas/instructions/core/DEILE.md
    this_file = Path(__file__).resolve()
    # deile/core/deile_md_loader.py → deile/personas/instructions/core/DEILE.md
    return (this_file.parent.parent / "personas" / "instructions" / "core" / "DEILE.md").resolve()


def _user_deile_md_path() -> Path:
    """Resolve o caminho do ~/.deile/DEILE.md do usuário."""
    return Path.home() / ".deile" / "DEILE.md"


def _cwd_deile_md_path(working_directory: Optional[Path] = None) -> Path:
    """Resolve o caminho do ./DEILE.md no CWD."""
    cwd = working_directory or Path.cwd()
    return cwd / "DEILE.md"


# ── Reading helpers ─────────────────────────────────────────────────

def _read_if_exists(path: Path) -> Optional[str]:
    """Lê o conteúdo de um arquivo se ele existir; retorna None caso contrário."""
    try:
        if not path.is_file():
            return None
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        logger.debug("DEILE.md lido: %s (%d caracteres)", path, len(content))
        return content
    except Exception as exc:
        logger.warning("Não foi possível ler %s: %s", path, exc)
        return None


# ── Layer wrapper ───────────────────────────────────────────────────

class DEILEMDSource:
    """Representa uma camada de DEILE.md com metadados de origem."""

    def __init__(self, label: str, path: Path, content: str, priority: int):
        self.label = label       # "CORE", "USUÁRIO", "PROJETO"
        self.path = path         # Path absoluto do arquivo
        self.content = content   # Conteúdo bruto (markdown)
        self.priority = priority # 1=Core, 2=Usuário, 3=CWD


# ── Main loader ─────────────────────────────────────────────────────

class DEILEMDLoader:
    """Carrega e compõe as três camadas hierárquicas de DEILE.md."""

    def __init__(self, working_directory: Optional[Path] = None):
        self._working_directory = working_directory or Path.cwd()
        self._core_path = _core_deile_md_path()
        self._user_path = _user_deile_md_path()
        self._cwd_path = _cwd_deile_md_path(self._working_directory)

    # ── Public API ──────────────────────────────────────────────────

    def load_core(self) -> Optional[DEILEMDSource]:
        """Carrega o core/DEILE.md (camada 1 — não negociável)."""
        content = _read_if_exists(self._core_path)
        if content is None:
            logger.warning("core/DEILE.md não encontrado em %s", self._core_path)
            return None
        return DEILEMDSource(
            label="CORE",
            path=self._core_path,
            content=content,
            priority=1,
        )

    def load_user(self) -> Optional[DEILEMDSource]:
        """Carrega o ~/.deile/DEILE.md (camada 2 — preferências do usuário)."""
        content = _read_if_exists(self._user_path)
        if content is None:
            return None
        return DEILEMDSource(
            label="USUÁRIO",
            path=self._user_path,
            content=content,
            priority=2,
        )

    def load_cwd(self) -> Optional[DEILEMDSource]:
        """Carrega o ./DEILE.md do CWD (camada 3 — convenções do projeto)."""
        content = _read_if_exists(self._cwd_path)
        if content is None:
            return None
        return DEILEMDSource(
            label="PROJETO",
            path=self._cwd_path,
            content=content,
            priority=3,
        )

    def load_all(self) -> Tuple[Optional[DEILEMDSource], Optional[DEILEMDSource], Optional[DEILEMDSource]]:
        """Carrega as três camadas de uma vez.

        Returns:
            Tuple de (core, user, cwd) — cada um pode ser None se o arquivo não existir.
        """
        return (self.load_core(), self.load_user(), self.load_cwd())

    def build_merged_prompt(self) -> str:
        """Constrói o bloco de system prompt mesclando as três camadas.

        Ordem fixa: CORE → USUÁRIO → PROJETO.
        Cada camada é demarcada com sua origem e prioridade.

        Returns:
            String formatada para injeção no system prompt, ou string vazia
            se nenhuma camada existir (além do Core, que sempre deve existir).
        """
        core, user, cwd = self.load_all()
        parts: list[str] = []

        # ── Camada 1: CORE (sempre presente) ──
        if core is not None:
            parts.append(
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║  🔴 CAMADA 1/3 — REGRAS ABSOLUTAS DO CORE (NÃO NEGOCIÁVEIS)  ║\n"
                "║  Fonte: core/DEILE.md (shippado com o pacote DEILE)           ║\n"
                "║  PRIORIDADE MÁXIMA — nenhuma camada inferior pode contradizer ║\n"
                "╚══════════════════════════════════════════════════════════════╝\n\n"
                + core.content
            )
        else:
            # Isso não deveria acontecer em produção — core/DEILE.md sempre shipa.
            logger.error("CRÍTICO: core/DEILE.md ausente! As regras absolutas não serão injetadas.")

        # ── Separador entre camadas ──
        sep = "\n\n" + "─" * 66 + "\n\n"

        # ── Camada 2: USUÁRIO (opcional) ──
        if user is not None:
            parts.append(
                sep
                + "╔══════════════════════════════════════════════════════════════╗\n"
                "║  🟡 CAMADA 2/3 — PREFERÊNCIAS DO USUÁRIO                      ║\n"
                "║  Fonte: ~/.deile/DEILE.md (comum a todos os projetos)         ║\n"
                "║  Estas preferências NÃO podem contradizer as regras do Core   ║\n"
                "╚══════════════════════════════════════════════════════════════╝\n\n"
                + user.content
            )

        # ── Camada 3: PROJETO (opcional) ──
        if cwd is not None:
            parts.append(
                sep
                + "╔══════════════════════════════════════════════════════════════╗\n"
                "║  🟢 CAMADA 3/3 — CONVENÇÕES DO PROJETO                        ║\n"
                "║  Fonte: ./DEILE.md (específico deste projeto)                 ║\n"
                "║  Estas convenções NÃO podem contradizer as regras do Core     ║\n"
                "╚══════════════════════════════════════════════════════════════╝\n\n"
                + cwd.content
            )

        if not parts:
            return ""

        # Fechamento com separador para demarcar o fim das camadas e início
        # da instrução de persona que vem em seguida.
        parts.append(
            "\n\n"
            + "═" * 66
            + "\n"
            + "🔽 FIM DAS CAMADAS DEILE.md — a seguir, instruções da persona ativa 🔽\n"
            + "═" * 66
        )

        return "\n".join(parts)

    def get_stats(self) -> dict:
        """Retorna estatísticas sobre as camadas carregadas."""
        core, user, cwd = self.load_all()
        return {
            "core": {
                "loaded": core is not None,
                "path": str(self._core_path),
                "size": len(core.content) if core else 0,
            },
            "user": {
                "loaded": user is not None,
                "path": str(self._user_path),
                "size": len(user.content) if user else 0,
            },
            "cwd": {
                "loaded": cwd is not None,
                "path": str(self._cwd_path),
                "size": len(cwd.content) if cwd else 0,
            },
            "working_directory": str(self._working_directory),
        }
