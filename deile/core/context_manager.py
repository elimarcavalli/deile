"""Context Manager para gerenciamento de contexto e RAG - DEILE 2.0 ULTRA"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..memory.memory_manager import MemoryManager
from ..orchestration.subagents.constants import is_display_only_entry
from ..parsers.base import ParseResult
from ..personas.instruction_loader import InstructionLoader
from ..personas.manager import PersonaManager
from ..skills.bootstrap import bootstrap_skills
from ..skills.router import SkillRouter, SkillSelectionContext
from ..storage.embeddings import EmbeddingStore
from ..tools.base import ToolResult
from .deile_md_loader import DEILEMDLoader  # Issue #62 — leitura hierárquica DEILE.md

logger = logging.getLogger(__name__)


async def _build_preferences_block(session: Any) -> str:
    """Issue #341: Render user preferences as a Markdown block for injection.

    Returns an empty string when there are no preferences (zero overhead)
    or when the PreferenceStore is unavailable / raises.
    """
    try:
        user_id = _resolve_user_id(session)
        if not user_id:
            return ""
        from deile.preferences.store import PreferenceStore

        store = PreferenceStore()
        prefs = store.get_all(user_id)
        if not prefs:
            return ""
        lines = ["## 📋 Preferências do Usuário"]
        for key, value in sorted(prefs.items()):
            lines.append(f"- `{key}`: {value}")
        return "\n".join(lines)
    except Exception:
        return ""


def _resolve_user_id(session: Any) -> Optional[str]:
    """Resolve the user_id from session, falling back to OS identity.

    Precedence:
    1. ``session.user_id`` (AgentSession — set by CLI/bot adapters)
    2. ``os.getuid()`` (POSIX only; returns None on Windows)
    3. ``os.environ.get("USER", "unknown")``
    """
    if session is not None:
        uid = getattr(session, "user_id", None)
        if uid:
            return str(uid)
    try:
        return str(os.getuid())
    except AttributeError:
        pass
    return os.environ.get("USER", "unknown")


def _merge_bot_extra(base: str, session: Any) -> str:
    """Append session.context_data['extra_system_prompt'] (bot mode) to base."""
    if session is None:
        return base
    ctx = getattr(session, "context_data", {})
    if not isinstance(ctx, dict):
        return base
    extra = ctx.get("extra_system_prompt")
    if not extra:
        return base
    try:
        from deile.core.bot_hooks import merge_extra_system_prompt

        return merge_extra_system_prompt(base, str(extra))
    except Exception:
        return base


async def _prepend_deile_md_layers(
    base_instruction: str, working_directory: Optional[str] = None
) -> str:
    """Issue #62: Prepend hierarchical DEILE.md layers (Core → User → CWD).

    As camadas DEILE.md são injetadas ANTES da instrução da persona,
    com demarcação clara de origem e prioridade. Core primeiro (não
    negociável), depois Usuário, depois CWD.

    Leitura de disco roda em thread auxiliar para honrar o princípio
    async-first do projeto (cf. `03-PRINCIPIOS-ARQUITETURAIS.md` §1).
    """
    try:
        wd = Path(working_directory) if working_directory else Path.cwd()
        loader = DEILEMDLoader(working_directory=wd)
        deile_md_block = await asyncio.to_thread(loader.build_merged_prompt)
        if deile_md_block:
            return deile_md_block + "\n\n" + base_instruction
        return base_instruction
    except Exception as exc:
        logger.warning(
            "Falha ao carregar camadas DEILE.md: %s — usando instrução base", exc
        )
        return base_instruction


@dataclass
class ContextChunk:
    """Chunk de contexto com metadata"""

    content: str
    source: str  # 'file', 'conversation', 'tool_result', etc.
    source_path: Optional[str] = None
    chunk_id: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    relevance_score: float = 0.0

    def __post_init__(self):
        if not self.chunk_id:
            self.chunk_id = f"{self.source}_{hash(self.content)}_{int(self.timestamp)}"


@dataclass
class ContextWindow:
    """Janela de contexto com limites de tokens"""

    chunks: List[ContextChunk] = field(default_factory=list)
    max_tokens: int = 8000  # Default para modelos médios
    current_tokens: int = 0

    def add_chunk(self, chunk: ContextChunk, estimated_tokens: int) -> bool:
        """Adiciona chunk se couber na janela"""
        if self.current_tokens + estimated_tokens <= self.max_tokens:
            self.chunks.append(chunk)
            self.current_tokens += estimated_tokens
            return True
        return False

    def remove_oldest(self) -> Optional[ContextChunk]:
        """Remove o chunk mais antigo"""
        if self.chunks:
            removed = self.chunks.pop(0)
            # Recalcula tokens (aproximação)
            self.current_tokens = sum(len(c.content) // 4 for c in self.chunks)
            return removed
        return None

    def clear(self) -> None:
        """Limpa a janela"""
        self.chunks.clear()
        self.current_tokens = 0


class ContextManager:
    # Class-level defaults so ``ContextManager.__new__(ContextManager)``
    # (the test pattern that bypasses ``__init__``) still has these attrs
    # available — the skills bootstrap reads them on every turn and would
    # otherwise AttributeError + fall into the build_context exception path.
    _skills_bootstrapped: bool = False
    _skill_router: Optional[SkillRouter] = None

    """Context Manager enterprise-grade para DEILE 2.0 ULTRA

    Integra novo sistema de personas e memory architecture híbrida:
    - PersonaManager para system instructions dinâmicas
    - MemoryManager para contexto inteligente
    - Event-driven context building
    - Hot-reload de configurações
    - DEILE.md hierarchical layers (Issue #62)
    """

    def __init__(
        self,
        embedding_store: Optional[EmbeddingStore] = None,
        max_context_tokens: int = 8000,
        persona_manager: Optional[PersonaManager] = None,
        memory_manager: Optional[MemoryManager] = None,
    ):
        # Core components
        self.embedding_store = embedding_store
        self.max_context_tokens = max_context_tokens

        # DEILE 2.0 ULTRA - Novos componentes
        self.persona_manager = persona_manager
        self.memory_manager = memory_manager

        # CORREÇÃO BG003: Instruction Loader para carregar de arquivos MD
        self.instruction_loader = InstructionLoader()

        # Skills router — lazily bootstrapped on first use so the cost is only
        # paid when context is actually built. ``None`` once bootstrap has run
        # means the subsystem is disabled or empty.
        self._skill_router: Optional[SkillRouter] = None
        self._skills_bootstrapped: bool = False

        # Estatísticas
        self._context_builds = 0
        self._persona_switches = 0
        self._memory_retrievals = 0

    async def build_context(
        self,
        user_input: str,
        parse_result: Optional[ParseResult] = None,
        tool_results: Optional[List[ToolResult]] = None,
        session: Optional[Any] = None,  # AgentSession
        **kwargs,
    ) -> Dict[str, Any]:
        """Constrói contexto para a próxima invocação do provider.

        Inclui o histórico completo da sessão em `messages` para providers
        non-Gemini (OpenAI / DeepSeek / Anthropic), que não mantêm chat
        session interna. Gemini usa create_chat_session e ignora `messages`.
        """
        self._context_builds += 1
        start_time = time.time()

        try:
            # Prepara system instruction
            system_instruction = await self._build_system_instruction(
                parse_result, session, **kwargs
            )

            # Reconstrói messages a partir do histórico da sessão. O agente já
            # adicionou a entrada "user" do turno corrente em add_to_history()
            # antes de chamar build_context, então conversation_history sempre
            # termina com a mensagem atual do usuário.
            #
            # Issue #257: entradas marcadas como "display-only" (resumo do painel
            # de sub-DEILEs paralelos) são FILTRADAS aqui — elas existem apenas
            # para que ``replay_history`` possa re-renderizar o painel no
            # ``/resume``, mas mandar para o provider quebra alternância
            # user/assistant (Anthropic 400, OpenAI percepção corrompida).
            messages: List[Dict[str, Any]] = []
            if session is not None and getattr(session, "conversation_history", None):
                for entry in session.conversation_history:
                    entry_meta = entry.get("metadata") or {}
                    if is_display_only_entry(entry_meta):
                        continue
                    role = entry.get("role", "user")
                    content = entry.get("content", "")
                    msg_dict: Dict[str, Any] = {"role": role, "content": content}
                    if entry_meta:
                        msg_dict["metadata"] = entry_meta
                    messages.append(msg_dict)
            if not messages:
                messages = [{"role": "user", "content": user_input}]

            context = {
                "messages": messages,
                "system_instruction": system_instruction,
                "metadata": {
                    "build_time": time.time() - start_time,
                    "user_input_length": len(user_input),
                    "tool_results_count": len(tool_results) if tool_results else 0,
                    "history_length": len(messages),
                    "chat_session_mode": True,
                },
            }

            # CORREÇÃO CRÍTICA: Inclui file_data se há arquivos uploadados no ParseResult
            if (
                parse_result
                and parse_result.metadata
                and "uploaded_files" in parse_result.metadata
            ):
                uploaded_files = parse_result.metadata["uploaded_files"]
                file_data_parts = []

                for file_info in uploaded_files:
                    if "file_data" in file_info:
                        file_data_parts.append(file_info["file_data"])

                if file_data_parts:
                    context["file_data_parts"] = file_data_parts
                    context["metadata"]["uploaded_files_count"] = len(file_data_parts)
                    logger.info(
                        f"Added {len(file_data_parts)} file_data_parts to context"
                    )

            # Inclui file_data na ÚLTIMA mensagem (turno atual) — Gemini-style parts
            if "file_data_parts" in context:
                user_message = context["messages"][-1]
                user_parts = [{"text": user_input}]

                for file_data in context["file_data_parts"]:
                    user_parts.append(file_data)

                user_message["parts"] = user_parts
                if "content" in user_message:
                    del user_message["content"]

            logger.debug("Built context: %d message(s) in history", len(messages))
            return context

        except Exception as e:
            logger.error(f"Error building context: {e}")
            return {
                "messages": [{"role": "user", "content": user_input}],
                "system_instruction": "You are DEILE, a helpful AI assistant.",
                "error": str(e),
            }

    def clear_cache(self) -> None:
        """Limpa todos os caches (mantido para compatibilidade)"""
        logger.debug("Cache clearing requested (simplified context manager)")

    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas simplificadas do context manager"""
        return {
            "context_builds": self._context_builds,
            "max_context_tokens": self.max_context_tokens,
            "chat_session_mode": True,
            "simplified": True,
        }

    async def _build_system_instruction(
        self, parse_result: Optional[ParseResult], session: Optional[Any], **kwargs
    ) -> str:
        """Constrói instrução do sistema usando PersonaManager ou fallback hardcoded.

        Issue #62: As camadas hierárquicas DEILE.md (Core → Usuário → CWD) são
        prefixadas à instrução da persona, com demarcação clara de origem e
        prioridade. As regras do Core são absolutamente não-negociáveis.

        Se a sessão tem `context_data["extra_system_prompt"]`, é apendado ao
        final da instrução base como bloco `<bot_capabilities>`.
        """

        working_directory = kwargs.get("working_directory", os.getcwd())

        # CORREÇÃO CRÍTICA: Usa PersonaManager se disponível
        if self.persona_manager:
            try:
                active_persona = self.persona_manager.get_active_persona()
                if active_persona:
                    logger.debug(
                        f"Using persona '{active_persona.name}' system instruction"
                    )

                    # Constrói instrução usando o método da persona (que carrega do MD)
                    context = {
                        "session": session,
                        "working_directory": working_directory,
                    }
                    base_instruction = await active_persona.build_system_instruction(
                        context
                    )

                    # Issue #62: Prefixa camadas DEILE.md (Core → User → CWD)
                    base_instruction = await _prepend_deile_md_layers(
                        base_instruction, working_directory
                    )

                    # Issue #341: Inject user preferences (after persona, before skills)
                    prefs_block = await _build_preferences_block(session)
                    if prefs_block:
                        base_instruction += f"\n\n{prefs_block}"

                    # Skills layer: aditiva, depois da persona e das regras DEILE.md.
                    skills_block = await self._build_skills_block(
                        parse_result, session, working_directory=working_directory
                    )
                    if skills_block:
                        base_instruction += f"\n\n{skills_block}"

                    # Adiciona contexto de arquivos
                    file_context = await self._build_file_context(session, **kwargs)
                    if file_context:
                        base_instruction += (
                            f"\n\n📁 [ARQUIVOS DISPONÍVEIS NO PROJETO]\n{file_context}"
                        )

                    return _merge_bot_extra(base_instruction, session)

            except Exception as e:
                logger.error(
                    f"Error using PersonaManager: {e}, falling back to hardcoded"
                )

        # Fallback para instrução de arquivo MD
        logger.debug(
            "Using fallback system instruction from MD file (PersonaManager not available)"
        )
        return await self._build_fallback_system_instruction(
            parse_result, session, **kwargs
        )

    async def _build_fallback_system_instruction(
        self,
        parse_result: Optional[ParseResult] = None,
        session: Optional[Any] = None,
        **kwargs,
    ) -> str:
        """CORREÇÃO BG003: Carrega instrução de arquivo MD (não mais hardcoded!)

        Issue #62: Também prefixa as camadas DEILE.md no fallback.
        """

        logger.debug("Loading system instruction from MD file (fallback)")

        working_directory = kwargs.get("working_directory", os.getcwd())

        # Carrega instrução de arquivo MD
        base_instruction = self.instruction_loader.load_fallback_instruction()

        # Issue #62: Prefixa camadas DEILE.md (Core → User → CWD)
        base_instruction = await _prepend_deile_md_layers(
            base_instruction, working_directory
        )

        # Issue #341: Inject user preferences (after DEILE.md layers, before skills)
        prefs_block = await _build_preferences_block(session)
        if prefs_block:
            base_instruction += f"\n\n{prefs_block}"

        # Skills layer também no fallback (mesma ordem do caminho com persona).
        skills_block = await self._build_skills_block(
            parse_result, session, working_directory=working_directory
        )
        if skills_block:
            base_instruction += f"\n\n{skills_block}"

        # Adiciona contexto de arquivos se disponível
        file_context = await self._build_file_context(session, **kwargs)
        if file_context:
            base_instruction += (
                f"\n\n📁 [ARQUIVOS DISPONÍVEIS NO PROJETO]\n{file_context}"
            )

        return _merge_bot_extra(base_instruction, session)

    async def _build_skills_block(
        self,
        parse_result: Optional[ParseResult],
        session: Optional[Any],
        working_directory: Optional[str] = None,
    ) -> str:
        """Resolve skills for this turn and render them as an appendable block.

        Returns an empty string when the subsystem is disabled, no skills are
        loaded, or no triggers fired. Bootstrap is lazy — the registry is
        populated on the first call and reused afterwards.

        The bootstrap is anchored on the session's ``working_directory`` (or
        the caller-supplied ``working_directory`` kwarg) so the
        ``SkillRouter``'s ``project_root`` matches the agent's actual project
        — critical for the path-traversal containment in
        ``file_content_patterns`` triggers, which would otherwise fall back
        to the process CWD and accept arbitrary references when the user
        launched DEILE from a different directory.
        """
        if not self._skills_bootstrapped:
            self._skills_bootstrapped = True
            try:
                project_dir: Optional[Path] = None
                session_wd = (
                    getattr(session, "working_directory", None) if session else None
                )
                if session_wd:
                    project_dir = Path(session_wd)
                elif working_directory:
                    project_dir = Path(working_directory)
                self._skill_router = await bootstrap_skills(project_dir=project_dir)
            except Exception as exc:
                logger.warning(
                    "skills: bootstrap failed (%s); subsystem disabled for session", exc
                )
                self._skill_router = None

        if self._skill_router is None:
            return ""

        # Pull the latest user input from the session for code-block detection.
        user_input = ""
        if session is not None and getattr(session, "conversation_history", None):
            for entry in reversed(session.conversation_history):
                if entry.get("role") == "user":
                    content = entry.get("content", "")
                    if isinstance(content, str):
                        user_input = content
                    break

        file_refs = (
            tuple(parse_result.file_references)
            if parse_result and parse_result.file_references
            else ()
        )

        context = SkillSelectionContext(
            user_input=user_input, file_references=file_refs
        )
        try:
            selected = self._skill_router.select_skills(context)
        except Exception as exc:
            logger.warning(
                "skills: selection raised %s; skipping injection this turn", exc
            )
            selected = []

        # Stash the active skill names on the session so the streaming layer
        # in ``DeileAgent`` can emit a STAGE event ("Skill ativa: <names>")
        # before the LLM call — that's the only place where the user actually
        # sees feedback that a skill is being used (auto-injection is
        # otherwise invisible). Best-effort: a session without
        # ``context_data`` is fine, we just skip the feedback.
        if session is not None:
            try:
                session.context_data["_active_skills"] = [s.name for s in selected]
            except Exception:
                pass

        # Always include the catalog (compact name+description list) so the LLM
        # knows what's available and can pull a non-triggered skill via the
        # ``invoke_skill`` tool. Auto-triggered skills are excluded from the
        # catalog to avoid duplicating their full bodies right above.
        excluded = {s.name for s in selected}
        catalog = self._skill_router.render_catalog(exclude_names=excluded)
        active_block = self._skill_router.render_block(selected) if selected else ""

        if selected:
            logger.info(
                "skills: injecting %d active skill(s): %s",
                len(selected),
                ", ".join(s.name for s in selected),
            )

        parts = [p for p in (active_block, catalog) if p]
        return "\n\n".join(parts)

    # Maximum characters for the file-context block injected into the system prompt.
    # Each LLM token is roughly 4 chars; keeping this at 8 000 chars ≈ 2 000 tokens —
    # enough to list a few hundred top-level paths without blowing the context window.
    _FILE_CONTEXT_MAX_CHARS: int = 8_000

    # Extensions that are never useful as references in a chat context.
    _IGNORE_EXTENSIONS: frozenset = frozenset(
        {
            ".pyc",
            ".pyo",
            ".pyd",  # compiled Python
            ".o",
            ".so",
            ".a",
            ".dylib",  # compiled C/C++
            ".class",
            ".jar",  # Java bytecode
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".bmp",
            ".ico",
            ".svg",
            ".webp",
            ".mp4",
            ".mp3",
            ".wav",
            ".avi",  # media
            ".zip",
            ".tar",
            ".gz",
            ".bz2",
            ".xz",
            ".rar",  # archives
            ".db",
            ".sqlite",
            ".sqlite3",  # databases
            ".bin",
            ".exe",
            ".dll",  # binaries
            ".lock",  # lock files (large, unreadable)
        }
    )

    async def _build_file_context(self, session: Optional[Any], **kwargs) -> str:
        """Constrói lista compacta de arquivos do projeto para o system prompt.

        Limitações aplicadas para evitar overflow de contexto:
        - Apenas arquivos não-binários e não-compilados.
        - Diretórios irrelevantes são ignorados (pruning real via dirs[:]).
        - Saída truncada a ``_FILE_CONTEXT_MAX_CHARS`` caracteres com aviso de log.
        """
        try:
            working_directory = None
            if session and hasattr(session, "working_directory"):
                working_directory = session.working_directory
            elif "working_directory" in kwargs:
                working_directory = kwargs["working_directory"]

            if not working_directory:
                return ""

            work_dir = Path(working_directory)
            if not work_dir.exists():
                return ""

            # Directories that are fully pruned from the walk (no descent, no listing).
            ignore_dirs = {
                ".git",
                ".github",
                ".hg",
                ".svn",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".venv",
                "venv",
                ".env",
                "env",
                "node_modules",
                "logs",
                ".claude",
                "cache",
                ".cache",
                "deilebot",
                "deile_bot",  # separate repo (canonical + transitional names)
                "work_items",  # large planning docs, not project code
                "test-your-might",  # sandbox output dir
                "dist",
                "build",
                "site-packages",
                ".worktrees",
            }

            import os

            file_list: List[str] = []
            root_str = str(work_dir)

            # os.walk with dirs[:] pruning — never descends into ignored dirs.
            for root, dirs, files in os.walk(work_dir):
                # Prune in-place so os.walk does NOT recurse into ignored dirs.
                dirs[:] = [
                    d for d in dirs if d not in ignore_dirs and not d.startswith(".")
                ]

                for file in files:
                    # Skip hidden files and binary/compiled extensions.
                    if file.startswith("."):
                        continue
                    ext = Path(file).suffix.lower()
                    if ext in self._IGNORE_EXTENSIONS:
                        continue

                    rel_path = os.path.relpath(os.path.join(root, file), root_str)
                    file_list.append(rel_path)

            if not file_list:
                return ""

            file_list.sort()

            # Build output and enforce hard character limit (sliding window: keep
            # the first N entries that fit so the most top-level paths are preserved).
            header = "Arquivos do projeto (use read_file para ler qualquer um):"
            # Reserve room for the worst-case footer (e.g. 6-digit omission count).
            _FOOTER_RESERVE = 80
            lines: List[str] = [header]
            char_budget = (
                self._FILE_CONTEXT_MAX_CHARS
                - len(header)
                - 1  # header newline
                - _FOOTER_RESERVE
            )
            truncated = False
            included = 0
            for entry in file_list:
                line = f"  {entry}"
                needed = len(line) + 1  # +1 for newline
                if char_budget - needed < 0:
                    truncated = True
                    break
                lines.append(line)
                char_budget -= needed
                included += 1

            if truncated:
                omitted = len(file_list) - included
                lines.append(
                    f"  ... ({omitted} arquivo(s) omitido(s) para respeitar limite de contexto)"
                )
                logger.warning(
                    "_build_file_context: truncated file list at %d/%d entries "
                    "to stay within %d chars. Add ignore patterns or reduce project size.",
                    included,
                    len(file_list),
                    self._FILE_CONTEXT_MAX_CHARS,
                )

            return "\n".join(lines)

        except Exception as exc:
            logger.debug("Error building file context: %s", exc)
            return ""
