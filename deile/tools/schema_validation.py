"""Validação de argumentos de function-call contra ``ToolSchema``.

Extraído de :class:`~deile.tools.registry.ToolRegistry` para isolar a
responsabilidade de validação de schema do registro/descoberta de tools
(SRP). O registry delega a estas funções — nenhum estado de registry é
necessário, então elas vivem como funções de módulo.
"""

from __future__ import annotations

from typing import Any, Dict

from .base import ToolSchema

# Nome de tipo JSON-schema -> tipo(s) Python aceitável(eis).
_TYPE_MAPPING: Dict[str, Any] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_type(value: Any, expected_type: str) -> bool:
    """Valida um valor contra um nome de tipo JSON-schema.

    Tipos desconhecidos são aceitos (retorna ``True``) — a checagem é
    deliberadamente tolerante.
    """
    expected_python_type = _TYPE_MAPPING.get(expected_type)
    if expected_python_type is None:
        return True
    return isinstance(value, expected_python_type)


def validate_function_arguments(
    schema: ToolSchema, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """Valida ``arguments`` contra ``schema``.

    Retorna ``{"valid": bool, "errors": List[str]}``. Checa campos
    obrigatórios ausentes e os tipos básicos das propriedades declaradas.

    Os campos obrigatórios vêm de ``schema.required`` — a fonte autoritativa
    que todos os conversores ``ToolSchema.to_*`` consomem. O sub-dict
    ``parameters`` carrega apenas ``properties`` no padrão canônico (ver
    ``MessagingTool``), então ``parameters["required"]`` não pode ser usado.
    """
    errors = []
    required_fields = schema.required or []
    properties = schema.parameters.get("properties", {})

    for field in required_fields:
        if field not in arguments:
            errors.append(f"Missing required field: {field}")

    for field, value in arguments.items():
        if field in properties:
            expected_type = properties[field].get("type")
            if expected_type and not validate_type(value, expected_type):
                errors.append(
                    f"Invalid type for field {field}: expected {expected_type}"
                )

    return {"valid": len(errors) == 0, "errors": errors}
