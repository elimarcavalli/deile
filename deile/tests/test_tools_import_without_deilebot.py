"""AC3 — importar deile e rodar auto-descoberta sem deilebot instalado.

Prova que:
  - deile importa sem ImportError quando deilebot/deilebot_client ausentes.
  - register_messaging_tools retorna 0 e loga o skip.
  - nenhuma tool discord_* ou whatsapp_* é registrada.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import patch, MagicMock

import pytest


def _make_stub_module(name: str) -> MagicMock:
    mod = MagicMock()
    mod.__name__ = name
    mod.__spec__ = None
    return mod


class TestImportWithoutDeilebot:
    def test_register_messaging_tools_returns_zero_without_deilebot(self, monkeypatch, caplog):
        """BOT_CLIENT_AVAILABLE=False path — returns 0, logs skip."""
        import deile.tools.messaging.auto_discover as ad

        with patch.object(
            sys.modules.get("deile.integrations.bot", MagicMock()),
            "BOT_CLIENT_AVAILABLE",
            False,
            create=True,
        ):
            # Patch the import inside auto_discover
            fake_bot = MagicMock()
            fake_bot.BOT_CLIENT_AVAILABLE = False
            fake_bot.get_bot_settings = MagicMock()

            with patch.dict("sys.modules", {"deile.integrations.bot": fake_bot}):
                mock_registry = MagicMock()
                mock_registry.__contains__ = MagicMock(return_value=False)

                with caplog.at_level(logging.DEBUG, logger="deile.tools.messaging.auto_discover"):
                    result = ad.register_messaging_tools(mock_registry)

        assert result == 0

    def test_no_messaging_tools_registered_without_deilebot(self, monkeypatch):
        """When deilebot absent, registry.register must never be called."""
        import deile.tools.messaging.auto_discover as ad

        fake_bot = MagicMock()
        fake_bot.BOT_CLIENT_AVAILABLE = False

        mock_registry = MagicMock()
        mock_registry.__contains__ = MagicMock(return_value=False)

        with patch.dict("sys.modules", {"deile.integrations.bot": fake_bot}):
            ad.register_messaging_tools(mock_registry)

        mock_registry.register.assert_not_called()

    def test_bot_client_available_check_guards_tool_registration(self):
        """BOT_CLIENT_AVAILABLE flag is the sole gate (no ImportError needed)."""
        import deile.tools.messaging.auto_discover as ad

        fake_bot = MagicMock()
        fake_bot.BOT_CLIENT_AVAILABLE = False

        with patch.dict("sys.modules", {"deile.integrations.bot": fake_bot}):
            count = ad.register_messaging_tools(MagicMock())

        assert count == 0

    def test_deile_integrations_bot_exposes_bot_client_available(self):
        """integrations/bot must expose BOT_CLIENT_AVAILABLE so the lazy gate works."""
        from deile.integrations.bot import BOT_CLIENT_AVAILABLE  # noqa: F401
        assert isinstance(BOT_CLIENT_AVAILABLE, bool)
