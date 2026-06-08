"""Tests for scripts/validate_results_schema.py."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from typing import Dict

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "validate_results_schema.py"
_spec = importlib.util.spec_from_file_location("validate_results_schema", _SCRIPT_PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

main = _module.main
validate_instance = _module.validate_instance
validate_schema_definition = _module.validate_schema_definition

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "deile" / "core" / "schemas" / "result_v1.json"


def _build_valid_result() -> Dict[str, object]:
    fingerprint = hashlib.sha256(b"content").hexdigest()
    return {
        "schema_version": 1,
        "task_id": "a1b2c3d4e5f6",
        "ok": True,
        "elapsed_s": 1.23,
        "brief": "test brief",
        "summary": "test summary",
        "files": ["result.json"],
        "channel_id": "123456789012345678",
        "workdir": "/tmp/workdir",
        "status_message_id": None,
        "finished_at": datetime.utcnow().isoformat() + "Z",
        "resume": {
            "ended": "incompleto",
            "pr_url": "",
            "motivo_bloqueio": "",
            "motivo_fim_loop": "natural",
            "fingerprint": fingerprint,
            "tentativa": 1,
            "budget_acumulado_s": 1.23,
        },
    }


def test_validate_instance_with_valid_payload():
    payload = _build_valid_result()
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = validate_instance(payload, schema)
    assert errors == []


def test_validate_instance_missing_required_property():
    payload = _build_valid_result()
    payload.pop("task_id")
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = validate_instance(payload, schema)
    assert any("task_id" in err for err in errors)


def test_validate_schema_definition_is_clean():
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert validate_schema_definition(schema) == []


def test_main_passes_with_valid_file(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    result_path = results_dir / "task.v1.json"
    result_path.write_text(json.dumps(_build_valid_result()), encoding="utf-8")

    ret = main([
        "--schema-file",
        str(_SCHEMA_PATH),
        "--results-dir",
        str(results_dir),
    ])
    assert ret == 0


def test_main_fails_for_invalid_payload(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    payload = _build_valid_result()
    payload.pop("summary")
    result_path = results_dir / "task.v1.json"
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    ret = main([
        "--schema-file",
        str(_SCHEMA_PATH),
        "--results-dir",
        str(results_dir),
    ])
    assert ret != 0
