"""Internal helper for orchestration data-directory resolution.

Centraliza a heurística (antes duplicada em ``plan_manager`` e
``sqlite_task_manager``) de escolher entre um diretório/arquivo *legacy*
na raiz do projeto e o destino atual sob ``.deile/``. Helper interno do
subpacote ``orchestration`` — não exposto por nenhum registry.
"""

from pathlib import Path

__all__ = ["resolve_data_dir"]


def resolve_data_dir(
    legacy_name: str,
    new_relative: str,
    *,
    require_nonempty: bool = True,
) -> Path:
    """Resolve um caminho de dados preferindo o legacy só quando ainda em uso.

    Constrói ambos os caminhos relativos a ``Path.cwd()`` e devolve o
    *legacy* apenas se ele ainda existir e o destino novo não — caso
    contrário devolve o caminho novo. A função apenas **resolve** o
    caminho; nunca cria diretórios (criação fica a cargo do call-site).

    Args:
        legacy_name: Nome do diretório/arquivo legacy, relativo ao cwd.
        new_relative: Caminho novo (sob ``.deile/``), relativo ao cwd.
        require_nonempty: Quando ``True`` (default), o legacy só é
            escolhido se for um diretório existente e não-vazio. Quando
            ``False``, basta que o legacy exista (variante usada para
            arquivos, ex.: um ``.db``).

    Returns:
        O ``Path`` legacy se ele estiver em uso e o novo não existir,
        senão o ``Path`` novo.
    """
    cwd = Path.cwd()
    legacy = cwd / legacy_name
    new = cwd / new_relative

    if require_nonempty:
        use_legacy = legacy.is_dir() and any(legacy.iterdir()) and not new.exists()
    else:
        use_legacy = legacy.exists() and not new.exists()

    return legacy if use_legacy else new
