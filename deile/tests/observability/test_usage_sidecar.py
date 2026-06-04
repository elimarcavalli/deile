"""Tests for the usage sidecar (DEILE_USAGE_SIDECAR) — AC2 of issue #508."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deile.observability.usage_sidecar import (
    SIDECAR_ENV,
    UsageEnvelope,
    collect_and_write_sidecar,
    get_sidecar_path,
    write_usage_sidecar,
)


class TestUsageEnvelope:
    def test_schema_version_is_1(self) -> None:
        env = UsageEnvelope(schema_version=1, cost_usd=0.5, tokens_in=100, tokens_out=50, turns=3)
        assert env.schema_version == 1

    def test_to_dict_keys(self) -> None:
        env = UsageEnvelope(schema_version=1, cost_usd=0.12, tokens_in=3400, tokens_out=890, turns=8)
        d = env.to_dict()
        assert set(d.keys()) == {"schema_version", "cost_usd", "tokens_in", "tokens_out", "turns"}

    def test_to_dict_values(self) -> None:
        env = UsageEnvelope(schema_version=1, cost_usd=0.12, tokens_in=3400, tokens_out=890, turns=8)
        d = env.to_dict()
        assert d["schema_version"] == 1
        assert d["cost_usd"] == 0.12
        assert d["tokens_in"] == 3400
        assert d["tokens_out"] == 890
        assert d["turns"] == 8

    def test_zero_values_valid(self) -> None:
        env = UsageEnvelope(schema_version=1, cost_usd=0.0, tokens_in=0, tokens_out=0, turns=0)
        assert env.to_dict()["cost_usd"] == 0.0


class TestGetSidecarPath:
    def test_returns_none_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(SIDECAR_ENV, raising=False)
        assert get_sidecar_path() is None

    def test_returns_none_for_empty_string(self, monkeypatch) -> None:
        monkeypatch.setenv(SIDECAR_ENV, "")
        assert get_sidecar_path() is None

    def test_returns_path_when_set(self, monkeypatch, tmp_path) -> None:
        p = str(tmp_path / "usage.json")
        monkeypatch.setenv(SIDECAR_ENV, p)
        assert get_sidecar_path() == p


class TestWriteUsageSidecar:
    def test_writes_valid_json(self, tmp_path) -> None:
        path = str(tmp_path / "usage.json")
        env = UsageEnvelope(schema_version=1, cost_usd=0.5, tokens_in=100, tokens_out=50, turns=3)
        write_usage_sidecar(env, path)
        data = json.loads(Path(path).read_text())
        assert data == {"schema_version": 1, "cost_usd": 0.5, "tokens_in": 100, "tokens_out": 50, "turns": 3}

    def test_does_not_raise_on_bad_path(self) -> None:
        env = UsageEnvelope(schema_version=1, cost_usd=0.0, tokens_in=0, tokens_out=0, turns=0)
        write_usage_sidecar(env, "/nonexistent/dir/usage.json")  # must not raise


class TestCollectAndWriteSidecar:
    def test_noop_when_env_unset(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv(SIDECAR_ENV, raising=False)
        collect_and_write_sidecar("sid-1")
        # No error, no file

    def test_writes_envelope_from_records(self, monkeypatch, tmp_path) -> None:
        path = str(tmp_path / "usage.json")
        monkeypatch.setenv(SIDECAR_ENV, path)

        # Two mock records
        rec1 = MagicMock()
        rec1.cost_usd = 0.10
        rec1.prompt_tokens = 1000
        rec1.completion_tokens = 200
        rec2 = MagicMock()
        rec2.cost_usd = 0.05
        rec2.prompt_tokens = 500
        rec2.completion_tokens = 100

        mock_repo = MagicMock()
        mock_repo.records_for_session.return_value = [rec1, rec2]

        with patch("deile.observability.usage_sidecar.get_usage_repository", return_value=mock_repo):
            collect_and_write_sidecar("sid-1")

        data = json.loads(Path(path).read_text())
        assert data["schema_version"] == 1
        assert data["cost_usd"] == pytest.approx(0.15)
        assert data["tokens_in"] == 1500
        assert data["tokens_out"] == 300
        assert data["turns"] == 2

    def test_empty_records_yields_zeros(self, monkeypatch, tmp_path) -> None:
        path = str(tmp_path / "usage.json")
        monkeypatch.setenv(SIDECAR_ENV, path)

        mock_repo = MagicMock()
        mock_repo.records_for_session.return_value = []

        with patch("deile.observability.usage_sidecar.get_usage_repository", return_value=mock_repo):
            collect_and_write_sidecar("sid-empty")

        data = json.loads(Path(path).read_text())
        assert data == {"schema_version": 1, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "turns": 0}

    def test_swallows_exception_from_repo(self, monkeypatch, tmp_path) -> None:
        path = str(tmp_path / "usage.json")
        monkeypatch.setenv(SIDECAR_ENV, path)

        with patch("deile.observability.usage_sidecar.get_usage_repository", side_effect=RuntimeError("db gone")):
            collect_and_write_sidecar("sid-broken")  # must not raise


class TestSidecarPathUniqueness:
    def test_unique_path_not_pre_existing(self, tmp_path) -> None:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".usage.json", dir=tmp_path)
        os.close(fd)
        os.unlink(path)
        assert not os.path.exists(path)
        env = UsageEnvelope(schema_version=1, cost_usd=0.0, tokens_in=0, tokens_out=0, turns=0)
        write_usage_sidecar(env, path)
        assert os.path.exists(path)
