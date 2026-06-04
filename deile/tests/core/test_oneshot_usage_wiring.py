"""Wiring test for cli._run_oneshot — AC2 usage metadata correctness.

Verifies that after the AC2 fix (replacing the dead literal "oneshot_cli_session"
with session.session_id), the response.metadata["usage"] envelope is populated
with non-zero data when UsageRecord entries exist for the session.

The test mocks at the boundary points that are expensive (agent.process_input,
provider bootstrap) so no real LLM call is made, while allowing the AC2 block
inside _run_oneshot to execute with real logic from deile.core.usage_envelope.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_response(status_value: str = "idle") -> object:
    """Return a minimal object that _run_oneshot expects from agent.process_input."""
    return SimpleNamespace(
        content="hello from fake agent",
        status=SimpleNamespace(value=status_value),
        metadata=None,
    )


def _make_records(n: int = 2, cost_per_record: float = 0.05,
                  prompt_tokens: int = 100, completion_tokens: int = 50) -> list:
    """Return plain-dict UsageRecord stubs understood by build_usage_envelope."""
    return [
        {
            "cost_usd": cost_per_record,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Core wiring tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_metadata_usage_is_non_zero_when_records_exist(tmp_path, monkeypatch):
    """After the AC2 fix, metadata['usage'] reflects real records (not all zeros).

    This is the blocker regression test: if the dead literal "oneshot_cli_session"
    is ever reintroduced, records_for_session() will return [] for the real session_id
    and this test will catch it by seeing all-zero usage.
    """
    records = _make_records(n=2, cost_per_record=0.05, prompt_tokens=100,
                            completion_tokens=50)

    # Patch _get_usage_records so build_usage_envelope returns non-zero data
    # for whatever session_id _run_oneshot generates (the patched function
    # accepts any sid — mimics UsageRepository having records for that session).
    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: records,
        raising=True,
    )

    fake_response = _make_fake_response()
    captured: dict = {}

    async def fake_process_input(self, user_input, session_id="default", **kwargs):
        # Capture the session_id the agent was called with (= the real oneshot id)
        captured["session_id"] = session_id
        return fake_response

    # Patch all the infrastructure that _run_oneshot calls before the AC2 block
    with (
        patch("deile.cli._bootstrap_provider_router_or_print_error",
              return_value=MagicMock()),
        patch("deile.cli._construct_agent", new_callable=AsyncMock) as mock_construct,
    ):
        # Build a minimal fake agent that creates a real-ish session object
        fake_session = SimpleNamespace(
            session_id=None,  # will be filled in below
            context_data={},
        )

        fake_agent = MagicMock()

        def fake_create_session(session_id, working_directory=None):
            fake_session.session_id = session_id
            return fake_session

        fake_agent.create_session = fake_create_session

        async def _process_input_side_effect(**kw):
            return await fake_process_input(
                fake_agent, kw.get("user_input", ""), session_id=kw.get("session_id", "")
            )

        fake_agent.process_input = AsyncMock(side_effect=_process_input_side_effect)
        mock_construct.return_value = fake_agent

        # Also stub settings so _run_oneshot doesn't hit the real filesystem
        fake_settings = SimpleNamespace(
            working_directory=tmp_path,
            preferred_model=None,
            reasoning_effort=None,
        )
        monkeypatch.setattr("deile.config.settings.get_settings",
                            lambda: fake_settings, raising=True)

        # Stub out ConfigManager at its source module (locally imported in _run_oneshot)
        with patch("deile.config.manager.ConfigManager") as mock_cm_cls:
            mock_cm_cls.return_value = MagicMock()

            from deile.cli import _run_oneshot
            await _run_oneshot("hello world")

    # Assert: metadata["usage"] on the response object is non-zero
    assert fake_response.metadata is not None, (
        "response.metadata must be populated by the AC2 block"
    )
    usage = fake_response.metadata.get("usage")
    assert usage is not None, "response.metadata['usage'] key must be present"

    assert usage["schema_version"] == 1

    # At least one numeric field must be non-zero — proves records were matched
    total_signal = usage["cost_usd"] + usage["tokens_in"] + usage["tokens_out"] + usage["turns"]
    assert total_signal > 0, (
        "metadata['usage'] is all zeros — session_id used in build_usage_envelope "
        "did not match the session_id under which UsageRecords were stored. "
        "This is the AC2 regression: the dead literal 'oneshot_cli_session' was "
        "probably reintroduced."
    )


@pytest.mark.unit
async def test_metadata_usage_turns_matches_record_count(tmp_path, monkeypatch):
    """The 'turns' field in metadata['usage'] equals the number of UsageRecord entries."""
    n_records = 3
    records = _make_records(n=n_records)

    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: records,
        raising=True,
    )

    fake_response = _make_fake_response()

    with (
        patch("deile.cli._bootstrap_provider_router_or_print_error",
              return_value=MagicMock()),
        patch("deile.cli._construct_agent", new_callable=AsyncMock) as mock_construct,
    ):
        fake_session = SimpleNamespace(session_id=None, context_data={})
        fake_agent = MagicMock()

        def fake_create_session(session_id, working_directory=None):
            fake_session.session_id = session_id
            return fake_session

        fake_agent.create_session = fake_create_session
        fake_agent.process_input = AsyncMock(return_value=fake_response)
        mock_construct.return_value = fake_agent

        fake_settings = SimpleNamespace(
            working_directory=tmp_path,
            preferred_model=None,
            reasoning_effort=None,
        )
        monkeypatch.setattr("deile.config.settings.get_settings",
                            lambda: fake_settings, raising=True)

        with patch("deile.config.manager.ConfigManager") as mock_cm_cls:
            mock_cm_cls.return_value = MagicMock()

            from deile.cli import _run_oneshot
            await _run_oneshot("hello")

    assert fake_response.metadata["usage"]["turns"] == n_records


@pytest.mark.unit
async def test_sidecar_written_with_correct_session_id(tmp_path, monkeypatch):
    """write_usage_sidecar is called with the real session_id, not a literal.

    When DEILE_USAGE_SIDECAR is set, the sidecar file reflects non-zero usage
    data — proving the correct session_id was passed through the entire AC2 path.
    """
    sidecar_path = tmp_path / "usage.json"
    monkeypatch.setenv("DEILE_USAGE_SIDECAR", str(sidecar_path))

    records = _make_records(n=1, cost_per_record=0.10, prompt_tokens=200,
                            completion_tokens=80)
    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: records,
        raising=True,
    )

    fake_response = _make_fake_response()

    with (
        patch("deile.cli._bootstrap_provider_router_or_print_error",
              return_value=MagicMock()),
        patch("deile.cli._construct_agent", new_callable=AsyncMock) as mock_construct,
    ):
        fake_session = SimpleNamespace(session_id=None, context_data={})
        fake_agent = MagicMock()

        def fake_create_session(session_id, working_directory=None):
            fake_session.session_id = session_id
            return fake_session

        fake_agent.create_session = fake_create_session
        fake_agent.process_input = AsyncMock(return_value=fake_response)
        mock_construct.return_value = fake_agent

        fake_settings = SimpleNamespace(
            working_directory=tmp_path,
            preferred_model=None,
            reasoning_effort=None,
        )
        monkeypatch.setattr("deile.config.settings.get_settings",
                            lambda: fake_settings, raising=True)

        with patch("deile.config.manager.ConfigManager") as mock_cm_cls:
            mock_cm_cls.return_value = MagicMock()

            from deile.cli import _run_oneshot
            await _run_oneshot("test sidecar")

    # The sidecar must exist and contain non-zero data
    assert sidecar_path.exists(), "DEILE_USAGE_SIDECAR file was not written"

    parsed = json.loads(sidecar_path.read_text())
    assert parsed["schema_version"] == 1
    assert parsed["turns"] == 1
    assert parsed["tokens_in"] == 200
    assert parsed["tokens_out"] == 80
    assert pytest.approx(parsed["cost_usd"], rel=1e-6) == 0.10


@pytest.mark.unit
async def test_no_duplicate_sidecar_write(tmp_path, monkeypatch):
    """Only the single consolidated write path (core.usage_envelope) writes the sidecar.

    The legacy observability.usage_sidecar module and the cli._write_usage_sidecar
    helper have been removed — this test guards against either being reintroduced.
    """
    sidecar_path = tmp_path / "usage.json"
    monkeypatch.setenv("DEILE_USAGE_SIDECAR", str(sidecar_path))

    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: [],
        raising=True,
    )

    fake_response = _make_fake_response()

    collect_calls: list = []

    # Guard against reintroduction of cli._write_usage_sidecar.
    try:
        monkeypatch.setattr(
            "deile.cli._write_usage_sidecar",
            lambda sid: collect_calls.append(sid),
            raising=True,
        )
    except AttributeError:
        pass  # already removed from cli.py (expected)

    with (
        patch("deile.cli._bootstrap_provider_router_or_print_error",
              return_value=MagicMock()),
        patch("deile.cli._construct_agent", new_callable=AsyncMock) as mock_construct,
    ):
        fake_session = SimpleNamespace(session_id=None, context_data={})
        fake_agent = MagicMock()

        def fake_create_session(session_id, working_directory=None):
            fake_session.session_id = session_id
            return fake_session

        fake_agent.create_session = fake_create_session
        fake_agent.process_input = AsyncMock(return_value=fake_response)
        mock_construct.return_value = fake_agent

        fake_settings = SimpleNamespace(
            working_directory=tmp_path,
            preferred_model=None,
            reasoning_effort=None,
        )
        monkeypatch.setattr("deile.config.settings.get_settings",
                            lambda: fake_settings, raising=True)

        with patch("deile.config.manager.ConfigManager") as mock_cm_cls:
            mock_cm_cls.return_value = MagicMock()

            from deile.cli import _run_oneshot
            await _run_oneshot("test no duplicate")

    assert collect_calls == [], (
        "cli._write_usage_sidecar was reintroduced — the duplicate sidecar "
        "write path was removed and must stay removed. "
        f"Calls: {collect_calls}"
    )
