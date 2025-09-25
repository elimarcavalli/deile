"""Exceções customizadas do DEILE"""

from typing import Optional, Any, Dict


class DEILEError(Exception):
    """Exceção base do DEILE"""
    
    def __init__(
        self, 
        message: str, 
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.context = context or {}
    
    def __str__(self) -> str:
        if self.error_code:
            return f"[{self.error_code}] {self.message}"
        return self.message


class ToolError(DEILEError):
    """Erro relacionado à execução de Tools"""
    
    def __init__(
        self, 
        message: str, 
        tool_name: Optional[str] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.tool_name = tool_name
        if tool_name:
            self.context["tool_name"] = tool_name


class ParserError(DEILEError):
    """Erro relacionado ao parsing de entrada"""
    
    def __init__(
        self, 
        message: str, 
        input_text: Optional[str] = None,
        parser_name: Optional[str] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.input_text = input_text
        self.parser_name = parser_name
        if input_text:
            self.context["input_text"] = input_text[:100]  # Limit for logging
        if parser_name:
            self.context["parser_name"] = parser_name


class ModelError(DEILEError):
    """Erro relacionado aos modelos de IA"""
    
    def __init__(
        self, 
        message: str, 
        model_name: Optional[str] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.model_name = model_name
        if model_name:
            self.context["model_name"] = model_name


class ConfigurationError(DEILEError):
    """Erro de configuração do sistema"""
    
    def __init__(
        self, 
        message: str, 
        config_key: Optional[str] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.config_key = config_key
        if config_key:
            self.context["config_key"] = config_key


class ValidationError(DEILEError):
    """Erro de validação de dados"""
    
    def __init__(
        self, 
        message: str, 
        field_name: Optional[str] = None,
        field_value: Optional[Any] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.field_name = field_name
        self.field_value = field_value
        if field_name:
            self.context["field_name"] = field_name
        if field_value is not None:
            self.context["field_value"] = str(field_value)[:50]  # Limit for logging


class CommandError(DEILEError):
    """Erro relacionado à execução de comandos slash"""

    def __init__(
        self,
        message: str,
        command_name: Optional[str] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.command_name = command_name
        if command_name:
            self.context["command_name"] = command_name


class PersonaError(DEILEError):
    """Exceção base para erros relacionados ao sistema de personas"""

    def __init__(
        self,
        message: str,
        persona_id: Optional[str] = None,
        operation: Optional[str] = None,
        recovery_suggestion: Optional[str] = None,
        error_code: Optional[str] = None,
        **kwargs
    ):
        # Input validation
        if not isinstance(message, str):
            message = str(message) if message is not None else "Unknown error"

        super().__init__(message, error_code=error_code, **kwargs)
        self.persona_id = persona_id
        self.operation = operation
        self.recovery_suggestion = recovery_suggestion

        # Add persona context with safe serialization
        if persona_id:
            self.context["persona_id"] = str(persona_id)
        if operation:
            self.context["operation"] = str(operation)
        if recovery_suggestion:
            self.context["recovery_suggestion"] = str(recovery_suggestion)

    def to_dict(self) -> Dict[str, Any]:
        """Convert error to dictionary for logging/serialization"""
        base_dict = {
            'message': self.message,
            'error_code': self.error_code,
            'context': self.context,
            'persona_id': self.persona_id,
            'operation': self.operation,
            'recovery_suggestion': self.recovery_suggestion
        }
        return base_dict


class PersonaLoadError(PersonaError):
    """Erro ao carregar ou inicializar persona"""

    def __init__(
        self,
        message: str,
        persona_id: str,
        config_path: Optional[str] = None,
        **kwargs
    ):
        super().__init__(
            message,
            persona_id=persona_id,
            operation="load_persona",
            error_code="PERSONA_LOAD_FAILED",
            **kwargs
        )
        self.config_path = config_path
        if config_path:
            self.context["config_path"] = config_path


class PersonaSwitchError(PersonaError):
    """Erro ao alternar entre personas"""

    def __init__(
        self,
        message: str,
        from_persona: str,
        to_persona: str,
        **kwargs
    ):
        super().__init__(
            message,
            persona_id=to_persona,
            operation="switch_persona",
            error_code="PERSONA_SWITCH_FAILED",
            **kwargs
        )
        self.from_persona = from_persona
        self.to_persona = to_persona
        self.context.update({
            "from_persona": from_persona,
            "to_persona": to_persona
        })


class PersonaConfigError(PersonaError):
    """Erro na configuração da persona"""

    def __init__(
        self,
        message: str,
        persona_id: str,
        config_key: Optional[str] = None,
        **kwargs
    ):
        super().__init__(
            message,
            persona_id=persona_id,
            operation="validate_config",
            error_code="PERSONA_CONFIG_INVALID",
            **kwargs
        )
        self.config_key = config_key
        if config_key:
            self.context["config_key"] = config_key


class PersonaExecutionError(PersonaError):
    """Erro durante execução de capacidade da persona"""

    def __init__(
        self,
        message: str,
        persona_id: str,
        capability: str,
        **kwargs
    ):
        super().__init__(
            message,
            persona_id=persona_id,
            operation=f"execute_capability:{capability}",
            error_code="PERSONA_EXECUTION_FAILED",
            **kwargs
        )
        self.capability = capability
        self.context["capability"] = capability


class PersonaInitializationError(PersonaError):
    """Erro durante inicialização da persona"""

    def __init__(
        self,
        message: str,
        persona_id: str,
        initialization_step: Optional[str] = None,
        **kwargs
    ):
        super().__init__(
            message,
            persona_id=persona_id,
            operation="initialize_persona",
            error_code="PERSONA_INIT_FAILED",
            **kwargs
        )
        self.initialization_step = initialization_step
        if initialization_step:
            self.context["initialization_step"] = initialization_step


class PersonaIntegrationError(PersonaError):
    """Erro na integração da persona com outros sistemas DEILE"""

    def __init__(
        self,
        message: str,
        persona_id: str,
        integration_component: str,
        **kwargs
    ):
        super().__init__(
            message,
            persona_id=persona_id,
            operation=f"integrate_with:{integration_component}",
            error_code="PERSONA_INTEGRATION_FAILED",
            **kwargs
        )
        self.integration_component = integration_component
        self.context["integration_component"] = integration_component