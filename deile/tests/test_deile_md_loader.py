"""Unit tests for DEILEMDLoader (Issue #62 / Feature #64).

Cobre a leitura hierárquica das três camadas de DEILE.md:
    1. core/DEILE.md
    2. ~/.deile/DEILE.md
    3. ./DEILE.md

E a composição do bloco de system prompt em ordem fixa CORE → USUÁRIO → PROJETO.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from deile.core.deile_md_loader import (DEILEMDLoader, DEILEMDSource,
                                        _read_if_exists, clear_cache)


@pytest.fixture(autouse=True)
def _isolate_loader_cache():
    """Garante que cada teste comece com cache limpo."""
    clear_cache()
    yield
    clear_cache()


# ── Fixtures ────────────────────────────────────────────────────────────────

CORE_MARKER = "CORE_RULE: nunca minta sobre paths."
USER_MARKER = "USER_RULE: sempre saudar com cupinxa."
CWD_MARKER = "CWD_RULE: sempre dar receita de bolo."


@pytest.fixture
def tmp_layout(tmp_path, monkeypatch):
    """Cria um layout isolado de three-layer DEILE.md para um cenário de teste.

    Yields uma factory que aceita keywords (`core`, `user`, `cwd`) com o conteúdo
    desejado em cada camada (None = arquivo ausente, "" = arquivo vazio).
    """

    home_dir = tmp_path / "home"
    cwd_dir = tmp_path / "project"
    core_dir = tmp_path / "pkg" / "personas" / "instructions" / "core"

    home_dir.mkdir(parents=True)
    cwd_dir.mkdir(parents=True)
    core_dir.mkdir(parents=True)

    core_path = core_dir / "DEILE.md"
    user_path = home_dir / ".deile" / "DEILE.md"
    user_path.parent.mkdir(parents=True)
    cwd_path = cwd_dir / "DEILE.md"

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))

    def _write(path: Path, content):
        if content is None:
            return
        path.write_text(content, encoding="utf-8")

    def factory(core=CORE_MARKER, user=USER_MARKER, cwd=CWD_MARKER):
        _write(core_path, core)
        _write(user_path, user)
        _write(cwd_path, cwd)

        loader = DEILEMDLoader(working_directory=cwd_dir)
        # Override do core path para apontar ao layout temporário.
        loader._core_path = core_path
        return loader

    return factory


# ── _read_if_exists ─────────────────────────────────────────────────────────


def test_read_if_exists_returns_content(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("hello", encoding="utf-8")
    assert _read_if_exists(p) == "hello"


def test_read_if_exists_returns_none_when_missing(tmp_path):
    assert _read_if_exists(tmp_path / "missing.md") is None


def test_read_if_exists_returns_none_for_empty_file(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("   \n  \n", encoding="utf-8")
    assert _read_if_exists(p) is None


def test_read_if_exists_returns_none_on_read_error(tmp_path):
    target = tmp_path / "x.md"
    target.write_text("data", encoding="utf-8")
    with patch("pathlib.Path.read_text", side_effect=PermissionError("denied")):
        assert _read_if_exists(target) is None


# ── DEILEMDSource ───────────────────────────────────────────────────────────


def test_source_dataclass_holds_metadata(tmp_path):
    src = DEILEMDSource(label="CORE", path=tmp_path / "x.md", content="rules", priority=1)
    assert src.label == "CORE"
    assert src.priority == 1
    assert src.content == "rules"


# ── DEILEMDLoader: load_* methods ───────────────────────────────────────────


def test_load_all_three_layers_present(tmp_layout):
    loader = tmp_layout()
    core, user, cwd = loader.load_all()

    assert core is not None and core.label == "CORE" and core.priority == 1
    assert user is not None and user.label == "USUÁRIO" and user.priority == 2
    assert cwd is not None and cwd.label == "PROJETO" and cwd.priority == 3
    assert CORE_MARKER in core.content
    assert USER_MARKER in user.content
    assert CWD_MARKER in cwd.content


def test_load_all_only_core(tmp_layout):
    loader = tmp_layout(user=None, cwd=None)
    core, user, cwd = loader.load_all()
    assert core is not None
    assert user is None
    assert cwd is None


def test_load_all_core_plus_user(tmp_layout):
    loader = tmp_layout(cwd=None)
    core, user, cwd = loader.load_all()
    assert core is not None and user is not None and cwd is None


def test_load_all_core_plus_cwd(tmp_layout):
    loader = tmp_layout(user=None)
    core, user, cwd = loader.load_all()
    assert core is not None and cwd is not None and user is None


def test_load_all_no_layers(tmp_layout):
    loader = tmp_layout(core=None, user=None, cwd=None)
    core, user, cwd = loader.load_all()
    assert core is None and user is None and cwd is None


def test_empty_file_treated_as_absent(tmp_layout):
    loader = tmp_layout(user="   \n", cwd="")
    core, user, cwd = loader.load_all()
    assert core is not None
    assert user is None
    assert cwd is None


def test_working_directory_override(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    (tmp_path / "h").mkdir()

    project_a = tmp_path / "A"
    project_b = tmp_path / "B"
    project_a.mkdir()
    project_b.mkdir()
    (project_a / "DEILE.md").write_text("ALPHA", encoding="utf-8")
    (project_b / "DEILE.md").write_text("BETA", encoding="utf-8")

    loader_a = DEILEMDLoader(working_directory=project_a)
    loader_b = DEILEMDLoader(working_directory=project_b)

    cwd_a = loader_a.load_cwd()
    cwd_b = loader_b.load_cwd()
    assert cwd_a is not None and "ALPHA" in cwd_a.content
    assert cwd_b is not None and "BETA" in cwd_b.content


def test_default_working_directory_is_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    (tmp_path / "h").mkdir()
    monkeypatch.chdir(tmp_path)
    loader = DEILEMDLoader()
    assert loader._working_directory == tmp_path


# ── DEILEMDLoader: build_merged_prompt ──────────────────────────────────────


def test_merged_prompt_keeps_fixed_order_core_user_cwd(tmp_layout):
    loader = tmp_layout()
    out = loader.build_merged_prompt()

    pos_core = out.find(CORE_MARKER)
    pos_user = out.find(USER_MARKER)
    pos_cwd = out.find(CWD_MARKER)

    assert pos_core != -1 and pos_user != -1 and pos_cwd != -1
    assert pos_core < pos_user < pos_cwd, "ordem deve ser CORE → USUÁRIO → PROJETO"


def test_merged_prompt_each_layer_has_demarcation(tmp_layout):
    loader = tmp_layout()
    out = loader.build_merged_prompt()

    assert "CAMADA 1/3" in out
    assert "CAMADA 2/3" in out
    assert "CAMADA 3/3" in out
    assert "CORE" in out and "USUÁRIO" in out and "PROJETO" in out


def test_merged_prompt_includes_closing_separator(tmp_layout):
    loader = tmp_layout()
    out = loader.build_merged_prompt()
    assert "FIM DAS CAMADAS DEILE.md" in out


def test_merged_prompt_only_core_skips_optional_layers(tmp_layout):
    loader = tmp_layout(user=None, cwd=None)
    out = loader.build_merged_prompt()

    assert "CAMADA 1/3" in out
    assert "CAMADA 2/3" not in out
    assert "CAMADA 3/3" not in out
    assert "FIM DAS CAMADAS DEILE.md" in out


def test_merged_prompt_keeps_order_when_user_missing(tmp_layout):
    loader = tmp_layout(user=None)
    out = loader.build_merged_prompt()

    pos_core = out.find(CORE_MARKER)
    pos_cwd = out.find(CWD_MARKER)
    assert pos_core < pos_cwd
    assert "CAMADA 2/3" not in out
    assert "CAMADA 3/3" in out


def test_merged_prompt_returns_empty_when_no_layers(tmp_layout):
    loader = tmp_layout(core=None, user=None, cwd=None)
    out = loader.build_merged_prompt()
    assert out == ""


def test_merged_prompt_marks_core_as_non_negotiable(tmp_layout):
    loader = tmp_layout()
    out = loader.build_merged_prompt()
    assert "NÃO NEGOCIÁVEIS" in out
    # As camadas opcionais devem trazer aviso de subordinação
    assert "não podem contradizer" in out.lower() or "nao podem contradizer" in out.lower()


def test_merged_prompt_disabled_via_settings_returns_empty(tmp_layout, monkeypatch):
    from deile.config import settings as settings_module

    settings = settings_module.get_settings()
    monkeypatch.setattr(settings, "deile_md_enabled", False)

    loader = tmp_layout()
    assert loader.build_merged_prompt() == ""


def test_settings_override_user_path(tmp_path, monkeypatch):
    from deile.config import settings as settings_module

    custom_user_dir = tmp_path / "custom-user"
    custom_user_dir.mkdir()
    custom_user_md = custom_user_dir / "MY_DEILE.md"
    custom_user_md.write_text("CUSTOM_USER_CONTENT", encoding="utf-8")

    settings = settings_module.get_settings()
    monkeypatch.setattr(settings, "deile_md_user_path", custom_user_md)

    loader = DEILEMDLoader(working_directory=tmp_path)
    user = loader.load_user()
    assert user is not None
    assert "CUSTOM_USER_CONTENT" in user.content


def test_settings_override_cwd_filename(tmp_path, monkeypatch):
    from deile.config import settings as settings_module

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()

    project = tmp_path / "p"
    project.mkdir()
    (project / "RULES.md").write_text("CUSTOM_CWD_CONTENT", encoding="utf-8")

    settings = settings_module.get_settings()
    monkeypatch.setattr(settings, "deile_md_cwd_filename", "RULES.md")

    loader = DEILEMDLoader(working_directory=project)
    cwd = loader.load_cwd()
    assert cwd is not None
    assert "CUSTOM_CWD_CONTENT" in cwd.content


def test_large_file_is_truncated(tmp_path, monkeypatch):
    from deile.config import settings as settings_module

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    (tmp_path / "h").mkdir()

    settings = settings_module.get_settings()
    monkeypatch.setattr(settings, "deile_md_max_bytes", 100)

    huge = tmp_path / "huge.md"
    huge.write_text("X" * 5000, encoding="utf-8")
    content = _read_if_exists(huge)
    assert content is not None
    assert len(content) <= 100


def test_cache_avoids_rereads_same_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    (tmp_path / "h").mkdir()

    target = tmp_path / "x.md"
    target.write_text("v1", encoding="utf-8")

    from deile.core import deile_md_loader as mod

    call_counter = {"n": 0}
    real_read_if_exists = mod._read_if_exists

    def counted(path):
        if str(path) == str(target):
            call_counter["n"] += 1
        return real_read_if_exists(path)

    monkeypatch.setattr(mod, "_read_if_exists", counted)

    assert mod._read_cached(target) == "v1"
    assert mod._read_cached(target) == "v1"
    assert mod._read_cached(target) == "v1"
    # Apenas uma leitura efetiva — as outras vieram do cache.
    assert call_counter["n"] == 1


def test_cache_invalidates_when_mtime_changes(tmp_path):
    import os
    target = tmp_path / "x.md"
    target.write_text("v1", encoding="utf-8")

    from deile.core import deile_md_loader as mod
    assert mod._read_cached(target) == "v1"

    target.write_text("v2", encoding="utf-8")
    # Garante mtime distinto
    os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 1.0))
    assert mod._read_cached(target) == "v2"


# ── get_stats ───────────────────────────────────────────────────────────────


def test_get_stats_reports_loaded_state(tmp_layout):
    loader = tmp_layout()
    stats = loader.get_stats()

    assert stats["core"]["loaded"] is True
    assert stats["user"]["loaded"] is True
    assert stats["cwd"]["loaded"] is True
    assert stats["core"]["size"] > 0
    assert "working_directory" in stats


def test_get_stats_when_layers_missing(tmp_layout):
    loader = tmp_layout(user=None, cwd=None)
    stats = loader.get_stats()
    assert stats["core"]["loaded"] is True
    assert stats["user"]["loaded"] is False
    assert stats["cwd"]["loaded"] is False
    assert stats["user"]["size"] == 0
