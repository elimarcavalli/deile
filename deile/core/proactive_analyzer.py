"""Sistema de análise proativa para execução automática de ferramentas contextuais

Enhanced ProactiveAnalyzer com capacidades de resolução inteligente de arquivos
e encadeamento de ações para autonomia completa no DEILE.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from ..tools.base import ToolContext
from .file_resolver import FileMatch, get_file_resolver

logger = logging.getLogger(__name__)


class ProactiveAction(Enum):
    """Tipos de ações proativas disponíveis"""

    READ_FILE = "read_file"
    LIST_FILES = "list_files"
    LIST_DIRECTORY = "list_directory"
    CHECK_FILE_EXISTS = "check_file_exists"
    # New autonomous actions
    SUGGEST_ALTERNATIVES = "suggest_alternatives"
    CHAIN_LIST_AND_READ = "chain_list_and_read"


@dataclass
class ProactiveIntent:
    """Intenção proativa detectada na entrada do usuário"""

    action: ProactiveAction
    target: str
    confidence: float
    context: str
    priority: int = 1
    # New fields for enhanced functionality
    resolved_file: Optional[FileMatch] = None
    chained_actions: List["ProactiveIntent"] = field(default_factory=list)
    autonomous_eligible: bool = False

    def resolve_target(self, resolver=None) -> Optional[FileMatch]:
        """Resolve the target using SmartFileResolver.

        ``resolver`` may be passed by the caller so resolution happens against
        the analyzer's configured workspace; otherwise a CWD-default resolver
        is used as a fallback.
        """
        if self.resolved_file is None and self.action in [
            ProactiveAction.READ_FILE,
            ProactiveAction.CHECK_FILE_EXISTS,
        ]:
            if resolver is None:
                resolver = get_file_resolver()
            self.resolved_file = resolver.get_best_match(self.target)
        return self.resolved_file


class ProactiveAnalyzer:
    """Analisador proativo que detecta quando ferramentas de arquivo devem ser executadas automaticamente"""

    def __init__(self, working_directory: str = "."):
        self.working_directory = Path(working_directory)
        self.logger = logging.getLogger(__name__)
        self.file_resolver = get_file_resolver(self.working_directory)

        # Enhanced patterns for file reading with better coverage
        self.file_read_patterns = [
            # Portuguese patterns
            r"(?:analise|examine|veja|olhe|leia|abra|verifique|confira)\s+(?:o\s+)?(?:arquivo|file)\s+([^\s]+)",
            r"(?:no|do)\s+arquivo\s+([^\s]+)",
            r"(?:o\s+que\s+(?:tem|está|há))\s+(?:no|em)\s+([^\s]+\.?[\w]*)",
            r"(?:conteúdo|contents?)\s+(?:do|of)\s+([^\s]+)",
            r"(?:mostra?|show)\s+(?:o\s+)?([^\s]+\.?[\w]*)",
            r"(?:código|code)\s+(?:em|in|do|of)\s+([^\s]+\.?[\w]*)",
            # English patterns
            r"(?:read|open|show|display|examine|check)\s+(?:the\s+)?([^\s]+)",
            r"(?:what'?s|what\s+is)\s+in\s+(?:the\s+)?([^\s]+)",
            r"(?:contents?\s+of|inside)\s+(?:the\s+)?([^\s]+)",
            r"(?:look\s+at|view)\s+(?:the\s+)?([^\s]+)",
            # Natural language patterns (high confidence). The verb prefix is
            # mandatory — without it, every mention of "main", "config", etc.
            # in casual conversation ("o que seria um bloco main?") would
            # be misclassified as a file-read request.
            r"(?:read|show|open)\s+(?:the\s+)?(readme|config|license|changelog|main|setup|requirements)\b",
            r"(?:what'?s\s+in\s+the\s+)(readme|config|license|changelog|main|setup|requirements)\b",
            r"(?:leia|examine)\s+o\s+(readme|config|license|changelog|main|setup|requirements)\b",
            # Known filename + explicit extension. The extension is what
            # distinguishes "README.md" / "main.py" / "config.yaml" from
            # the bare word "main" in casual chat.
            r"\b(readme|config|license|changelog|main|setup|requirements)\.(?:md|txt|yaml|yml|json|toml|cfg|ini|py|sh|rst|html|css|xml|csv)\b",
            # Multiple file patterns
            r"(?:compare|compare|confronte)\s+(?:os\s+)?(?:arquivos|files)\s+([^\s]+)\s+(?:e|and)\s+([^\s]+)",
            r"(?:examine|analise)\s+(?:os\s+)?(?:arquivos|files)\s+([^\s]+)\s+(?:e|and)\s+([^\s]+)",
        ]

        # Enhanced patterns for listing operations
        self.list_patterns = [
            # Basic listing
            r"(?:liste|list|mostre|show)\s+(?:os\s+)?(?:arquivos|files)",
            r"(?:quais|what)\s+(?:são\s+os\s+|files\s+)?(?:arquivos|files)\s*(?:are|estão)?\s*(?:here|aqui)?",
            r"(?:what\s+files|which\s+files)\s+(?:are\s+)?(?:here|available|aqui|disponíveis)?",
            # Directory-specific
            r"(?:arquivos|files)\s+(?:no|in)\s+(?:diretório|directory|pasta|folder)\s+([^\s]+)",
            r"(?:estrutura|structure)\s+(?:do\s+)?(?:projeto|project|diretório|directory)",
            r"(?:o\s+que\s+(?:tem|há|está))\s+(?:neste|nessa|no)\s+(?:diretório|directory|pasta|folder)",
            # Command-like patterns
            r"(?:ls|dir)(?:\s+.*)?$",
            r"(?:show\s+me\s+the\s+files)",
            r"(?:directory\s+contents?)",
            # Specific file type listing
            r"(?:mostre|show|liste|list)\s+(?:todos\s+os\s+)?(?:arquivos|files)\s+(?:python|py|javascript|js)",
            r"(?:todos\s+os\s+)?(?:arquivos|files)\s+(?:.*?)\s+(?:do\s+projeto|project)",
        ]

        # Padrões que sugerem análise de projeto
        self.project_analysis_patterns = [
            r"(?:analise|analyze)\s+(?:o\s+)?(?:projeto|project)",
            r"(?:arquitetura|architecture)\s+(?:do\s+)?(?:projeto|project)",
            r"(?:estrutura|structure)\s+(?:do\s+)?(?:projeto|project)",
            r"(?:componentes|components)\s+(?:do\s+)?(?:projeto|project)",
            r"(?:principais|main)\s+(?:arquivos|files)",
            r"(?:overview|visão\s+geral)\s+(?:do\s+)?(?:projeto|project)",
            r"(?:como\s+(?:funciona|works))\s+(?:este|this|o)\s+(?:projeto|project|sistema|system)",
        ]

        # Confidence thresholds for autonomous execution
        self.high_confidence_threshold = 0.8
        self.medium_confidence_threshold = 0.6
        self.autonomous_threshold = 0.7
        self.file_resolution_threshold = 0.8

    async def analyze(
        self, user_input: str, session_context: Dict = None
    ) -> List[ProactiveIntent]:
        """Async alias for ``analyze_input``.

        Provided so callers can ``await`` the analyzer without depending on the
        sync entry point.
        """
        return self.analyze_input(user_input, session_context)

    async def analyze_enhanced(
        self, user_input: str, session_context: Dict = None
    ) -> List[ProactiveIntent]:
        """Async alias for ``analyze_input`` (enhanced semantics are already
        included in the sync implementation)."""
        return self.analyze_input(user_input, session_context)

    def analyze_input(
        self, user_input: str, session_context: Dict = None
    ) -> List[ProactiveIntent]:
        """Enhanced analysis with smart file resolution and action chaining"""
        if session_context is None:
            session_context = {}

        intents = []
        user_input_lower = user_input.lower()

        # 1. Detect file read intents
        file_intents = self._detect_file_read_intents(user_input, user_input_lower)
        intents.extend(file_intents)

        # 2. Detect list intents
        list_intents = self._detect_list_intents(user_input, user_input_lower)
        intents.extend(list_intents)

        # 3. Detect project analysis intents
        project_intents = self._detect_project_analysis_intents(
            user_input, user_input_lower
        )
        intents.extend(project_intents)

        # 4. Resolve targets and determine autonomy eligibility
        for intent in intents:
            intent.resolve_target(self.file_resolver)
            intent.autonomous_eligible = self._is_autonomous_eligible(intent)

        # 5. Generate chained actions for ambiguous requests
        intents = self._generate_chained_actions(intents)

        # 6. Prioritize and filter intents
        intents = self._prioritize_intents(intents)

        return intents

    def _detect_file_read_intents(
        self, original_input: str, user_input_lower: str
    ) -> List[ProactiveIntent]:
        """Detect file reading intents with enhanced pattern matching"""
        intents = []

        for pattern in self.file_read_patterns:
            matches = re.finditer(pattern, original_input, re.IGNORECASE)
            for match in matches:
                if match.groups():
                    # Process all captured groups (for multiple files)
                    for group_idx in range(1, len(match.groups()) + 1):
                        target = match.group(group_idx)
                        if target:
                            target = target.strip()

                            # Calculate confidence based on pattern specificity
                            confidence = self._calculate_enhanced_confidence(
                                pattern, match, original_input, target
                            )

                            intent = ProactiveIntent(
                                action=ProactiveAction.READ_FILE,
                                target=target,
                                confidence=confidence,
                                context=original_input,
                                priority=(
                                    2
                                    if confidence > self.high_confidence_threshold
                                    else 1
                                ),
                            )
                            intents.append(intent)

        return intents

    def _detect_list_intents(
        self, original_input: str, user_input_lower: str
    ) -> List[ProactiveIntent]:
        """Detect directory listing intents"""
        intents = []

        for pattern in self.list_patterns:
            matches = re.finditer(pattern, original_input, re.IGNORECASE)
            for match in matches:
                # Determine target directory
                target_dir = "."
                if match.groups() and match.group(1):
                    target_dir = match.group(1).strip()

                confidence = self._calculate_enhanced_confidence(
                    pattern, match, original_input, target_dir, "list"
                )

                intent = ProactiveIntent(
                    action=ProactiveAction.LIST_DIRECTORY,
                    target=target_dir,
                    confidence=confidence,
                    context=original_input,
                    priority=1,
                )
                intents.append(intent)
                break  # Only one list intent per input

        return intents

    def _detect_project_analysis_intents(
        self, original_input: str, user_input_lower: str
    ) -> List[ProactiveIntent]:
        """Detect general project analysis intents"""
        intents = []

        for pattern in self.project_analysis_patterns:
            if re.search(pattern, original_input, re.IGNORECASE):
                confidence = self._calculate_enhanced_confidence(
                    pattern, None, original_input, ".", "project_analysis"
                )

                # For project analysis, list main files
                intent = ProactiveIntent(
                    action=ProactiveAction.LIST_DIRECTORY,
                    target=".",
                    confidence=confidence,
                    context=original_input,
                    priority=1,
                )
                intents.append(intent)

                # Also suggest reading key project files
                key_files = self._find_key_project_files()
                for file_path in key_files:
                    intent = ProactiveIntent(
                        action=ProactiveAction.READ_FILE,
                        target=file_path,
                        confidence=confidence * 0.8,  # Slightly lower confidence
                        context=f"Key project file: '{file_path}'",
                        priority=3,
                    )
                    intents.append(intent)

        return intents

    def _calculate_enhanced_confidence(
        self,
        pattern: str,
        match: Optional[re.Match],
        user_input: str,
        target: str,
        intent_type: str = "file_read",
    ) -> float:
        """Calculate enhanced confidence score for a pattern match"""
        base_confidence = 0.7

        # Boost confidence for specific high-value patterns
        high_confidence_indicators = {
            "readme": 0.2,
            "config": 0.15,
            "license": 0.1,
            "main": 0.15,
            "requirements": 0.15,
            "setup": 0.1,
            "package": 0.1,
        }

        # Check if target matches high-value patterns
        target_lower = target.lower()
        for indicator, boost in high_confidence_indicators.items():
            if indicator in target_lower:
                base_confidence += boost
                break

        # Boost confidence for explicit action words
        action_words = ["read", "show", "open", "examine", "leia", "mostre", "analise"]
        if any(word in user_input.lower() for word in action_words):
            base_confidence += 0.1

        # Boost confidence for natural language patterns
        if any(
            keyword in pattern for keyword in ["readme", "config", "license", "main"]
        ):
            base_confidence += 0.15

        # Penalize very short targets unless they're known patterns
        if len(target.strip()) < 3 and target_lower not in high_confidence_indicators:
            base_confidence -= 0.15

        # Special handling for different intent types
        if intent_type == "list":
            base_confidence += 0.1  # List operations are generally safer
        elif intent_type == "project_analysis":
            base_confidence += 0.15  # Project analysis is high-value

        return min(base_confidence, 0.95)

    def _is_autonomous_eligible(self, intent: ProactiveIntent) -> bool:
        """Determine if an intent is eligible for autonomous execution"""
        # Must have high confidence
        if intent.confidence < self.autonomous_threshold:
            return False

        # Check file resolution for read operations
        if intent.action == ProactiveAction.READ_FILE:
            if (
                intent.resolved_file
                and intent.resolved_file.confidence >= self.file_resolution_threshold
            ):
                return True
            # Even without perfect resolution, allow if confidence is very high
            elif intent.confidence >= 0.9:
                return True

        # List operations are always autonomous eligible if confidence is high
        if intent.action == ProactiveAction.LIST_DIRECTORY:
            return True

        return False

    def _generate_chained_actions(
        self, intents: List[ProactiveIntent]
    ) -> List[ProactiveIntent]:
        """Generate chained actions for ambiguous file requests"""
        enhanced_intents = []

        for intent in intents:
            enhanced_intents.append(intent)

            # If file read intent but no good resolution, chain list and suggest actions
            if (
                intent.action == ProactiveAction.READ_FILE
                and intent.confidence >= self.medium_confidence_threshold
                and (
                    not intent.resolved_file
                    or intent.resolved_file.confidence < self.file_resolution_threshold
                )
            ):

                # Add chained list action
                list_intent = ProactiveIntent(
                    action=ProactiveAction.LIST_DIRECTORY,
                    target=".",
                    confidence=0.6,
                    context=f"Chained from: {intent.context}",
                    priority=1,
                )

                # Add suggest alternatives action
                suggest_intent = ProactiveIntent(
                    action=ProactiveAction.SUGGEST_ALTERNATIVES,
                    target=intent.target,
                    confidence=0.7,
                    context=f"Alternatives for: {intent.target}",
                    priority=1,
                )

                intent.chained_actions = [list_intent, suggest_intent]
                enhanced_intents.extend([list_intent, suggest_intent])

        return enhanced_intents

    def _is_valid_file_reference(self, file_path: str) -> bool:
        """Valida se a referência parece ser um arquivo real"""
        # Remove caracteres especiais e verifica formato básico
        chars_to_strip = ".,!?;\"'()[]{}"
        file_path = file_path.strip(chars_to_strip)

        # Allow references without extensions (common patterns like 'readme', 'config')
        if "." not in file_path:
            # Check if it's a known pattern
            from .file_resolver import CommonFilePatterns

            return CommonFilePatterns.find_matching_pattern(file_path) is not None

        # Não deve ser muito longo (provavelmente não é um arquivo)
        if len(file_path) > 100:
            return False

        # Não deve conter espaços (arquivos geralmente não têm espaços em paths)
        if " " in file_path and not (
            file_path.startswith('"') and file_path.endswith('"')
        ):
            return False

        return True

    def _calculate_confidence(
        self, user_input: str, target: str, intent_type: str
    ) -> float:
        """Calcula nível de confiança da intenção detectada (legacy method)"""
        return self._calculate_enhanced_confidence(
            "", None, user_input, target, intent_type
        )

    def _find_key_project_files(self) -> List[str]:
        """Encontra arquivos chave do projeto que devem ser lidos proativamente"""
        key_files = []
        potential_files = [
            "README.md",
            "README.txt",
            "README",
            "requirements.txt",
            "requirements.in",
            "setup.py",
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "main.py",
            "app.py",
            "index.py",
            "config.py",
            "settings.py",
        ]

        for filename in potential_files:
            file_path = self.working_directory / filename
            if file_path.exists() and file_path.is_file():
                key_files.append(filename)

        return key_files[:3]  # Limita aos 3 primeiros encontrados

    def _prioritize_intents(
        self, intents: List[ProactiveIntent]
    ) -> List[ProactiveIntent]:
        """Prioriza e filtra intenções para evitar sobrecarga"""
        if not intents:
            return []

        # Remove duplicatas
        unique_intents = []
        seen = set()
        for intent in intents:
            key = (intent.action, intent.target)
            if key not in seen:
                unique_intents.append(intent)
                seen.add(key)

        # Ordena por prioridade e confiança
        unique_intents.sort(key=lambda x: (x.priority, -x.confidence))

        # Limita número máximo de ações proativas para evitar spam
        max_actions = 5  # Increased for enhanced functionality
        return unique_intents[:max_actions]

    def should_execute_proactively(self, intent: ProactiveIntent) -> bool:
        """Determina se uma intenção deve ser executada proativamente"""
        return intent.autonomous_eligible

    def create_proactive_context(
        self, intent: ProactiveIntent, session_context: Dict
    ) -> ToolContext:
        """Cria contexto para execução proativa de ferramenta"""
        context_data = session_context.copy()

        # Adiciona metadados sobre execução proativa
        context_data["is_proactive"] = True
        context_data["proactive_reason"] = intent.context
        context_data["proactive_confidence"] = intent.confidence
        context_data["autonomous_execution"] = intent.autonomous_eligible

        # Prepara argumentos baseado no tipo de ação
        parsed_args = {}
        if intent.action == ProactiveAction.READ_FILE:
            # Use resolved file path if available
            target_path = intent.target
            if intent.resolved_file and intent.resolved_file.exists:
                target_path = str(intent.resolved_file.path)
            parsed_args = {"file_path": target_path}
        elif intent.action in [
            ProactiveAction.LIST_FILES,
            ProactiveAction.LIST_DIRECTORY,
        ]:
            parsed_args = {
                "directory": (
                    intent.target
                    if intent.target != "."
                    else str(self.working_directory)
                )
            }

        # Why: tools fall back to parsing ``user_input`` with regex when their
        # named args are unset. A synthetic natural-language string here would
        # accidentally trip those fallbacks (e.g. ListFilesTool's `directory\s+`
        # pattern captured "f" from "directory for ."). Pass empty string so
        # the parsed_args we built are the only input source.
        return ToolContext(
            user_input="",
            parsed_args=parsed_args,
            session_data=context_data,
            working_directory=str(self.working_directory),
            metadata={
                "proactive_action": intent.action.value,
                "proactive_target": intent.target,
                "proactive_confidence": intent.confidence,
                "proactive_execution": intent.autonomous_eligible,
                "resolved_file_info": (
                    {
                        "path": (
                            str(intent.resolved_file.path)
                            if intent.resolved_file
                            else None
                        ),
                        "confidence": (
                            intent.resolved_file.confidence
                            if intent.resolved_file
                            else None
                        ),
                        "match_type": (
                            intent.resolved_file.match_type.value
                            if intent.resolved_file
                            else None
                        ),
                    }
                    if intent.resolved_file
                    else None
                ),
            },
        )


def get_proactive_analyzer(working_directory: str = ".") -> ProactiveAnalyzer:
    """Factory function para obter instância do analisador proativo"""
    return ProactiveAnalyzer(working_directory)


def get_enhanced_proactive_analyzer(working_directory: str = ".") -> ProactiveAnalyzer:
    """Get enhanced proactive analyzer instance (alias for compatibility)"""
    return ProactiveAnalyzer(working_directory)
