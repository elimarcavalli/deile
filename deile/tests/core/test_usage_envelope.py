"""
Comprehensive tests for deile.core.usage_envelope (AC2).

Covers:
  - build_usage_envelope returns schema_version=1
  - build_usage_envelope sums cost_usd, tokens_in, tokens_out, turns
  - build_usage_envelope with no records returns zeros
  - write_usage_sidecar writes valid JSON to DEILE_USAGE_SIDECAR path
  - write_usage_sidecar does nothing when DEILE_USAGE_SIDECAR is not set
  - write_usage_sidecar does not raise on error (e.g. bad path)
  - Sidecar JSON is parseable and contains all 5 required fields
"""

import json

import pytest

from deile.core.usage_envelope import build_usage_envelope, write_usage_sidecar

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_record(cost_usd=0.0, prompt_tokens=0, completion_tokens=0):
    """Return a minimal usage record dict understood by build_usage_envelope."""
    return {
        "cost_usd": cost_usd,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


# ---------------------------------------------------------------------------
# build_usage_envelope
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_schema_version_is_one(tmp_path):
    """build_usage_envelope always returns schema_version=1."""
    session_id = "sess-schema-version"
    envelope = build_usage_envelope(session_id)
    assert envelope["schema_version"] == 1


@pytest.mark.unit
def test_returns_dict_with_all_five_fields(tmp_path):
    """build_usage_envelope returns a dict containing exactly the 5 required fields."""
    session_id = "sess-fields"
    envelope = build_usage_envelope(session_id)
    required_fields = {"schema_version", "cost_usd", "tokens_in", "tokens_out", "turns"}
    assert required_fields.issubset(envelope.keys())


@pytest.mark.unit
def test_no_records_returns_zeros(tmp_path):
    """build_usage_envelope with no usage records returns zero for numeric fields."""
    session_id = "sess-no-records"
    envelope = build_usage_envelope(session_id)
    assert envelope["schema_version"] == 1
    assert envelope["cost_usd"] == 0.0
    assert envelope["tokens_in"] == 0
    assert envelope["tokens_out"] == 0
    assert envelope["turns"] == 0


@pytest.mark.unit
def test_sums_cost_usd(tmp_path, monkeypatch):
    """build_usage_envelope sums cost_usd across all records."""
    session_id = "sess-cost-sum"
    records = [
        _make_record(cost_usd=0.10),
        _make_record(cost_usd=0.05),
        _make_record(cost_usd=0.25),
    ]
    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: records,
        raising=False,
    )
    envelope = build_usage_envelope(session_id)
    assert pytest.approx(envelope["cost_usd"], rel=1e-6) == 0.40


@pytest.mark.unit
def test_sums_tokens_in(tmp_path, monkeypatch):
    """build_usage_envelope sums prompt_tokens into tokens_in."""
    session_id = "sess-tokens-in"
    records = [
        _make_record(prompt_tokens=100),
        _make_record(prompt_tokens=200),
        _make_record(prompt_tokens=50),
    ]
    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: records,
        raising=False,
    )
    envelope = build_usage_envelope(session_id)
    assert envelope["tokens_in"] == 350


@pytest.mark.unit
def test_sums_tokens_out(tmp_path, monkeypatch):
    """build_usage_envelope sums completion_tokens into tokens_out."""
    session_id = "sess-tokens-out"
    records = [
        _make_record(completion_tokens=40),
        _make_record(completion_tokens=60),
    ]
    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: records,
        raising=False,
    )
    envelope = build_usage_envelope(session_id)
    assert envelope["tokens_out"] == 100


@pytest.mark.unit
def test_turns_equals_record_count(tmp_path, monkeypatch):
    """build_usage_envelope sets turns to the number of usage records."""
    session_id = "sess-turns"
    records = [
        _make_record(),
        _make_record(),
        _make_record(),
    ]
    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: records,
        raising=False,
    )
    envelope = build_usage_envelope(session_id)
    assert envelope["turns"] == 3


@pytest.mark.unit
def test_sums_all_fields_together(tmp_path, monkeypatch):
    """build_usage_envelope correctly sums all numeric fields in one call."""
    session_id = "sess-all-fields"
    records = [
        _make_record(cost_usd=0.01, prompt_tokens=10, completion_tokens=5),
        _make_record(cost_usd=0.02, prompt_tokens=20, completion_tokens=10),
    ]
    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: records,
        raising=False,
    )
    envelope = build_usage_envelope(session_id)
    assert envelope["schema_version"] == 1
    assert pytest.approx(envelope["cost_usd"], rel=1e-6) == 0.03
    assert envelope["tokens_in"] == 30
    assert envelope["tokens_out"] == 15
    assert envelope["turns"] == 2


# ---------------------------------------------------------------------------
# write_usage_sidecar
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_sidecar_writes_json(tmp_path, monkeypatch):
    """write_usage_sidecar writes a JSON file to the path in DEILE_USAGE_SIDECAR."""
    sidecar_path = tmp_path / "usage.json"
    monkeypatch.setenv("DEILE_USAGE_SIDECAR", str(sidecar_path))

    session_id = "sess-write-json"
    write_usage_sidecar(session_id)

    assert sidecar_path.exists(), "Sidecar file should have been created"


@pytest.mark.unit
def test_write_sidecar_json_is_parseable(tmp_path, monkeypatch):
    """The sidecar file written by write_usage_sidecar must be valid JSON."""
    sidecar_path = tmp_path / "usage.json"
    monkeypatch.setenv("DEILE_USAGE_SIDECAR", str(sidecar_path))

    session_id = "sess-parseable"
    write_usage_sidecar(session_id)

    content = sidecar_path.read_text()
    parsed = json.loads(content)  # raises if not valid JSON
    assert isinstance(parsed, dict)


@pytest.mark.unit
def test_write_sidecar_json_has_all_five_fields(tmp_path, monkeypatch):
    """Sidecar JSON must contain all 5 required fields."""
    sidecar_path = tmp_path / "usage.json"
    monkeypatch.setenv("DEILE_USAGE_SIDECAR", str(sidecar_path))

    session_id = "sess-five-fields"
    write_usage_sidecar(session_id)

    parsed = json.loads(sidecar_path.read_text())
    required_fields = {"schema_version", "cost_usd", "tokens_in", "tokens_out", "turns"}
    assert required_fields.issubset(
        parsed.keys()
    ), f"Missing fields: {required_fields - set(parsed.keys())}"


@pytest.mark.unit
def test_write_sidecar_schema_version_is_one(tmp_path, monkeypatch):
    """Sidecar JSON schema_version must be 1."""
    sidecar_path = tmp_path / "usage.json"
    monkeypatch.setenv("DEILE_USAGE_SIDECAR", str(sidecar_path))

    session_id = "sess-schema-v1"
    write_usage_sidecar(session_id)

    parsed = json.loads(sidecar_path.read_text())
    assert parsed["schema_version"] == 1


@pytest.mark.unit
def test_write_sidecar_does_nothing_when_env_not_set(tmp_path, monkeypatch):
    """write_usage_sidecar does nothing when DEILE_USAGE_SIDECAR is not set."""
    monkeypatch.delenv("DEILE_USAGE_SIDECAR", raising=False)

    session_id = "sess-no-env"
    # Should not raise and should not create any file
    write_usage_sidecar(session_id)

    # Verify no sidecar was written in tmp_path
    written = list(tmp_path.iterdir())
    assert written == [], "No file should be written when env var is absent"


@pytest.mark.unit
def test_write_sidecar_does_not_raise_on_bad_path(monkeypatch):
    """write_usage_sidecar must not raise even when the sidecar path is invalid."""
    monkeypatch.setenv("DEILE_USAGE_SIDECAR", "/nonexistent/directory/usage.json")

    session_id = "sess-bad-path"
    # Must not raise any exception
    write_usage_sidecar(session_id)


@pytest.mark.unit
def test_write_sidecar_reflects_usage_data(tmp_path, monkeypatch):
    """Sidecar JSON reflects the aggregated usage data for the session."""
    sidecar_path = tmp_path / "usage.json"
    monkeypatch.setenv("DEILE_USAGE_SIDECAR", str(sidecar_path))

    records = [
        _make_record(cost_usd=0.03, prompt_tokens=300, completion_tokens=150),
        _make_record(cost_usd=0.07, prompt_tokens=700, completion_tokens=350),
    ]
    monkeypatch.setattr(
        "deile.core.usage_envelope._get_usage_records",
        lambda sid: records,
        raising=False,
    )

    session_id = "sess-data-check"
    write_usage_sidecar(session_id)

    parsed = json.loads(sidecar_path.read_text())
    assert parsed["schema_version"] == 1
    assert pytest.approx(parsed["cost_usd"], rel=1e-6) == 0.10
    assert parsed["tokens_in"] == 1000
    assert parsed["tokens_out"] == 500
    assert parsed["turns"] == 2
