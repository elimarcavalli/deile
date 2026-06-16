"""DEILE.md Hierarchical Loader — Core > Usuário > CWD (Issue #62 / Feature #64).

Lê e mescla três camadas de DEILE.md em ordem fixa e imutável:
  1. core/DEILE.md       — regras absolutas do pacote (não negociáveis)
  2. ~/.deile/DEILE.md   — preferências pessoais do usuário
  3. ./DEILE.md          — convenções do projeto atual

As regras do Core nunca podem ser contraditas pelas camadas inferiores.

Caching: cada camada é memoizada por (path, mtime) num cache de processo —
relê só quando o arquivo muda, mantendo o efeito de "hot-reload barato".
Tamanho máximo por camada: `Settings.deile_md_max_bytes` (default 64KB).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from deile.config.settings import get_settings

logger = logging.getLogger(__name__)


# ── Path resolution ─────────────────────────────────────────────────────────


def _core_deile_md_path() -> Path:
    """Resolve o caminho do core/DEILE.md dentro do pacote DEILE."""
    return (
        Path(__file__).resolve().parent.parent
        / "personas"
        / "instructions"
        / "core"
        / "DEILE.md"
    ).resolve()


def _user_deile_md_path() -> Path:
    """Resolve o caminho do DEILE.md do usuário (override via Settings)."""
    override = getattr(get_settings(), "deile_md_user_path", None)
    if override:
        return Path(override)
    return Path.home() / ".deile" / "DEILE.md"


def _cwd_deile_md_path(working_directory: Optional[Path] = None) -> Path:
    """Resolve o caminho do DEILE.md no CWD (filename via Settings)."""
    filename = getattr(get_settings(), "deile_md_cwd_filename", None) or "DEILE.md"
    return (working_directory or Path.cwd()) / filename


# ── Reading helpers ─────────────────────────────────────────────────────────


def _max_layer_bytes() -> int:
    return int(getattr(get_settings(), "deile_md_max_bytes", 64 * 1024))


def _read_if_exists(path: Path) -> Optional[str]:
    """Lê o conteúdo de um arquivo se existir; trunca acima do limite.

    Retorna None se: ausente, vazio (após strip), ou erro de leitura.
    """
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size == 0:
            return None
        cap = _max_layer_bytes()
        if size > cap:
            logger.warning(
                "DEILE.md %s excede %d bytes (atual: %d); truncando.",
                path,
                cap,
                size,
            )
            content = path.read_bytes()[:cap].decode("utf-8", errors="replace")
        else:
            content = path.read_text(encoding="utf-8")
        content = content.strip()
        return content if content else None
    except Exception as exc:
        logger.warning("Não foi possível ler %s: %s", path, exc)
        return None


# Cache de processo: path -> (mtime, content_or_none).
# Memoiza por mtime para evitar releitura a cada turno; muda no arquivo,
# muda o mtime, cache invalida automaticamente.
_CACHE: Dict[str, Tuple[float, Optional[str]]] = {}


def _read_cached(path: Path) -> Optional[str]:
    key = str(path)
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except Exception:
        mtime = 0.0
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    content = _read_if_exists(path)
    _CACHE[key] = (mtime, content)
    return content


def clear_cache() -> None:
    """Esvazia o cache de leituras (uso primário: testes)."""
    _CACHE.clear()


# ── Layer wrapper ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DEILEMDSource:
    """Representa uma camada de DEILE.md com metadados de origem."""

    label: str  # "CORE", "USUÁRIO", "PROJETO"
    path: Path
    content: str
    priority: int  # 1=Core, 2=Usuário, 3=CWD


# ── Main loader ─────────────────────────────────────────────────────────────


# Headers compactos: priorizam token-economy mantendo demarcação clara
# de origem, número de camada e relação de autoridade.
_LAYER_HEADERS = {
    "CORE": (
        "[DEILE.md CAMADA 1/3 — CORE — NÃO NEGOCIÁVEIS, " "fonte: pacote core/DEILE.md]"
    ),
    "USUÁRIO": (
        "[DEILE.md CAMADA 2/3 — USUÁRIO — não podem contradizer o CORE, "
        "fonte: ~/.deile/DEILE.md]"
    ),
    "PROJETO": (
        "[DEILE.md CAMADA 3/3 — PROJETO — não podem contradizer o CORE, "
        "fonte: ./DEILE.md]"
    ),
}
_CLOSING = "[FIM DAS CAMADAS DEILE.md — instruções da persona a seguir]"


class DEILEMDLoader:
    """Carrega e compõe as três camadas hierárquicas de DEILE.md."""

    def __init__(self, working_directory: Optional[Path] = None):
        self._working_directory = working_directory or Path.cwd()
        self._core_path = _core_deile_md_path()
        self._user_path = _user_deile_md_path()
        self._cwd_path = _cwd_deile_md_path(self._working_directory)

    # ── Public API ──────────────────────────────────────────────────────

    def load_core(self) -> Optional[DEILEMDSource]:
        return self._load_layer("CORE", self._core_path, 1, log_missing=True)

    def load_user(self) -> Optional[DEILEMDSource]:
        return self._load_layer("USUÁRIO", self._user_path, 2)

    def load_cwd(self) -> Optional[DEILEMDSource]:
        return self._load_layer("PROJETO", self._cwd_path, 3)

    def load_all(
        self,
    ) -> Tuple[
        Optional[DEILEMDSource], Optional[DEILEMDSource], Optional[DEILEMDSource]
    ]:
        """Carrega as três camadas. Cada slot pode ser None se ausente."""
        return (self.load_core(), self.load_user(), self.load_cwd())

    def build_merged_prompt(self) -> str:
        """Monta o bloco mesclado para injeção no system prompt.

        Ordem fixa: CORE → USUÁRIO → PROJETO. Cada camada presente é
        prefixada com seu header de demarcação. Se nenhuma camada existir
        (nem mesmo o Core), retorna string vazia.
        """
        if not getattr(get_settings(), "deile_md_enabled", True):
            return ""

        sources = [s for s in self.load_all() if s is not None]
        if not sources:
            return ""

        parts: List[str] = [
            f"{_LAYER_HEADERS[src.label]}\n{src.content}" for src in sources
        ]
        parts.append(_CLOSING)
        return "\n\n".join(parts)

    def get_stats(self) -> dict:
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

    # ── Internals ───────────────────────────────────────────────────────

    def _load_layer(
        self,
        label: str,
        path: Path,
        priority: int,
        log_missing: bool = False,
    ) -> Optional[DEILEMDSource]:
        content = _read_cached(path)
        if content is None:
            if log_missing:
                logger.warning("%s/DEILE.md não encontrado em %s", label.lower(), path)
            return None
        return DEILEMDSource(label=label, path=path, content=content, priority=priority)
