"""Regression tests for the synthetic-snowflake guard in ``infra/k8s/worker_server.py``.

Dois cenários geram IDs sintéticos no DEILE:

1. **channel_id sintético**: o pipeline (``WorkerImplementer``) e o subagent
   runner usam ``pipeline-issue-299`` / ``pipeline-pr-123`` /
   ``cli:<session>:<task>`` para isolar sandboxes por unidade de trabalho.
2. **user_message_id sintético**: o slash command ``/deile``
   (``deilebot.foundation.slash_dispatch.build_envelope``) gera
   ``slash-<timestamp_ms>`` quando não há mensagem real subjacente
   (interactions Discord não são mensagens addressable).

Antes do guard, esses IDs vazavam pro ``_post/_edit/_react`` → adapter
Discord → ``int(snowflake)`` ValueError → ``ProviderError`` → 502 Bad
Gateway no cliente + ``outbound_failed`` no audit log do bot, com retries
em loop.

Estes testes pinam o contrato:

1. Synthetic snowflakes (qualquer não-dígito) → helpers no-op sem chamar bot.
2. Real snowflakes (só dígitos) → helpers chamam o bot normalmente.
3. ``_react`` e ``_edit`` checam AMBOS channel_id E message_id.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import worker_server  # noqa: E402

# --------------------------------------------------------------------------
# _is_synthetic_snowflake — pure predicate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snowflake",
    [
        # channel_id flavors
        "pipeline-issue-299",
        "pipeline-pr-123",
        "pipeline-mention-pr-264",
        "cli:abc123:0",
        "cli:abc123:42",
        # message_id flavors (slash command synthetic IDs)
        "slash-1734567890123",
        "slash-1",
        # defensive cases
        "",  # empty → synthetic
        None,  # None → synthetic
        "not-a-number",
        "123abc",  # has digit + letter → synthetic
        "12-34",  # has dash → synthetic
    ],
)
def test_synthetic_ids_are_detected(snowflake):
    assert worker_server._is_synthetic_snowflake(snowflake) is True


@pytest.mark.parametrize(
    "snowflake",
    [
        "1",
        "42",
        "1475913578648436909",  # real-shaped snowflake
        "9999999999999999999",  # 19 digits
    ],
)
def test_real_snowflakes_pass_through(snowflake: str):
    assert worker_server._is_synthetic_snowflake(snowflake) is False


def test_legacy_alias_still_works():
    """``_is_synthetic_channel`` é mantido como alias para compatibilidade."""
    assert worker_server._is_synthetic_channel is worker_server._is_synthetic_snowflake


# --------------------------------------------------------------------------
# _post/_edit/_react — short-circuit on synthetic ids, no bot call
# --------------------------------------------------------------------------


async def test_post_status_message_no_ops_on_synthetic_channel():
    """Synthetic channel must NOT reach _bot_facade()."""
    fake_facade = MagicMock()
    fake_facade.channel_post = AsyncMock()
    with patch.object(worker_server, "_bot_facade", return_value=fake_facade):
        result = await worker_server._post_status_message("pipeline-pr-299", "hi")
    assert result is None
    fake_facade.channel_post.assert_not_called()


async def test_edit_status_message_no_ops_on_synthetic_channel():
    fake_facade = MagicMock()
    fake_facade.message_edit = AsyncMock()
    with patch.object(worker_server, "_bot_facade", return_value=fake_facade):
        result = await worker_server._edit_status_message(
            "cli:sess:0",
            "12345",
            "hi",
        )
    assert result is False
    fake_facade.message_edit.assert_not_called()


async def test_edit_status_message_no_ops_on_synthetic_message_id():
    """Message_id sintético (ex.: 'slash-...') também trava o edit."""
    fake_facade = MagicMock()
    fake_facade.message_edit = AsyncMock()
    with patch.object(worker_server, "_bot_facade", return_value=fake_facade):
        result = await worker_server._edit_status_message(
            "12345",
            "slash-1734567890123",
            "hi",
        )
    assert result is False
    fake_facade.message_edit.assert_not_called()


async def test_react_no_ops_on_synthetic_channel():
    fake_facade = MagicMock()
    fake_facade.reaction_add = AsyncMock()
    with patch.object(worker_server, "_bot_facade", return_value=fake_facade):
        result = await worker_server._react(
            "pipeline-issue-42",
            "12345",
            "🔧",
        )
    assert result is False
    fake_facade.reaction_add.assert_not_called()


async def test_react_no_ops_on_synthetic_message_id():
    """O caso real reportado: slash command produz user_message_id sintético."""
    fake_facade = MagicMock()
    fake_facade.reaction_add = AsyncMock()
    with patch.object(worker_server, "_bot_facade", return_value=fake_facade):
        result = await worker_server._react(
            "1499608051114836128",  # canal real
            "slash-1734567890123",  # user_message_id sintético do /deile
            "🔧",
        )
    assert result is False
    fake_facade.reaction_add.assert_not_called()


# --------------------------------------------------------------------------
# _post/_edit/_react — real snowflakes DO call through (sanity)
# --------------------------------------------------------------------------


async def test_post_status_message_calls_facade_on_real_snowflake():
    fake_result = MagicMock()
    fake_result.message_id = "msg-99"
    fake_facade = MagicMock()
    fake_facade.channel_post = AsyncMock(return_value=fake_result)
    with patch.object(worker_server, "_bot_facade", return_value=fake_facade):
        result = await worker_server._post_status_message("12345", "hi")
    assert result == "msg-99"
    fake_facade.channel_post.assert_awaited_once()


async def test_edit_status_message_calls_facade_on_real_snowflake():
    """Ambos channel_id E message_id devem ser numéricos para o edit fluir."""
    fake_facade = MagicMock()
    fake_facade.message_edit = AsyncMock()
    with patch.object(worker_server, "_bot_facade", return_value=fake_facade):
        result = await worker_server._edit_status_message(
            "12345",
            "67890",
            "hi",
        )
    assert result is True
    fake_facade.message_edit.assert_awaited_once()


async def test_react_calls_facade_on_real_snowflake():
    fake_facade = MagicMock()
    fake_facade.reaction_add = AsyncMock()
    with patch.object(worker_server, "_bot_facade", return_value=fake_facade):
        result = await worker_server._react("12345", "67890", "🔧")
    assert result is True
    fake_facade.reaction_add.assert_awaited_once()
