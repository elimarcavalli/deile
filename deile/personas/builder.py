"""Builder pattern para construção de personas personalizadas"""

import logging
from typing import Dict, List, Optional, Any
from pathlib import Path
import yaml

from .base import BasePersona, PersonaConfig, PersonaCapability, PersonaStyle

logger = logging.getLogger(__name__)


class PersonaBuilder:
    """Builder pattern para criação fluente de personas

    Permite construção step-by-step de personas com validação automática
    e configuração flexível.

    Example:
        builder = PersonaBuilder()
        config = (builder
            .with_name("MyPersona")
            .with_description("Custom persona for specific tasks")
            .add_capability(PersonaCapability.CODE_GENERATION)
            .with_communication_style(PersonaStyle.TECHNICAL)
            .with_system_instruction("You are a specialized assistant...")
            .build())
    """

    def __init__(self):
        self._reset()

    def _reset(self):
        """Reseta o builder para um estado limpo"""
        self._config_data = {
            "name": "",
            "description": "",
            "persona_id": "",
            "capabilities": [],
            "specializations": [],
            "expertise_level": 5,
            "communication_style": PersonaStyle.FRIENDLY,
            "formality_level": 5,
            "verbosity_level": 5,
            "system_instruction": "",
            "max_context_length": 8000,
            "temperature": 0.1,
            "use_tools": True,
            "auto_suggest_improvements": True,
            "tags": [],
            "author": None,
            "version": "1.0.0"
        }

    def with_name(self, name: str) -> 'PersonaBuilder':
        """Define o nome da persona"""
        self._config_data["name"] = name
        if not self._config_data["persona_id"]:
            # Gera persona_id automaticamente baseado no nome
            self._config_data["persona_id"] = self._name_to_id(name)
        return self

    def with_description(self, description: str) -> 'PersonaBuilder':
        """Define a descrição da persona"""
        self._config_data["description"] = description
        return self

    def with_persona_id(self, persona_id: str) -> 'PersonaBuilder':
        """Define o ID da persona (sobrescreve o gerado automaticamente)"""
        self._config_data["persona_id"] = persona_id
        return self

    def with_author(self, author: str) -> 'PersonaBuilder':
        """Define o autor da persona"""
        self._config_data["author"] = author
        return self

    def with_version(self, version: str) -> 'PersonaBuilder':
        """Define a versão da persona"""
        self._config_data["version"] = version
        return self

    def add_capability(self, capability: PersonaCapability) -> 'PersonaBuilder':
        """Adiciona uma capacidade à persona"""
        if capability not in self._config_data["capabilities"]:
            self._config_data["capabilities"].append(capability)
        return self

    def add_capabilities(self, capabilities: List[PersonaCapability]) -> 'PersonaBuilder':
        """Adiciona múltiplas capacidades à persona"""
        for capability in capabilities:
            self.add_capability(capability)
        return self

    def remove_capability(self, capability: PersonaCapability) -> 'PersonaBuilder':
        """Remove uma capacidade da persona"""
        if capability in self._config_data["capabilities"]:
            self._config_data["capabilities"].remove(capability)
        return self

    def add_specialization(self, specialization: str) -> 'PersonaBuilder':
        """Adiciona uma especialização à persona"""
        if specialization not in self._config_data["specializations"]:
            self._config_data["specializations"].append(specialization)
        return self

    def add_specializations(self, specializations: List[str]) -> 'PersonaBuilder':
        """Adiciona múltiplas especializações à persona"""
        for spec in specializations:
            self.add_specialization(spec)
        return self

    def with_expertise_level(self, level: int) -> 'PersonaBuilder':
        """Define o nível de expertise (1-10)"""
        if not 1 <= level <= 10:
            raise ValueError("Expertise level deve estar entre 1 e 10")
        self._config_data["expertise_level"] = level
        return self

    def with_communication_style(self, style: PersonaStyle) -> 'PersonaBuilder':
        """Define o estilo de comunicação"""
        self._config_data["communication_style"] = style
        return self

    def with_formality_level(self, level: int) -> 'PersonaBuilder':
        """Define o nível de formalidade (1-10)"""
        if not 1 <= level <= 10:
            raise ValueError("Formality level deve estar entre 1 e 10")
        self._config_data["formality_level"] = level
        return self

    def with_verbosity_level(self, level: int) -> 'PersonaBuilder':
        """Define o nível de verbosidade (1-10)"""
        if not 1 <= level <= 10:
            raise ValueError("Verbosity level deve estar entre 1 e 10")
        self._config_data["verbosity_level"] = level
        return self

    def with_system_instruction(self, instruction: str) -> 'PersonaBuilder':
        """Define a instrução do sistema"""
        self._config_data["system_instruction"] = instruction
        return self

    def with_greeting_template(self, template: str) -> 'PersonaBuilder':
        """Define template de saudação personalizada"""
        self._config_data["greeting_template"] = template
        return self

    def with_task_approach_template(self, template: str) -> 'PersonaBuilder':
        """Define template de abordagem de tarefas"""
        self._config_data["task_approach_template"] = template
        return self

    def with_max_context_length(self, length: int) -> 'PersonaBuilder':
        """Define o comprimento máximo do contexto"""
        if length < 1000:
            raise ValueError("Max context length deve ser pelo menos 1000")
        self._config_data["max_context_length"] = length
        return self

    def with_temperature(self, temperature: float) -> 'PersonaBuilder':
        """Define a temperatura para geração"""
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("Temperature deve estar entre 0.0 e 2.0")
        self._config_data["temperature"] = temperature
        return self

    def with_tools_enabled(self, enabled: bool = True) -> 'PersonaBuilder':
        """Define se a persona pode usar ferramentas"""
        self._config_data["use_tools"] = enabled
        return self

    def with_auto_improvements(self, enabled: bool = True) -> 'PersonaBuilder':
        """Define se a persona sugere melhorias automaticamente"""
        self._config_data["auto_suggest_improvements"] = enabled
        return self

    def add_tag(self, tag: str) -> 'PersonaBuilder':
        """Adiciona uma tag à persona"""
        if tag not in self._config_data["tags"]:
            self._config_data["tags"].append(tag)
        return self

    def add_tags(self, tags: List[str]) -> 'PersonaBuilder':
        """Adiciona múltiplas tags à persona"""
        for tag in tags:
            self.add_tag(tag)
        return self

    def for_developer(self) -> 'PersonaBuilder':
        """Configura como persona de desenvolvedor (preset)"""
        return (self
            .add_capabilities([
                PersonaCapability.CODE_GENERATION,
                PersonaCapability.DEBUGGING,
                PersonaCapability.CODE_REVIEW,
                PersonaCapability.TESTING
            ])
            .add_specializations(["Python", "JavaScript", "API Development", "Database Design"])
            .with_communication_style(PersonaStyle.TECHNICAL)
            .with_expertise_level(8)
            .add_tags(["developer", "programming", "technical"]))

    def for_architect(self) -> 'PersonaBuilder':
        """Configura como persona de arquiteto (preset)"""
        return (self
            .add_capabilities([
                PersonaCapability.ARCHITECTURE_DESIGN,
                PersonaCapability.CODE_REVIEW,
                PersonaCapability.OPTIMIZATION,
                PersonaCapability.DOCUMENTATION
            ])
            .add_specializations(["System Architecture", "Design Patterns", "Scalability", "Clean Code"])
            .with_communication_style(PersonaStyle.EXPERT)
            .with_expertise_level(9)
            .add_tags(["architect", "design", "patterns"]))

    def for_debugger(self) -> 'PersonaBuilder':
        """Configura como persona de debugger (preset)"""
        return (self
            .add_capabilities([
                PersonaCapability.DEBUGGING,
                PersonaCapability.PROBLEM_SOLVING,
                PersonaCapability.TESTING,
                PersonaCapability.CODE_REVIEW
            ])
            .add_specializations(["Error Analysis", "Performance Debugging", "Test Debugging", "Log Analysis"])
            .with_communication_style(PersonaStyle.TECHNICAL)
            .with_expertise_level(8)
            .add_tags(["debugging", "troubleshooting", "analysis"]))

    def for_mentor(self) -> 'PersonaBuilder':
        """Configura como persona de mentor (preset)"""
        return (self
            .add_capabilities([
                PersonaCapability.MENTORING,
                PersonaCapability.DOCUMENTATION,
                PersonaCapability.CODE_REVIEW,
                PersonaCapability.PROBLEM_SOLVING
            ])
            .add_specializations(["Teaching", "Code Reviews", "Best Practices", "Career Guidance"])
            .with_communication_style(PersonaStyle.MENTOR)
            .with_expertise_level(9)
            .with_verbosity_level(7)
            .add_tags(["mentor", "teaching", "guidance"]))

    def for_security_expert(self) -> 'PersonaBuilder':
        """Configura como persona de especialista em segurança (preset)"""
        return (self
            .add_capabilities([
                PersonaCapability.SECURITY_ANALYSIS,
                PersonaCapability.CODE_REVIEW,
                PersonaCapability.TESTING,
                PersonaCapability.DOCUMENTATION
            ])
            .add_specializations(["Security Analysis", "Vulnerability Assessment", "Secure Coding", "Penetration Testing"])
            .with_communication_style(PersonaStyle.EXPERT)
            .with_expertise_level(9)
            .add_tags(["security", "vulnerability", "analysis"]))

    def build(self) -> PersonaConfig:
        """Constrói a configuração da persona

        Returns:
            PersonaConfig: Configuração validada da persona

        Raises:
            ValueError: Se a configuração está incompleta ou inválida
        """
        # Validações básicas antes de criar o objeto
        if not self._config_data["name"]:
            raise ValueError("Nome da persona é obrigatório")

        if not self._config_data["description"]:
            raise ValueError("Descrição da persona é obrigatória")

        if not self._config_data["system_instruction"]:
            # Gera system instruction básica se não fornecida
            self._generate_default_system_instruction()

        if not self._config_data["capabilities"]:
            raise ValueError("Persona deve ter pelo menos uma capacidade")

        try:
            # Cria e valida a configuração
            config = PersonaConfig(**self._config_data)
            logger.debug(f"Persona '{config.name}' construída com sucesso")

            # Reseta o builder para próximo uso
            self._reset()

            return config

        except Exception as e:
            logger.error(f"Erro ao construir persona: {e}")
            raise ValueError(f"Configuração inválida: {e}")

    def build_and_save(self, file_path: Path) -> PersonaConfig:
        """Constrói a persona e salva em arquivo YAML

        Args:
            file_path: Caminho onde salvar a configuração

        Returns:
            PersonaConfig: Configuração construída
        """
        config = self.build()

        # Converte enum values para strings para serialização
        config_dict = config.dict()
        config_dict["capabilities"] = [cap.value for cap in config.capabilities]
        config_dict["communication_style"] = config.communication_style.value

        # Salva em arquivo YAML
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True, indent=2)

        logger.info(f"Persona '{config.name}' salva em {file_path}")
        return config

    def _name_to_id(self, name: str) -> str:
        """Converte nome para ID válido"""
        # Remove caracteres especiais e converte para lowercase
        import re
        clean_name = re.sub(r'[^\w\s-]', '', name.strip())
        persona_id = re.sub(r'[-\s]+', '_', clean_name).lower()
        return persona_id

    def _generate_default_system_instruction(self):
        """Gera system instruction padrão baseada nas capacidades"""
        name = self._config_data["name"]
        capabilities = self._config_data["capabilities"]
        specializations = self._config_data["specializations"]

        instruction_parts = [
            f"Você é {name}, um assistente de IA especializado."
        ]

        if capabilities:
            cap_names = [cap.value.replace('_', ' ').title() for cap in capabilities]
            instruction_parts.append(f"Suas principais capacidades incluem: {', '.join(cap_names)}.")

        if specializations:
            instruction_parts.append(f"Você tem expertise em: {', '.join(specializations)}.")

        instruction_parts.extend([
            "Seu objetivo é ajudar o usuário da melhor forma possível usando suas especializações.",
            "Seja preciso, útil e mantenha-se dentro de suas áreas de competência."
        ])

        self._config_data["system_instruction"] = " ".join(instruction_parts)


def create_developer_persona(name: str = "Developer") -> PersonaConfig:
    """Função helper para criar rapidamente uma persona de desenvolvedor"""
    return (PersonaBuilder()
        .with_name(name)
        .with_description("Especialista em desenvolvimento de software e programação")
        .for_developer()
        .build())


def create_architect_persona(name: str = "Architect") -> PersonaConfig:
    """Função helper para criar rapidamente uma persona de arquiteto"""
    return (PersonaBuilder()
        .with_name(name)
        .with_description("Arquiteto de software focado em design de sistemas")
        .for_architect()
        .build())


def create_debugger_persona(name: str = "Debugger") -> PersonaConfig:
    """Função helper para criar rapidamente uma persona de debugger"""
    return (PersonaBuilder()
        .with_name(name)
        .with_description("Especialista em debugging e resolução de problemas")
        .for_debugger()
        .build())


def create_custom_persona(name: str, description: str, capabilities: List[PersonaCapability],
                         specializations: List[str] = None, **kwargs) -> PersonaConfig:
    """Função helper para criar persona customizada rapidamente"""
    builder = (PersonaBuilder()
        .with_name(name)
        .with_description(description)
        .add_capabilities(capabilities))

    if specializations:
        builder.add_specializations(specializations)

    # Aplica configurações adicionais
    for key, value in kwargs.items():
        method_name = f"with_{key}"
        if hasattr(builder, method_name):
            method = getattr(builder, method_name)
            builder = method(value)

    return builder.build()