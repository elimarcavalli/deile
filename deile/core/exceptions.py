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