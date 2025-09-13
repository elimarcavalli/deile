"""Gerenciador de personas com hot-reload e ciclo de vida completo"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import yaml
import json
from datetime import datetime

from .base import BasePersona, PersonaConfig, PersonaCapability
from .loader import PersonaLoader
from ..core.exceptions import DEILEError

logger = logging.getLogger(__name__)


class PersonaError(DEILEError):
    """Exceções específicas do sistema de personas"""
    pass


class PersonaConfigHandler(FileSystemEventHandler):
    """Handler para mudanças nos arquivos de configuração de personas"""

    def __init__(self, manager: 'PersonaManager'):
        self.manager = manager
        super().__init__()

    def on_modified(self, event):
        """Chamado quando um arquivo de configuração é modificado"""
        if event.is_directory:
            return

        if event.src_path.endswith(('.yaml', '.yml', '.json')):
            logger.info(f"Configuração de persona modificada: {event.src_path}")
            asyncio.create_task(self.manager.reload_persona_from_file(event.src_path))


class PersonaManager:
    """Gerenciador central de personas com capacidades enterprise-grade

    Features:
    - Hot-reload de configurações
    - Ciclo de vida completo das personas
    - Validação e verificação de integridade
    - Métricas e monitoramento
    - Auto-discovery de personas
    """

    def __init__(self, personas_dir: Optional[Path] = None):
        self.personas_dir = personas_dir or Path("deile/personas/library")
        self.personas_dir.mkdir(parents=True, exist_ok=True)

        # Storage de personas ativas
        self._personas: Dict[str, BasePersona] = {}
        self._persona_configs: Dict[str, PersonaConfig] = {}
        self._active_persona: Optional[str] = None

        # Hot-reload setup
        self._observer: Optional[Observer] = None
        self._hot_reload_enabled = False

        # Loader para carregar personas dinamicamente
        self.loader = PersonaLoader()

        # Métricas do manager
        self._total_switches = 0
        self._last_reload_time = 0.0

        logger.info(f"PersonaManager inicializado. Diretório: {self.personas_dir}")

    async def initialize(self, enable_hot_reload: bool = True) -> None:
        """Inicializa o manager e carrega todas as personas disponíveis"""
        logger.info("Inicializando PersonaManager...")

        try:
            # Carrega todas as personas do diretório
            await self.discover_and_load_personas()

            # Configura hot-reload se solicitado
            if enable_hot_reload:
                await self.enable_hot_reload()

            logger.info(f"PersonaManager inicializado com {len(self._personas)} personas")

        except Exception as e:
            logger.error(f"Erro na inicialização do PersonaManager: {e}")
            raise PersonaError(f"Falha na inicialização: {e}")

    async def discover_and_load_personas(self) -> None:
        """Descobre e carrega todas as personas disponíveis"""
        config_files = list(self.personas_dir.glob("*.yaml")) + list(self.personas_dir.glob("*.yml"))

        if not config_files:
            logger.warning(f"Nenhuma configuração de persona encontrada em {self.personas_dir}")
            await self.create_default_personas()
            return

        for config_file in config_files:
            try:
                await self.load_persona_from_file(config_file)
            except Exception as e:
                logger.error(f"Erro ao carregar persona de {config_file}: {e}")

    async def load_persona_from_file(self, config_path: Path) -> str:
        """Carrega uma persona de um arquivo de configuração

        Args:
            config_path: Caminho para o arquivo de configuração

        Returns:
            str: ID da persona carregada
        """
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)

            # Cria configuração validada
            config = PersonaConfig(**config_data)

            # Carrega a persona usando o loader
            persona = await self.loader.load_persona(config)

            # Armazena no manager
            self._personas[config.persona_id] = persona
            self._persona_configs[config.persona_id] = config

            logger.info(f"Persona '{config.name}' carregada com sucesso ({config.persona_id})")
            return config.persona_id

        except Exception as e:
            logger.error(f"Erro ao carregar persona de {config_path}: {e}")
            raise PersonaError(f"Falha ao carregar {config_path}: {e}")

    async def reload_persona_from_file(self, config_path: str) -> None:
        """Recarrega uma persona quando o arquivo é modificado"""
        try:
            path = Path(config_path)
            if path.exists() and path.suffix in ['.yaml', '.yml']:
                persona_id = await self.load_persona_from_file(path)
                logger.info(f"Persona {persona_id} recarregada via hot-reload")
                self._last_reload_time = datetime.now().timestamp()

        except Exception as e:
            logger.error(f"Erro no hot-reload de {config_path}: {e}")

    async def enable_hot_reload(self) -> None:
        """Habilita hot-reload de configurações de personas"""
        if self._hot_reload_enabled:
            return

        try:
            self._observer = Observer()
            event_handler = PersonaConfigHandler(self)
            self._observer.schedule(event_handler, str(self.personas_dir), recursive=False)
            self._observer.start()
            self._hot_reload_enabled = True

            logger.info("Hot-reload de personas habilitado")

        except Exception as e:
            logger.error(f"Erro ao habilitar hot-reload: {e}")
            raise PersonaError(f"Falha no hot-reload: {e}")

    async def disable_hot_reload(self) -> None:
        """Desabilita hot-reload"""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            self._hot_reload_enabled = False
            logger.info("Hot-reload de personas desabilitado")

    def get_persona(self, persona_id: str) -> Optional[BasePersona]:
        """Obtém uma persona pelo ID"""
        return self._personas.get(persona_id)

    def get_active_persona(self) -> Optional[BasePersona]:
        """Obtém a persona atualmente ativa"""
        if self._active_persona:
            return self._personas.get(self._active_persona)
        return None

    async def switch_persona(self, persona_id: str) -> bool:
        """Muda para uma persona específica

        Args:
            persona_id: ID da persona para ativar

        Returns:
            bool: True se a mudança foi bem-sucedida
        """
        if persona_id not in self._personas:
            logger.error(f"Persona '{persona_id}' não encontrada")
            return False

        try:
            # Desativa persona atual se houver
            if self._active_persona:
                current_persona = self._personas.get(self._active_persona)
                if current_persona:
                    current_persona.deactivate()

            # Ativa nova persona
            new_persona = self._personas[persona_id]
            new_persona.activate()
            self._active_persona = persona_id
            self._total_switches += 1

            logger.info(f"Mudança para persona '{new_persona.name}' ({persona_id})")
            return True

        except Exception as e:
            logger.error(f"Erro ao mudar para persona {persona_id}: {e}")
            return False

    def list_personas(self) -> List[Dict[str, Any]]:
        """Lista todas as personas disponíveis"""
        return [
            {
                "persona_id": persona_id,
                "name": persona.name,
                "capabilities": [cap.value for cap in persona.capabilities],
                "expertise_areas": persona.expertise_areas,
                "is_active": persona.is_active,
                "communication_style": persona.config.communication_style.value,
                "expertise_level": persona.config.expertise_level
            }
            for persona_id, persona in self._personas.items()
        ]

    def list_by_capability(self, capability: PersonaCapability) -> List[str]:
        """Lista personas que possuem uma capacidade específica"""
        return [
            persona_id for persona_id, persona in self._personas.items()
            if capability in persona.capabilities
        ]

    async def find_best_persona_for_task(self, task_description: str, required_capabilities: List[PersonaCapability] = None) -> Optional[str]:
        """Encontra a melhor persona para uma tarefa específica

        Args:
            task_description: Descrição da tarefa
            required_capabilities: Capacidades obrigatórias

        Returns:
            Optional[str]: ID da melhor persona ou None se nenhuma adequada
        """
        candidates = []

        for persona_id, persona in self._personas.items():
            if persona.can_handle_task(task_description, required_capabilities):
                # Calcula score baseado em expertise e métricas
                score = persona.config.expertise_level
                if persona.metrics.success_rate > 0:
                    score += persona.metrics.success_rate / 10  # Bonus por histórico

                candidates.append((persona_id, score))

        if not candidates:
            return None

        # Retorna a persona com maior score
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    async def validate_all_personas(self) -> Dict[str, List[str]]:
        """Valida todas as personas carregadas

        Returns:
            Dict[str, List[str]]: Mapeamento persona_id -> lista de erros
        """
        validation_results = {}

        for persona_id, persona in self._personas.items():
            errors = await persona.validate_config()
            if errors:
                validation_results[persona_id] = errors

        return validation_results

    async def get_manager_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do manager"""
        return {
            "total_personas": len(self._personas),
            "active_persona": self._active_persona,
            "total_switches": self._total_switches,
            "hot_reload_enabled": self._hot_reload_enabled,
            "last_reload_time": self._last_reload_time,
            "personas_by_capability": {
                cap.value: len(self.list_by_capability(cap))
                for cap in PersonaCapability
            },
            "directory": str(self.personas_dir)
        }

    async def create_default_personas(self) -> None:
        """Cria personas padrão se nenhuma existir"""
        logger.info("Criando personas padrão...")

        default_personas = [
            {
                "name": "Developer",
                "description": "Especialista em desenvolvimento de software e programação",
                "persona_id": "developer",
                "capabilities": ["code_generation", "debugging", "testing", "code_review"],
                "specializations": ["Python", "JavaScript", "API Development", "Database Design"],
                "expertise_level": 8,
                "communication_style": "technical",
                "system_instruction": "Você é um desenvolvedor de software experiente, especializado em Python e desenvolvimento de APIs. Seu objetivo é ajudar com codificação, debugging, e melhores práticas de desenvolvimento. Seja preciso, técnico e forneça exemplos de código quando apropriado."
            },
            {
                "name": "Architect",
                "description": "Arquiteto de software focado em design de sistemas",
                "persona_id": "architect",
                "capabilities": ["architecture_design", "code_review", "optimization", "documentation"],
                "specializations": ["System Architecture", "Design Patterns", "Scalability", "Clean Code"],
                "expertise_level": 9,
                "communication_style": "expert",
                "system_instruction": "Você é um arquiteto de software sênior, especializado em design de sistemas escaláveis e manutenção. Foque em arquitetura limpa, padrões de design, e decisões arquiteturais estratégicas. Sempre considere escalabilidade, manutenibilidade e performance."
            },
            {
                "name": "Debugger",
                "description": "Especialista em debugging e resolução de problemas",
                "persona_id": "debugger",
                "capabilities": ["debugging", "problem_solving", "testing", "code_review"],
                "specializations": ["Error Analysis", "Performance Debugging", "Test Debugging", "Log Analysis"],
                "expertise_level": 8,
                "communication_style": "technical",
                "system_instruction": "Você é um especialista em debugging e resolução de problemas. Sua abordagem é sistemática: analise o erro, identifique a causa raiz, proponha soluções e sugira prevenção. Seja metodico e forneça passos claros para resolução."
            }
        ]

        for persona_data in default_personas:
            config_path = self.personas_dir / f"{persona_data['persona_id']}.yaml"
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(persona_data, f, default_flow_style=False, allow_unicode=True, indent=2)

            logger.info(f"Persona padrão criada: {config_path}")

        # Carrega as personas criadas
        await self.discover_and_load_personas()

    async def shutdown(self) -> None:
        """Finaliza o manager e limpa recursos"""
        logger.info("Finalizando PersonaManager...")

        # Desabilita hot-reload
        await self.disable_hot_reload()

        # Desativa todas as personas
        for persona in self._personas.values():
            persona.deactivate()

        # Limpa storage
        self._personas.clear()
        self._persona_configs.clear()
        self._active_persona = None

        logger.info("PersonaManager finalizado")

    def __del__(self):
        """Destructor que garante limpeza dos recursos"""
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join()