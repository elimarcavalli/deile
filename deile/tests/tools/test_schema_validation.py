"""Unit tests for ``deile/tools/schema_validation``.

This module was extracted from ``ToolRegistry`` (SRP) and, in the same
change, fixed a latent bug: required-field validation used to read
``schema.parameters["required"]`` — always empty for tools built with
the canonical pattern (``MessagingTool``), where required fields live in
``ToolSchema.required``. These tests pin the corrected behaviour so a
regression to the wrong field source fails fast.
"""

from __future__ import annotations

from deile.tools.base import ToolSchema
from deile.tools.schema_validation import _validate_type, validate_function_arguments


def _schema(parameters, required):
    return ToolSchema(
        name="t",
        description="d",
        parameters=parameters,
        required=required,
    )


def test_valid_arguments_pass():
    schema = _schema(
        {"type": "object", "properties": {"a": {"type": "string"}}},
        required=["a"],
    )
    result = validate_function_arguments(schema, {"a": "x"})
    assert result == {"valid": True, "errors": []}


def test_missing_required_field_reported():
    schema = _schema(
        {"type": "object", "properties": {"a": {"type": "string"}}},
        required=["a"],
    )
    result = validate_function_arguments(schema, {})
    assert result["valid"] is False
    assert "Missing required field: a" in result["errors"]


def test_invalid_type_reported():
    schema = _schema(
        {"type": "object", "properties": {"a": {"type": "string"}}},
        required=["a"],
    )
    result = validate_function_arguments(schema, {"a": 123})
    assert result["valid"] is False
    assert any("expected string" in e for e in result["errors"])


def test_canonical_pattern_required_read_from_schema_required():
    """Regression: canonical schemas carry required in ``schema.required``,
    not in ``parameters["required"]`` — validation must read the former."""
    schema = _schema(
        {"type": "object", "properties": {"content": {"type": "string"}}},
        required=["content"],
    )
    assert "required" not in schema.parameters
    result = validate_function_arguments(schema, {})
    assert result["valid"] is False
    assert "Missing required field: content" in result["errors"]


def test_arguments_none_treated_as_empty():
    schema = _schema({"type": "object", "properties": {}}, required=[])
    assert validate_function_arguments(schema, None) == {
        "valid": True,
        "errors": [],
    }


def test_arguments_none_still_flags_missing_required():
    schema = _schema(
        {"type": "object", "properties": {"a": {"type": "string"}}},
        required=["a"],
    )
    result = validate_function_arguments(schema, None)
    assert result["valid"] is False
    assert "Missing required field: a" in result["errors"]


def test_unknown_type_is_accepted():
    assert _validate_type("anything", "weird-type") is True


def test_validate_type_matches_known_types():
    assert _validate_type("s", "string") is True
    assert _validate_type(1, "integer") is True
    assert _validate_type(1.5, "number") is True
    assert _validate_type([], "array") is True
    assert _validate_type({}, "object") is True
    assert _validate_type(True, "boolean") is True


def test_bool_rejected_as_integer_and_number():
    """``isinstance(True, int)`` is True in Python — a boolean must not
    silently satisfy an ``integer``/``number`` field."""
    assert _validate_type(True, "integer") is False
    assert _validate_type(False, "number") is False
