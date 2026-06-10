#!/usr/bin/env python3
"""Validate worker `.results/*.v1.json` files against the schema definition."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Mapping, Sequence, Union

DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "deile" / "core" / "schemas" / "result_v1.json"
DEFAULT_RESULTS_DIR = Path(".results")
DEFAULT_PATTERN = "*.v1.json"
META_SCHEMA_URL = "https://json-schema.org/draft/2020-12/schema"

JSON_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
}


class ResultValidationError(Exception):
    """Raised when validation cannot proceed due to schema issues."""


def load_json(path: Path) -> Union[Mapping[str, object], Sequence[object]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate worker results against the v1 schema",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--schema-file",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="Path to the schema definition (JSON).",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="Directory containing *.v1.json result files.",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help="Glob pattern used to locate result files within --results-dir.",
    )
    return parser.parse_args(argv)


def match_type(value: object, expected: str) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected in JSON_TYPE_MAP:
        return isinstance(value, JSON_TYPE_MAP[expected])
    return False


def validate_schema_definition(schema: Mapping[str, object]) -> List[str]:
    problems: List[str] = []
    if schema.get("$schema") != META_SCHEMA_URL:
        problems.append("schema metadata missing or $schema does not point to draft 2020-12")
    if schema.get("type") != "object":
        problems.append("root schema must have type \"object\"")
    required = schema.get("required")
    expected_required = [
        "schema_version",
        "task_id",
        "ok",
        "elapsed_s",
        "brief",
        "summary",
        "files",
        "channel_id",
        "workdir",
        "status_message_id",
        "finished_at",
    ]
    if not isinstance(required, list):
        problems.append("required must be a list of property names")
    else:
        for name in expected_required:
            if name not in required:
                problems.append(f"missing required property '{name}' in schema")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        problems.append("properties must be a mapping")
    return problems


def _validate(
    value: object,
    schema: Mapping[str, object],
    path: str = "$",
) -> List[str]:
    errors: List[str] = []

    schema_type = schema.get("type")
    if schema_type is not None:
        allowed: Sequence[str]
        if isinstance(schema_type, list):
            allowed = schema_type
        else:
            allowed = (schema_type,)
        if not any(match_type(value, expected) for expected in allowed):
            errors.append(
                f"{path}: expected type {allowed}, got {type(value).__name__}"
            )
            return errors

    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append(f"{path}: value must be one of {schema['enum']}")

    if "const" in schema:
        if value != schema["const"]:
            errors.append(f"{path}: value must be {schema['const']}")

    if "pattern" in schema and isinstance(value, str):
        pattern = schema["pattern"]
        if not re.fullmatch(pattern, value):
            errors.append(f"{path}: value does not match pattern {pattern}")

    if schema.get("format") == "date-time" and isinstance(value, str):
        try:
            datetime.fromisoformat(value)
        except ValueError:
            errors.append(f"{path}: value is not a valid ISO date-time")

    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for prop_name, prop_schema in properties.items():
                if prop_name in value:
                    errors.extend(
                        _validate(
                            value[prop_name],
                            prop_schema,
                            f"{path}/{prop_name}",
                        )
                    )
        required = schema.get("required")
        if isinstance(required, list):
            for req in required:
                if req not in value:
                    errors.append(f"{path}: property '{req}' is required")
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected property '{key}'")

    if isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                errors.extend(
                    _validate(item, item_schema, f"{path}[{index}]")
                )

    if isinstance(value, (int, float)) and "minimum" in schema:
        if schema.get("minimum") is not None and value < schema["minimum"]:  # type: ignore[arg-type]
            errors.append(f"{path}: value {value} below minimum {schema['minimum']}")
    if isinstance(value, (int, float)) and "maximum" in schema:
        if schema.get("maximum") is not None and value > schema["maximum"]:  # type: ignore[arg-type]
            errors.append(f"{path}: value {value} above maximum {schema['maximum']}")

    return errors


def validate_instance(
    instance: Mapping[str, object], schema: Mapping[str, object]
) -> List[str]:
    return _validate(instance, schema, path="$")


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    schema_file = args.schema_file
    if not schema_file.is_file():
        print(f"Schema file not found: {schema_file}", file=sys.stderr)
        return 1

    try:
        schema = load_json(schema_file)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    schema_problems = validate_schema_definition(schema)
    if schema_problems:
        for problem in schema_problems:
            print(f"schema error: {problem}", file=sys.stderr)
        return 1

    results_dir = args.results_dir
    if not results_dir.exists():
        print(f"Results directory does not exist: {results_dir}", file=sys.stderr)
        return 0

    files = sorted(results_dir.glob(args.pattern))
    if not files:
        print(f"No result files matching {args.pattern} in {results_dir}")
        return 0

    errors: List[str] = []
    for file in files:
        try:
            instance = load_json(file)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(instance, Mapping):
            errors.append(f"{file}: result must be a JSON object")
            continue
        instance_errors = validate_instance(instance, schema)
        if instance_errors:
            errors.extend(f"{file}: {msg}" for msg in instance_errors)

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"Validated {len(files)} result files against {schema_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
