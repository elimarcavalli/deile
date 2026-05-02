"""Tests for the deile_bot CLI parser."""

from __future__ import annotations

import pytest

from deile_bot.cli import _build_parser


class TestParser:
    def test_run_discord(self):
        p = _build_parser()
        ns = p.parse_args(["run", "--provider", "discord"])
        assert ns.command == "run"
        assert ns.provider == "discord"

    def test_run_unknown_provider_rejected(self):
        p = _build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["run", "--provider", "irc"])

    def test_dlq_list(self):
        p = _build_parser()
        ns = p.parse_args(["dlq", "list", "--provider", "discord"])
        assert ns.dlq_action == "list"
        assert ns.provider == "discord"

    def test_dlq_purge(self):
        p = _build_parser()
        ns = p.parse_args(["dlq", "purge", "--older-than-days", "10"])
        assert ns.dlq_action == "purge"
        assert ns.older_than_days == 10

    def test_sessions_purge(self):
        p = _build_parser()
        ns = p.parse_args(["sessions", "purge", "--older-than-days", "30"])
        assert ns.older_than_days == 30

    def test_metrics(self):
        p = _build_parser()
        ns = p.parse_args(["metrics"])
        assert ns.command == "metrics"

    def test_migrate(self):
        p = _build_parser()
        ns = p.parse_args(["migrate-memory-json", "--source", "x.json", "--dry-run"])
        assert ns.dry_run is True

    def test_persona_list(self):
        p = _build_parser()
        ns = p.parse_args(["persona", "list"])
        assert ns.persona_action == "list"
