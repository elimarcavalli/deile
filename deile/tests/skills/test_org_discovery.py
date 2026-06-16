"""Testes de integração — skills da org no scan-order (issue #741).

Cobre:
- AC: skill de org é descoberta e sua precedência sombrea user, mas é sombreada por project.
- AC: backward-compat — sem org_paths, comportamento idêntico ao baseline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.skills.discovery import ScanEntry, default_scan_order, discover_skills_sync


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKILL_TMPL = """\
---
name: {name}
description: {name} skill
---
# {name}
content from {source}
"""


def _write_skill(directory: Path, name: str, source: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        _SKILL_TMPL.format(name=name, source=source), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Testes da função default_scan_order
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDefaultScanOrderOrgEntry:
    def test_no_org_paths_preserves_baseline(self, tmp_path: Path) -> None:
        order = default_scan_order(project_dir=tmp_path, user_home=tmp_path)
        sources = [e.source for e in order]
        assert "org" not in sources

    def test_org_entry_inserted_between_user_and_project(self, tmp_path: Path) -> None:
        org_dir = tmp_path / "org_skills"
        order = default_scan_order(
            project_dir=tmp_path,
            user_home=tmp_path,
            org_paths=[org_dir],
        )
        sources = [e.source for e in order]
        user_idx = next(i for i, e in enumerate(order) if e.source == "user" and e.kind == "skill")
        project_idx = next(i for i, e in enumerate(order) if e.source == "project" and e.kind == "skill")
        org_idx = next(i for i, e in enumerate(order) if e.source == "org")
        assert user_idx < org_idx < project_idx, (
            f"Esperado user({user_idx}) < org({org_idx}) < project({project_idx})"
        )

    def test_org_entry_has_correct_attributes(self, tmp_path: Path) -> None:
        org_dir = tmp_path / "org_skills"
        order = default_scan_order(
            project_dir=tmp_path,
            user_home=tmp_path,
            org_paths=[org_dir],
        )
        org_entries = [e for e in order if e.source == "org"]
        assert len(org_entries) == 1
        entry = org_entries[0]
        assert entry.directory == org_dir
        assert entry.kind == "skill"
        assert entry.force_uppercase_name is False

    def test_multiple_org_paths_all_inserted(self, tmp_path: Path) -> None:
        org1 = tmp_path / "org1"
        org2 = tmp_path / "org2"
        order = default_scan_order(
            project_dir=tmp_path,
            user_home=tmp_path,
            org_paths=[org1, org2],
        )
        org_entries = [e for e in order if e.source == "org"]
        assert len(org_entries) == 2


# ---------------------------------------------------------------------------
# Testes de integração — discover_skills_sync com precedência
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestOrgSkillPrecedence:
    """Verifica o contrato de precedência no conjunto mergeado de skills."""

    def test_org_skill_shadows_user_skill(self, tmp_path: Path) -> None:
        """Skill de org sombrea skill de user com o mesmo nome."""
        user_dir = tmp_path / "user" / ".deile" / "skills"
        org_dir = tmp_path / "org_skills"
        _write_skill(user_dir, "myskill", "user")
        _write_skill(org_dir, "myskill", "org")

        skills, _ = discover_skills_sync(
            project_dir=tmp_path / "project",
            user_home=tmp_path / "user",
            org_paths=[org_dir],
        )
        by_name = {s.name: s for s in skills}
        assert "myskill" in by_name
        assert by_name["myskill"].source == "org", (
            "Skill de org deve sombrar skill de user"
        )

    def test_project_skill_shadows_org_skill(self, tmp_path: Path) -> None:
        """Skill de project sombrea skill de org com o mesmo nome."""
        org_dir = tmp_path / "org_skills"
        project_dir = tmp_path / "project"
        project_skills_dir = project_dir / ".deile" / "skills"
        _write_skill(org_dir, "myskill", "org")
        _write_skill(project_skills_dir, "myskill", "project")

        skills, _ = discover_skills_sync(
            project_dir=project_dir,
            user_home=tmp_path / "user",
            org_paths=[org_dir],
        )
        by_name = {s.name: s for s in skills}
        assert "myskill" in by_name
        assert by_name["myskill"].source == "project", (
            "Skill de project deve sombrar skill de org"
        )

    def test_full_precedence_chain(self, tmp_path: Path) -> None:
        """Verifica a cadeia completa: user < org < project."""
        user_dir = tmp_path / "user" / ".deile" / "skills"
        org_dir = tmp_path / "org_skills"
        project_dir = tmp_path / "project"
        project_skills_dir = project_dir / ".deile" / "skills"

        # Cria a mesma skill nos três níveis
        _write_skill(user_dir, "shared", "user")
        _write_skill(org_dir, "shared", "org")
        _write_skill(project_skills_dir, "shared", "project")

        # Skill exclusiva do org (não sombreada)
        _write_skill(org_dir, "orgonly", "org")

        skills, _ = discover_skills_sync(
            project_dir=project_dir,
            user_home=tmp_path / "user",
            org_paths=[org_dir],
        )
        by_name = {s.name: s for s in skills}

        # "shared" deve ser do project (mais prioritário)
        assert by_name["shared"].source == "project"
        # "orgonly" deve existir e ser do org
        assert "orgonly" in by_name
        assert by_name["orgonly"].source == "org"

    def test_backward_compat_without_org(self, tmp_path: Path) -> None:
        """Sem org_paths, skills são idênticas ao baseline (sem entry 'org')."""
        user_dir = tmp_path / "user" / ".deile" / "skills"
        project_dir = tmp_path / "project"
        project_skills_dir = project_dir / ".deile" / "skills"
        _write_skill(user_dir, "skill_a", "user")
        _write_skill(project_skills_dir, "skill_b", "project")

        skills_without_org, _ = discover_skills_sync(
            project_dir=project_dir,
            user_home=tmp_path / "user",
        )
        skills_with_empty_org, _ = discover_skills_sync(
            project_dir=project_dir,
            user_home=tmp_path / "user",
            org_paths=[],
        )

        names_without = {s.name for s in skills_without_org}
        names_with_empty = {s.name for s in skills_with_empty_org}
        assert names_without == names_with_empty
        assert all(s.source != "org" for s in skills_without_org)
