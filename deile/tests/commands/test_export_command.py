"""Testes do comando /export — dados reais, sem mock hardcoded"""

import json
import zipfile
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.__version__ import __version__
from deile.commands.base import CommandContext
from deile.commands.builtin.export_command import ExportCommand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    session_id: str = "real-session-123",
    history: Optional[List[Dict[str, Any]]] = None,
    created_at: float = 1700000000.0,
) -> MagicMock:
    session = MagicMock()
    session.session_id = session_id
    session.conversation_history = history or []
    session.created_at = created_at
    return session


def _make_agent(persona_name: str = "developer") -> MagicMock:
    agent = MagicMock()
    agent.model_router.providers = {"openai": MagicMock()}
    persona = MagicMock()
    persona.name = persona_name
    agent.persona_manager.get_active_persona.return_value = persona
    agent.memory_manager.get_memory_usage = AsyncMock(
        return_value={"total_memory_mb": 5.0}
    )
    return agent


def _make_context(
    args: str = "",
    session: Optional[Any] = None,
    agent: Optional[Any] = None,
) -> CommandContext:
    ctx = CommandContext(user_input=f"/export {args}", args=args)
    ctx.session = session or _make_session()
    ctx.agent = agent or _make_agent()
    return ctx


# ---------------------------------------------------------------------------
# Rastreabilidade de dados reais
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_session_id_matches_real_session():
    session = _make_session(session_id="my-unique-session-abc")
    ctx = _make_context(args="json", session=session)
    cmd = ExportCommand()
    data = await cmd._get_export_data(
        ctx, include_artifacts=False, include_plans=False, include_session=False
    )
    assert data["conversation"]["session_id"] == "my-unique-session-abc"
    assert data["export_metadata"]["session_id"] == "my-unique-session-abc"


@pytest.mark.unit
async def test_message_count_matches_session():
    history = [
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "olá"},
        {"role": "user", "content": "tudo bem?"},
    ]
    session = _make_session(history=history)
    ctx = _make_context(session=session)
    cmd = ExportCommand()
    data = await cmd._get_export_data(
        ctx, include_artifacts=False, include_plans=False, include_session=False
    )
    assert data["conversation"]["total_messages"] == 3
    assert len(data["conversation"]["messages"]) == 3


@pytest.mark.unit
async def test_version_from_version_module():
    ctx = _make_context()
    cmd = ExportCommand()
    data = await cmd._get_export_data(
        ctx, include_artifacts=False, include_plans=False, include_session=False
    )
    assert data["export_metadata"]["deile_version"] == __version__
    assert data["export_metadata"]["deile_version"] != "4.0.0"


@pytest.mark.unit
async def test_empty_session_exports_empty_lists():
    session = _make_session(history=[])
    ctx = _make_context(session=session)
    cmd = ExportCommand()
    data = await cmd._get_export_data(
        ctx, include_artifacts=True, include_plans=True, include_session=True
    )
    assert data["conversation"]["total_messages"] == 0
    assert data["conversation"]["messages"] == []
    assert data["artifacts"]["count"] == 0
    assert data["plans"]["count"] == 0


@pytest.mark.unit
async def test_no_hardcoded_session_id():
    ctx = _make_context()
    cmd = ExportCommand()
    data = await cmd._get_export_data(
        ctx, include_artifacts=False, include_plans=False, include_session=False
    )
    assert data["conversation"]["session_id"] != "session_20250906_184500"
    assert data["export_metadata"]["session_id"] != "session_20250906_184500"


@pytest.mark.unit
async def test_no_hardcoded_version():
    ctx = _make_context()
    cmd = ExportCommand()
    data = await cmd._get_export_data(
        ctx, include_artifacts=False, include_plans=False, include_session=False
    )
    assert data["export_metadata"]["deile_version"] != "4.0.0"


# ---------------------------------------------------------------------------
# Integridade do arquivo
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_json_output_is_valid(tmp_path):
    ctx = _make_context(args=f"json --path {tmp_path}")
    cmd = ExportCommand()
    result = await cmd.execute(ctx)
    assert result.success
    json_files = list(tmp_path.glob("*.json"))
    assert len(json_files) == 1
    data = json.loads(json_files[0].read_text())
    assert "conversation" in data
    assert "export_metadata" in data


@pytest.mark.unit
async def test_md_output_has_correct_headings(tmp_path):
    ctx = _make_context(args=f"md --path {tmp_path}")
    cmd = ExportCommand()
    result = await cmd.execute(ctx)
    assert result.success
    md_files = list(tmp_path.glob("*.md"))
    assert len(md_files) >= 1
    content = md_files[0].read_text()
    assert content.startswith("# ")


@pytest.mark.unit
async def test_zip_output_is_valid(tmp_path):
    ctx = _make_context(args=f"zip --path {tmp_path}")
    cmd = ExportCommand()
    result = await cmd.execute(ctx)
    assert result.success
    zip_files = list(tmp_path.glob("*.zip"))
    assert len(zip_files) == 1
    assert zipfile.is_zipfile(zip_files[0])
    with zipfile.ZipFile(zip_files[0]) as zf:
        names = zf.namelist()
    assert "MANIFEST.json" in names
    assert any("complete_export.json" in n for n in names)


@pytest.mark.unit
async def test_no_artifacts_flag_excludes_artifacts(tmp_path):
    history = [{"role": "user", "content": "x"}]
    session = _make_session(history=history)
    ctx = _make_context(args=f"json --path {tmp_path} --no-artifacts", session=session)
    cmd = ExportCommand()
    result = await cmd.execute(ctx)
    assert result.success
    json_files = list(tmp_path.glob("*.json"))
    data = json.loads(json_files[0].read_text())
    assert "artifacts" not in data


@pytest.mark.unit
async def test_export_metadata_has_data_sources():
    ctx = _make_context()
    cmd = ExportCommand()
    data = await cmd._get_export_data(
        ctx, include_artifacts=False, include_plans=False, include_session=True
    )
    sources = data["export_metadata"]["data_sources"]
    assert isinstance(sources, list)
    assert len(sources) > 0
    assert "AgentSession" in sources


@pytest.mark.unit
async def test_message_timestamps_are_from_session():
    history = [
        {"role": "user", "content": "msg1", "timestamp": "2026-01-01T10:00:00"},
    ]
    session = _make_session(history=history)
    ctx = _make_context(session=session)
    cmd = ExportCommand()
    data = await cmd._get_export_data(
        ctx, include_artifacts=False, include_plans=False, include_session=False
    )
    msgs = data["conversation"]["messages"]
    assert msgs[0]["timestamp"] == "2026-01-01T10:00:00"


@pytest.mark.unit
async def test_no_agent_graceful_degradation():
    ctx = _make_context(session=_make_session(), agent=None)
    ctx.agent = None
    cmd = ExportCommand()
    data = await cmd._get_export_data(
        ctx, include_artifacts=False, include_plans=False, include_session=True
    )
    # Should not crash; session_info should exist but model is indisponível
    assert "session_info" in data
    assert data["session_info"]["model"] == "indisponível"
