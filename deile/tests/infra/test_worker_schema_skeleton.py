"""AC9 (issue #620) — schema_version skeleton no resultado do deile-worker.

Valida três coisas:
  1. ``_run_task`` grava ``"schema_version": 1`` no JSON de resultado.
  2. O arquivo ``deile/core/schemas/result_v1.json`` existe e é um JSON Schema
     draft-2020-12 bem-formado.
  3. ``result_handler`` aceita um resultado SEM ``schema_version`` (compat
     forward) — serve com warning, não rejeita.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

aiohttp_test_utils = pytest.importorskip("aiohttp.test_utils")

import worker_server  # noqa: E402

import deile.core.schemas as schemas  # noqa: E402

RESULT_SCHEMA_PATH = schemas.RESULT_SCHEMA_PATH
RESULT_SCHEMA_VERSION = schemas.RESULT_SCHEMA_VERSION

pytestmark = pytest.mark.unit

_TOKEN = "test-token-0123456789abcdef"


@pytest.fixture
def _clean_tasks():
    worker_server._TASKS.clear()
    yield
    worker_server._TASKS.clear()


# ----- 1. result carrega schema_version ------------------------------------


async def test_run_task_result_has_schema_version(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_server, "WORK_ROOT", tmp_path)

    class _Resp:
        content = "feito"

    agent = MagicMock()
    agent.get_or_create_session = AsyncMock(return_value=MagicMock(context_data={}))
    agent.process_input = AsyncMock(return_value=_Resp())
    agent.process_input_stream = None
    monkeypatch.setattr(worker_server, "_get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(worker_server, "_post_status_message", AsyncMock(return_value=None))
    monkeypatch.setattr(worker_server, "_edit_status_message", AsyncMock(return_value=True))
    monkeypatch.setattr(worker_server, "_react", AsyncMock(return_value=True))

    result = await worker_server._run_task(
        "cccccccccccc", "faça algo", "12345", None, "developer",
    )
    assert result["schema_version"] == RESULT_SCHEMA_VERSION == 1

    # E o JSON persistido no PVC também o contém.
    persisted = tmp_path / ".results" / (result["task_id"] + ".json")
    assert persisted.is_file()
    on_disk = json.loads(persisted.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1


# ----- 2. arquivo de schema existe e é válido ------------------------------


def test_schema_file_exists_and_is_valid_draft202012():
    assert RESULT_SCHEMA_PATH.is_file(), f"{RESULT_SCHEMA_PATH} não existe"
    doc = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    # Declara draft-2020-12.
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "schema_version" in doc["properties"]
    assert doc["properties"]["schema_version"]["const"] == 1

    # Se o validador estiver disponível, prova que o schema é bem-formado.
    jsonschema = pytest.importorskip(
        "jsonschema",
        reason="jsonschema não é dependência do projeto; validação completa "
               "roda apenas quando presente (a estrutura já é checada acima)",
    )
    jsonschema.Draft202012Validator.check_schema(doc)
    # E que um documento de resultado real valida contra ele.
    sample = {
        "schema_version": 1, "task_id": "abcdef012345", "ok": True,
        "elapsed_s": 1.5, "files": ["a.py"], "summary": "ok",
    }
    jsonschema.Draft202012Validator(doc).validate(sample)


# ----- 3. result_handler tolera ausência de schema_version -----------------


@pytest.fixture
async def client(_clean_tasks):
    app = worker_server.build_app(_TOKEN)
    async with aiohttp_test_utils.TestClient(
        aiohttp_test_utils.TestServer(app)
    ) as cli:
        yield cli


async def test_result_handler_accepts_missing_schema_version(client, caplog):
    """Resultado legado SEM schema_version → servido com warning (não 4xx)."""
    task_id = "ddeeff001122"
    worker_server._TASKS[task_id] = {
        "task_id": task_id, "ok": True, "elapsed_s": 1.0, "summary": "x",
        # sem "schema_version"
    }
    with caplog.at_level(logging.WARNING, logger="deile.worker_server"):
        resp = await client.get(
            f"/v1/result/{task_id}",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status == 200
    data = await resp.json()
    assert data["task_id"] == task_id
    assert any("schema_version" in r.getMessage() for r in caplog.records)


async def test_result_handler_no_warning_for_current_version(client, caplog):
    task_id = "112233445566"
    worker_server._TASKS[task_id] = {
        "task_id": task_id, "ok": True, "elapsed_s": 1.0,
        "schema_version": 1,
    }
    with caplog.at_level(logging.WARNING, logger="deile.worker_server"):
        resp = await client.get(
            f"/v1/result/{task_id}",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status == 200
    assert not any("schema_version" in r.getMessage() for r in caplog.records)
