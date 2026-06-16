"""Wiring tests for the project-agnostic forge repo (issue #612).

These tests exercise the **real call-sites** that consume
:func:`resolve_forge_repo` — not the resolver in isolation — so they prove the
injected config flows end-to-end (anti-Goodhart, ref. GC #596):

- **AC-3 (config ausente falha alto):** with no repo configured, the resolver
  and the production startup path (``build_default_pipeline_config``) abort with
  a clear :class:`ConfigurationError` instead of silently using the historical
  ``elimarcavalli/deile`` default.
- **AC-4 (sem vazamento do default):** with a neutral repo injected, the real
  call-sites (implementer ``gh api repos/<repo>/...``; the monitor's forge
  client) consume the injected value and never the literal default.
- **AC-6 (fiação tick→despacho):** ``build_default_pipeline_config`` carries the
  injected repo into ``PipelineConfig.repo``, and a ``PipelineMonitor`` built
  from it routes its forge client to that exact repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.config.settings import get_settings, reset_settings
from deile.core.exceptions import ConfigurationError
from deile.orchestration.pipeline.constants import (
    resolve_forge_repo,
    resolve_pipeline_repo,
)


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """Each test gets a pristine Settings singleton with no repo configured.

    Redirect DEILE_SETTINGS_FILE to a non-existent path and clear the forge
    env vars so the dataclass defaults (empty repo) win — that is the
    config-absent state AC-3 must fail loud on.
    """
    monkeypatch.setenv("DEILE_SETTINGS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.delenv("DEILE_FORGE_REPO", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_REPO", raising=False)
    monkeypatch.delenv("DEILE_FORGE_KIND", raising=False)
    reset_settings()
    yield
    reset_settings()


def _inject_repo(value: str) -> None:
    """Mutate the singleton as a manifest/ConfigMap injection would."""
    get_settings().forge_repo = value


# ---------------------------------------------------------------------------
# AC-3 — config ausente falha alto (não cai no default silencioso)
# ---------------------------------------------------------------------------


class TestAC3FailLoudOnMissingConfig:
    def test_resolve_forge_repo_raises_when_unconfigured(self):
        with pytest.raises(ConfigurationError) as exc:
            resolve_forge_repo()
        assert "project-agnostic" in str(exc.value)
        # Never silently impersonates the DEILE repo.
        assert "elimarcavalli/deile" not in str(exc.value)

    def test_resolve_forge_repo_error_carries_config_key(self):
        with pytest.raises(ConfigurationError) as exc:
            resolve_forge_repo()
        assert exc.value.config_key == "forge.repo"

    def test_legacy_alias_also_fails_loud(self):
        with pytest.raises(ConfigurationError):
            resolve_pipeline_repo()

    def test_build_default_pipeline_config_aborts_when_unconfigured(self, monkeypatch):
        """The real production startup path (pipeline pod entrypoint /
        /pipeline start) builds its config here; with no repo it must abort
        at startup, not mid-tick."""
        from deile.orchestration.pipeline import monitor as monitor_mod

        monkeypatch.setattr(
            "deile.tools._pipeline_paths.resolve_base_path", lambda: Path("/tmp")
        )
        with pytest.raises(ConfigurationError):
            monitor_mod.build_default_pipeline_config()

    def test_non_required_resolution_warns_and_uses_fallback(self, caplog):
        """Graceful surfaces (panel/CLI) may degrade with a clear WARNING
        instead of aborting — but never silently."""
        import logging

        with caplog.at_level(logging.WARNING):
            result = resolve_forge_repo(require=False, fallback="display/only")
        assert result == "display/only"
        assert any(
            "No forge repository configured" in r.message for r in caplog.records
        )

    def test_non_required_without_fallback_returns_empty(self):
        assert resolve_forge_repo(require=False) == ""


# ---------------------------------------------------------------------------
# AC-4 — sem vazamento do default no fluxo real (repo injetado é consumido)
# ---------------------------------------------------------------------------


class TestAC4NoDefaultLeak:
    def test_injected_forge_repo_is_resolved(self):
        _inject_repo("acme/neutral-project")
        assert resolve_forge_repo() == "acme/neutral-project"

    def test_legacy_pipeline_repo_is_resolved_when_forge_repo_blank(self):
        s = get_settings()
        s.forge_repo = ""
        s.pipeline_repo = "legacy/project"
        assert resolve_forge_repo() == "legacy/project"

    def test_forge_repo_wins_over_pipeline_repo(self):
        s = get_settings()
        s.forge_repo = "new/canonical"
        s.pipeline_repo = "legacy/project"
        assert resolve_forge_repo() == "new/canonical"

    async def test_implementer_call_site_uses_injected_repo(self, monkeypatch):
        """The implementer's real gh call-site (``_collect_review_delta``)
        issues ``gh api repos/<repo>/...`` against the INJECTED repo — proving
        the config reaches the dispatch-adjacent forge call, not a default."""
        import asyncio as _aio

        _inject_repo("acme/neutral-project")
        from deile.orchestration.pipeline import implementer as impl_mod

        calls = []

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return _FakeProc()

        # The method does ``import asyncio as _aio`` locally, so patching the
        # global asyncio.create_subprocess_exec is the real seam.
        monkeypatch.setattr(_aio, "create_subprocess_exec", fake_exec)

        # _collect_review_delta lives on the concrete WorkerImplementer and
        # only uses module-level resolve_forge_repo() (no self.<attr>), so an
        # uninitialised instance is enough to exercise the call-site.
        implementer = impl_mod.WorkerImplementer.__new__(impl_mod.WorkerImplementer)
        await implementer._collect_review_delta(
            pr_number=7, prev_completed_at=1_700_000_000
        )

        flat = [" ".join(str(a) for a in args) for args in calls]
        assert any(
            "repos/acme/neutral-project/issues/7/comments" in c for c in flat
        ), flat
        assert not any("elimarcavalli/deile" in c for c in flat)


# ---------------------------------------------------------------------------
# AC-6 — fiação tick→despacho (build_default_pipeline_config → monitor → forge)
# ---------------------------------------------------------------------------


class TestAC6WiringToDispatch:
    def test_build_default_pipeline_config_carries_injected_repo(self, monkeypatch):
        _inject_repo("acme/neutral-project")
        from deile.orchestration.pipeline import monitor as monitor_mod

        monkeypatch.setattr(
            "deile.tools._pipeline_paths.resolve_base_path", lambda: Path("/tmp")
        )
        cfg = monitor_mod.build_default_pipeline_config()
        assert cfg.repo == "acme/neutral-project"

    def test_monitor_forge_client_routes_to_injected_repo(self, monkeypatch):
        """End-to-end wiring: the config the orchestrator builds drives the
        forge client the monitor dispatches through — ``monitor.forge.repo``
        is the injected value, never the hardcoded default."""
        _inject_repo("acme/neutral-project")
        from deile.orchestration.pipeline import monitor as monitor_mod

        monkeypatch.setattr(
            "deile.tools._pipeline_paths.resolve_base_path", lambda: Path("/tmp")
        )
        cfg = monitor_mod.build_default_pipeline_config()
        mon = monitor_mod.PipelineMonitor(cfg)
        assert mon.forge.repo == "acme/neutral-project"
        assert mon.forge.repo != "elimarcavalli/deile"
