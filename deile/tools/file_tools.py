"""Ferramentas para manipulação de arquivos"""

import fnmatch
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.exceptions import ValidationError
from .base import SyncTool, ToolContext, ToolResult, ToolStatus

logger = logging.getLogger(__name__)


# Extension → cheap, side-effect-free validator the LLM should run after a write.
_POST_WRITE_VALIDATORS: Dict[str, Dict[str, str]] = {
    ".py":  {"kind": "python_syntax",  "template": "python -m py_compile {path}"},
    ".sh":  {"kind": "bash_syntax",    "template": "bash -n {path}"},
    ".json": {"kind": "json_parse",    "template": 'python -c "import json; json.load(open({path!r}))"'},
    ".yaml": {"kind": "yaml_parse",    "template": 'python -c "import yaml; yaml.safe_load(open({path!r}))"'},
    ".yml":  {"kind": "yaml_parse",    "template": 'python -c "import yaml; yaml.safe_load(open({path!r}))"'},
    ".js":  {"kind": "node_syntax",    "template": "node --check {path}"},
    ".mjs": {"kind": "node_syntax",    "template": "node --check {path}"},
    ".ts":  {"kind": "typescript_check", "template": "npx --yes tsc --noEmit {path}"},
    ".tsx": {"kind": "typescript_check", "template": "npx --yes tsc --noEmit --jsx react {path}"},
}


def _post_write_validation_hint(file_path: str) -> Optional[Dict[str, str]]:
    """Return a validation hint for files whose extension is executable / parseable.

    Returns None for extensions we don't have a cheap validator for (text,
    markdown, etc.) — the persona's DoD still applies for those, but there
    is no specific shell command to suggest.
    """
    suffix = Path(file_path).suffix.lower()
    spec = _POST_WRITE_VALIDATORS.get(suffix)
    if spec is None:
        return None
    return {"kind": spec["kind"], "command": spec["template"].format(path=file_path)}


class LocalFileAccessViolation(ValidationError):
    """Exceção para violações de acesso a arquivos locais"""
    pass


@dataclass(frozen=True)
class ResolvedPath:
    """Output of :func:`_resolve_project_path`.

    Attributes
    ----------
    absolute:
        Final resolved absolute path string. Always inside the working
        directory.
    relative_to_cwd:
        Path relative to the working directory (POSIX-style separators), used
        for human-facing display and tool result messages.
    input:
        The exact string the caller passed in, preserved for diagnostic
        messages.
    note:
        ``None`` when the input was already a clean project-relative path.
        A human-readable string describing the normalization when one
        happened (e.g. ``"leading '/' stripped — interpreted as project-relative"``).
        The note flows into ``write_file``'s ``message`` so the LLM sees
        exactly what the system did with its input and can correct course
        on the next turn instead of misremembering where the file landed.
    """

    absolute: str
    relative_to_cwd: str
    input: str
    note: Optional[str]


# Patterns we reject outright, even after normalization.
# `<>|*?` are shell metachars that don't belong in well-formed paths.
# Null byte is a classic path-injection vector in C-extension callers.
_DANGEROUS_PATH_CHARS = re.compile(r"[\x00<>|*?]")

# Windows drive prefix: ``C:\foo``, ``D:/bar``, ``c:\\baz``, etc.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]+")


def _resolve_absolute_or_strip(
    candidate: str, work_dir: Path, raw: str
) -> Tuple[str, Optional[Path], Optional[str]]:
    """Handle a path that starts with '/'. Returns ``(candidate, target, note)``:

    - If the path resolves inside ``work_dir``, keep it as-is — ``target`` is
      the resolved Path, no note.
    - Otherwise the leading '/' is stripped and the caller will resolve the
      remainder against ``work_dir`` — ``target`` is None, note describes
      the normalization.
    """
    try:
        as_is = Path(candidate).resolve()
    except (OSError, RuntimeError):
        as_is = None
    if as_is is not None:
        try:
            as_is.relative_to(work_dir)
            return candidate, as_is, None
        except ValueError:
            pass
    stripped = candidate.lstrip("/")
    if not stripped:
        raise LocalFileAccessViolation(
            f"path is just slashes: {raw!r} — refuse to write to the project "
            "root as a file"
        )
    return stripped, None, (
        "leading '/' stripped — interpreted as project-relative, "
        "NOT as a system-absolute path. The file lives INSIDE the "
        "project working directory."
    )


def _resolve_project_path(file_path: str, working_directory: str) -> ResolvedPath:
    """Resolve an LLM-supplied path to an absolute path inside ``working_directory``.

    Normalizes the patterns LLMs mangle: ``@`` prefix, backslashes, Windows
    drives, ``~``/``~/`` (NOT expanded to ``$HOME`` — treated as project-
    relative), and leading ``/`` (treated as project-relative typo unless
    the absolute path is already inside CWD). ``..`` is allowed when it
    resolves inside CWD; rejected when it escapes.
    """
    if file_path is None:
        raise LocalFileAccessViolation("path is None")

    raw = file_path
    if not isinstance(raw, str):
        raise LocalFileAccessViolation(
            f"path must be str, got {type(raw).__name__}"
        )

    stripped = raw.strip()
    if not stripped:
        raise LocalFileAccessViolation("path is empty")

    if _DANGEROUS_PATH_CHARS.search(stripped):
        raise LocalFileAccessViolation(
            f"path contains forbidden characters (null byte or shell "
            f"metacharacters <>|*?): {raw!r}"
        )

    notes: List[str] = []
    candidate = stripped

    # 2. @-prefix
    if candidate.startswith("@"):
        candidate = candidate[1:]
        notes.append("'@' prefix stripped")

    # 3. Backslash → forward slash
    if "\\" in candidate:
        candidate = candidate.replace("\\", "/")
        notes.append("backslashes converted to forward slashes")

    # 4. Windows drive prefix
    if _WINDOWS_DRIVE_RE.match(candidate):
        candidate = _WINDOWS_DRIVE_RE.sub("", candidate)
        notes.append("Windows drive prefix stripped — path is project-relative")

    # 5. Home shorthand
    if candidate.startswith("~/") or candidate == "~":
        candidate = candidate[2:] if candidate.startswith("~/") else ""
        notes.append(
            "leading '~' stripped — '~' is NOT expanded to system $HOME; "
            "path is project-relative"
        )
        if not candidate:
            candidate = "."

    # 6. Leading slash. Three cases handled by _resolve_absolute_or_strip:
    # (a) absolute and already inside CWD → pass through; (b) absolute and
    # outside → strip the slash and treat as project-relative typo; (c)
    # unresolvable → strip and let containment check catch escapes.
    work_dir = Path(working_directory).resolve()
    target: Optional[Path] = None
    if candidate.startswith("/"):
        candidate, target, slash_note = _resolve_absolute_or_strip(candidate, work_dir, raw)
        if slash_note:
            notes.append(slash_note)

    if not candidate:
        candidate = "."

    if target is None:
        try:
            target = (work_dir / candidate).resolve()
        except (OSError, RuntimeError) as exc:
            raise LocalFileAccessViolation(
                f"could not resolve {raw!r} against {work_dir}: {exc}"
            ) from exc

    # Final containment check. Path.is_relative_to was added in 3.9; we
    # support older runtimes via try/relative_to.
    try:
        target.relative_to(work_dir)
    except ValueError:
        raise LocalFileAccessViolation(
            f"path {raw!r} resolves to {target}, which is OUTSIDE the project "
            f"working directory {work_dir}. Use a project-relative path "
            f"(e.g. drop any leading '..' that escapes the project root)."
        )

    # POSIX-style relative for display (works across platforms in messages)
    rel = target.relative_to(work_dir).as_posix() or "."
    note = "; ".join(notes) if notes else None

    if note is not None:
        logger.debug(
            "path normalized: input=%r resolved=%s note=%s", raw, target, note
        )

    return ResolvedPath(
        absolute=str(target),
        relative_to_cwd=rel,
        input=raw,
        note=note,
    )


def _validate_path_within_working_directory(file_path: str, working_directory: str) -> str:
    """Backward-compatible wrapper returning only the absolute path.

    Existing callers that only need the path string can keep using this. New
    code should call :func:`_resolve_project_path` directly to get access to
    the normalization ``note`` and surface it to the LLM.
    """
    return _resolve_project_path(file_path, working_directory).absolute


class ReadFileTool(SyncTool):
    """Ferramenta para leitura de arquivos"""
    
    @property
    def name(self) -> str:
        return "read_file"
    
    def _read_file_universal(self, file_path: Path) -> str:
        """Lê qualquer tipo de arquivo com detecção robusta de encoding e verificação de tamanho"""
        import chardet

        from ..config.settings import get_settings

        settings = get_settings()

        try:
            # Verifica tamanho do arquivo primeiro
            file_size = file_path.stat().st_size
            max_size = settings.max_file_size_bytes

            if file_size > max_size:
                size_mb = file_size / (1024 * 1024)
                max_mb = max_size / (1024 * 1024)
                raise ValueError(f"Arquivo muito grande ({size_mb:.2f}MB). Limite: {max_mb:.2f}MB")

            # Lê o arquivo como bytes primeiro
            with open(file_path, 'rb') as f:
                raw_data = f.read()

            # Verifica se é arquivo binário analisando os primeiros bytes
            def is_binary_file(data):
                # Verifica presença de bytes nulos nos primeiros 1024 bytes
                sample = data[:1024]
                return b'\x00' in sample

            # Se é arquivo binário, tenta interpretação especial
            if is_binary_file(raw_data):
                return self._handle_binary_file(file_path, raw_data)

            # Para arquivos de texto, detecta encoding automaticamente
            if settings.file_encoding_detection:
                try:
                    detected = chardet.detect(raw_data)
                    encoding = detected.get('encoding', 'utf-8')
                    confidence = detected.get('confidence', 0)

                    logger.debug(f"Detected encoding for {file_path}: {encoding} (confidence: {confidence:.2f})")

                    # Se confiança baixa, tenta encodings comuns
                    if confidence < 0.7:
                        encodings_to_try = ['utf-8', 'utf-16', 'latin-1', 'cp1252', 'iso-8859-1']
                    else:
                        encodings_to_try = [encoding, 'utf-8', 'utf-16', 'latin-1']

                    # Tenta cada encoding
                    for enc in encodings_to_try:
                        try:
                            content = raw_data.decode(enc)
                            # Remove BOM se presente
                            if content.startswith('\ufeff'):
                                content = content[1:]
                            logger.debug(f"Successfully read {file_path} with encoding: {enc}")
                            return content
                        except (UnicodeDecodeError, UnicodeError):
                            continue

                    # Fallback final - força utf-8 com errors='replace'
                    content = raw_data.decode('utf-8', errors='replace')
                    logger.warning(f"Used fallback utf-8 with errors='replace' for {file_path}")
                    return content

                except ImportError:
                    # Se chardet não disponível, usa fallbacks manuais
                    logger.debug("chardet not available, using manual encoding detection")
                    return self._read_file_manual_encoding(file_path)
            else:
                # Se detecção automática desabilitada, força UTF-8
                return raw_data.decode('utf-8', errors='replace')

        except Exception as e:
            logger.error(f"Error in universal file reading for {file_path}: {e}")
            # Fallback final mais seguro
            try:
                return file_path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                return f"[ERRO: Não foi possível ler o arquivo {file_path}: {str(e)}]"

    def _handle_binary_file(self, file_path: Path, raw_data: bytes) -> str:
        """Lida com arquivos binários fornecendo informações úteis"""
        file_extension = file_path.suffix.lower()
        file_size = len(raw_data)

        # Detecta tipo de arquivo baseado na extensão e magic numbers
        magic_signatures = {
            b'\x89PNG\r\n\x1a\n': 'PNG Image',
            b'\xff\xd8\xff': 'JPEG Image',
            b'GIF8': 'GIF Image',
            b'%PDF': 'PDF Document',
            b'PK\x03\x04': 'ZIP Archive (or Office Document)',
            b'\x50\x4b\x05\x06': 'ZIP Archive (empty)',
            b'\x50\x4b\x07\x08': 'ZIP Archive (spanned)',
            b'RIFF': 'RIFF Media File (WAV/AVI)',
            b'\x00\x00\x01\x00': 'ICO Icon',
            b'BM': 'Bitmap Image',
            b'\x1f\x8b': 'GZIP Archive',
            b'7z\xbc\xaf\x27\x1c': '7-Zip Archive'
        }

        # Verifica magic numbers
        file_type = "Binary File"
        for signature, type_name in magic_signatures.items():
            if raw_data.startswith(signature):
                file_type = type_name
                break

        # Formatação especial para tipos conhecidos
        if 'image' in file_type.lower():
            return f"""[ARQUIVO DE IMAGEM]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Extensão: {file_extension}
Caminho: {file_path}

Este é um arquivo de imagem binário. Para visualizar:
- Use um visualizador de imagens
- Converta para base64 se necessário para incorporação
- Primeira linha de bytes: {raw_data[:32].hex()}
"""

        elif 'pdf' in file_type.lower():
            return f"""[DOCUMENTO PDF]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Caminho: {file_path}

Este é um documento PDF. Para extrair texto:
- Use bibliotecas como PyPDF2, pdfplumber ou pdfminer
- Primeira linha de bytes: {raw_data[:64].decode('ascii', errors='replace')}
"""

        elif 'archive' in file_type.lower() or 'zip' in file_type.lower():
            return f"""[ARQUIVO COMPACTADO]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Extensão: {file_extension}
Caminho: {file_path}

Este é um arquivo compactado. Para extrair:
- Use bibliotecas como zipfile, tarfile, ou 7z
- Primeira linha de bytes: {raw_data[:32].hex()}
"""

        else:
            # Tenta ver se há algum texto legível no arquivo
            sample_text = raw_data[:512].decode('utf-8', errors='replace')
            readable_chars = sum(1 for c in sample_text if c.isprintable())

            if readable_chars > len(sample_text) * 0.7:  # Se 70%+ são legíveis
                return f"""[ARQUIVO MISTO/BINÁRIO COM TEXTO]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Extensão: {file_extension}
Caminho: {file_path}

Amostra de texto encontrada:
{sample_text[:200]}...

[Resto do arquivo contém dados binários]
"""
            else:
                return f"""[ARQUIVO BINÁRIO]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Extensão: {file_extension}
Caminho: {file_path}

Este arquivo contém dados binários não-texto.
Primeira linha de bytes (hex): {raw_data[:32].hex()}
Primeira linha de bytes (ascii): {raw_data[:32].decode('ascii', errors='replace')}
"""
    
    def _read_file_manual_encoding(self, file_path: Path) -> str:
        """Detecção manual de encoding sem chardet"""
        encodings = ['utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'latin-1', 'cp1252', 'iso-8859-1']
        
        for encoding in encodings:
            try:
                content = file_path.read_text(encoding=encoding)
                # Remove BOM se presente
                if content.startswith('\ufeff'):
                    content = content[1:]
                logger.debug(f"Successfully read {file_path} with manual encoding: {encoding}")
                return content
            except (UnicodeDecodeError, UnicodeError):
                continue
        
        # Última tentativa com errors='replace'
        logger.warning(f"All encodings failed for {file_path}, using utf-8 with errors='replace'")
        return file_path.read_text(encoding='utf-8', errors='replace')
    
    @property
    def description(self) -> str:
        return "Reads the content of a file"
    
    @property
    def category(self) -> str:
        return "file"
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa leitura de arquivo"""
        # CORREÇÃO ROBUSTA: Debug e múltiplos fallbacks para extração de file_path
        logger.debug(f"ReadFileTool - parsed_args: {context.parsed_args}")
        logger.debug(f"ReadFileTool - file_list: {context.file_list}")
        
        # Obtém caminho do arquivo dos argumentos parseados ou da file_list
        file_path = context.parsed_args.get("file_path") or context.parsed_args.get("path")
        
        # Fallback para file_list se disponível
        if not file_path and context.file_list:
            file_path = context.file_list[0]  # Primeiro arquivo da lista
        
        # Fallback para argumentos posicionais se disponível
        if not file_path:
            # Tenta extrair path de outros argumentos comuns
            for key in ["file", "filename", "filepath"]:
                if context.parsed_args.get(key):
                    file_path = context.parsed_args.get(key)
                    break
        
        # NOVO: Fallback para argumentos sem nome (posicionais)
        if not file_path and context.parsed_args:
            # Se há apenas um argumento, assume que é o file_path
            args_values = list(context.parsed_args.values())
            if len(args_values) == 1 and isinstance(args_values[0], str):
                file_path = args_values[0]
                logger.debug(f"Using positional argument as file_path: {file_path}")
        
        # NOVO: Fallback para user_input se contém referência a arquivo
        if not file_path and context.user_input:
            import re

            # Filler tokens (articles, pronouns) that may appear between a verb
            # and the actual file reference, e.g. "read the readme",
            # "show me the README". Skipping them lets the capture group land
            # on the real file reference.
            _FILLERS = r'(?:(?:the|a|an|me|my|this|that|o|a|os|as|um|uma|este|esta|esse|essa)\s+)*'
            # Procura por padrões de arquivo no user_input
            file_patterns = [
                r'(?:file|arquivo)\s+([^\s]+)',
                r'([^\s]+\.(?:txt|py|md|json|js|html|css|xml|csv|ya?ml|toml|conf|cfg|ini|sh|rst))',
                r'@([^\s]+)',  # Remove @ e usa só o nome do arquivo
                rf'(?:ler|read|abrir|open|examine)\s+{_FILLERS}(?:arquivo\s+)?(?:chamado\s+)?(?:@)?([^\s]+)',
                rf'(?:show|mostrar|exibir)\s+{_FILLERS}(?:arquivo\s+)?(?:@)?([^\s]+)'
            ]
            for pattern in file_patterns:
                match = re.search(pattern, context.user_input, re.IGNORECASE)
                if match:
                    file_path = match.group(1)
                    # Remove @ se presente no início do nome do arquivo
                    if file_path.startswith('@'):
                        file_path = file_path[1:]
                    logger.debug(f"Extracted file_path from user_input: {file_path}")
                    break

            # If the regex extracted a candidate that does not exist as-is,
            # discard it so the smart resolver below can handle case
            # variations (e.g. "readme" → README.md) and partial names.
            if file_path and context.working_directory:
                from pathlib import Path as _Path
                candidate = _Path(context.working_directory) / file_path
                if not candidate.exists() and not _Path(file_path).is_absolute():
                    logger.debug(
                        f"Regex-extracted path '{file_path}' does not exist; "
                        f"deferring to smart file resolution."
                    )
                    file_path = None

        # NOVO: Smart File Resolution - último fallback antes do erro
        if not file_path and context.user_input:
            try:
                # Extrai termos que podem ser referências de arquivo do user_input
                import re

                from ..core.file_resolver import get_file_resolver

                # Filler tokens to skip between verb and file reference
                _Q_FILLERS = r'(?:(?:the|a|an|me|my|this|that|o|a|os|as|um|uma|este|esta|esse|essa)\s+)*'
                # Padrões para extrair referências naturais a arquivos
                query_patterns = [
                    rf'(?:leia?|read|examine?|show|mostrar|abrir|open)\s+{_Q_FILLERS}(?:arquivo\s+)?(?:chamado\s+)?(?:@)?([^\s,\.!?]+)',
                    r'(?:arquivo|file)\s+([^\s,\.!?]+)',
                    r'([a-zA-Z0-9_\-]+(?:\.[a-zA-Z0-9]+)?)\s*(?:file|arquivo)?',
                    r'@([a-zA-Z0-9_\-\.]+)',  # Referencias com @
                ]

                potential_query = None
                for pattern in query_patterns:
                    match = re.search(pattern, context.user_input, re.IGNORECASE)
                    if match:
                        candidate = re.sub(r'[^\w\-\.]', '', match.group(1).strip())
                        # Skip 1-char captures (e.g. "I" from "I want to ..."):
                        # they fuzz-match arbitrary files at high confidence.
                        if len(candidate) > 1:
                            potential_query = candidate
                            break

                if potential_query:
                    logger.debug(f"Attempting smart file resolution for query: '{potential_query}'")

                    # Usa o file resolver para encontrar o arquivo
                    file_resolver = get_file_resolver(Path(context.working_directory))
                    best_match = file_resolver.get_best_match(potential_query, min_confidence=0.6)

                    if best_match:
                        file_path = str(best_match.path)
                        logger.info(f"✅ Smart resolution: '{potential_query}' → '{best_match.path.name}' (confidence: {best_match.confidence:.1%})")
                    else:
                        # Se não encontrou match bom, tenta obter sugestões
                        suggestions = file_resolver.suggest_alternatives(potential_query, max_suggestions=3)
                        if suggestions:
                            suggestion_names = [s.path.name for s in suggestions[:3]]
                            error_msg = (
                                f"Arquivo '{potential_query}' não encontrado. Sugestões: {', '.join(suggestion_names)}. "
                                f"Para usar um destes arquivos, tente: 'leia o arquivo {suggestion_names[0]}'"
                            )
                            return ToolResult(
                                status=ToolStatus.ERROR,
                                message=error_msg,
                                error=ValidationError("file not found with suggestions")
                            )
                        # No match and no suggestions: still surface a clear
                        # "not found" message rather than the generic
                        # "no file path provided" error below.
                        return ToolResult(
                            status=ToolStatus.ERROR,
                            message=f"File '{potential_query}' not found in working directory.",
                            error=FileNotFoundError(f"File '{potential_query}' not found"),
                        )

            except Exception as e:
                logger.debug(f"Smart file resolution failed: {e}")
                # Continua para o erro padrão

        if not file_path:
            error_msg = (
                f"No file path provided. Please specify a file to read. "
                f"Debug info: parsed_args={context.parsed_args}, "
                f"file_list={context.file_list}, user_input='{context.user_input}'"
            )
            return ToolResult(
                status=ToolStatus.ERROR,
                message=error_msg,
                error=ValidationError("file_path is required")
            )
        
        try:
            # Resolve and validate the path. The path resolver normalizes
            # leading '/', '@', '~', backslashes, Windows drives, etc.
            resolved = _resolve_project_path(file_path, context.working_directory)
            full_path = Path(resolved.absolute)

            # Verifica se arquivo existe
            if not full_path.exists():
                hint = (
                    f" (input was {resolved.input!r} → {resolved.note})"
                    if resolved.note
                    else ""
                )
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=(
                        f"File not found: {resolved.relative_to_cwd}"
                        f"{hint}. Use list_files to inspect the project tree before "
                        f"assuming a path."
                    ),
                    error=FileNotFoundError(
                        f"File '{resolved.relative_to_cwd}' not found"
                    ),
                )

            # Verifica se é um arquivo
            if not full_path.is_file():
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=f"Path is not a file: {resolved.relative_to_cwd}",
                    error=ValueError(f"'{resolved.relative_to_cwd}' is not a file"),
                )

            # Verifica configurações de segurança (se habilitadas)
            from ..config.settings import get_settings
            settings = get_settings()

            if settings.enable_file_safety_checks and not settings.allow_all_file_types:
                file_extension = full_path.suffix.lower()
                if file_extension and file_extension not in settings.allowed_file_extensions:
                    return ToolResult(
                        status=ToolStatus.ERROR,
                        message=f"File type not allowed: {file_extension}. Allowed types: {settings.allowed_file_extensions}",
                        error=PermissionError(f"File extension '{file_extension}' not in allowed list")
                    )

            # Lê conteúdo do arquivo com sistema universal
            content = self._read_file_universal(full_path)

            # Prepara display rico
            lines = content.splitlines()
            line_count = len(lines)

            # Cria preview do arquivo (primeiras 10 linhas)
            preview_lines = []
            for i, line in enumerate(lines[:10]):
                line_num = str(i + 1).zfill(3)
                preview_lines.append(f"        {line_num}        {line}")

            rich_display = (
                f"● read_file({resolved.relative_to_cwd})\n"
                f"  ⎿ Read {line_count} lines\n" + "\n".join(preview_lines)
            )
            if line_count > 10:
                rich_display += "\n        ..."

            message_parts = [
                f"Read {len(content)} characters ({line_count} lines) from:",
                f"  file_path: {resolved.absolute}",
                f"  project_relative: {resolved.relative_to_cwd}",
            ]
            if resolved.note:
                message_parts.append(
                    f"  ⚠️  PATH_NORMALIZED: {resolved.note}"
                )

            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=content,
                message="\n".join(message_parts),
                metadata={
                    "function_name": "read_file",
                    "file_path": resolved.absolute,
                    "project_relative_path": resolved.relative_to_cwd,
                    "input_path": resolved.input,
                    "path_normalization_note": resolved.note,
                    "file_size": len(content),
                    "encoding": "utf-8",
                    "rich_display": rich_display,
                },
            )
            
        except LocalFileAccessViolation as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=str(e),
                error=e
            )
        except UnicodeDecodeError as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Error decoding file {file_path}: {str(e)}",
                error=e
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Error reading file {file_path}: {str(e)}",
                error=e
            )
    
    async def can_handle(self, user_input: str) -> bool:
        """Verifica se pode processar a entrada"""
        input_lower = user_input.lower()
        return any(keyword in input_lower for keyword in [
            "read", "show", "display", "view", "content", "file", "@"
        ])


class WriteFileTool(SyncTool):
    """Ferramenta para escrita de arquivos"""
    
    @property
    def name(self) -> str:
        return "write_file"
    
    @property
    def description(self) -> str:
        return "Writes content to a file"
    
    @property
    def category(self) -> str:
        return "file"
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa escrita de arquivo"""
        import re

        # DEBUG: Log dos argumentos recebidos
        logger.debug(f"WriteFileTool - parsed_args: {context.parsed_args}")
        logger.debug(f"WriteFileTool - user_input: {context.user_input}")

        # Extração robusta dos argumentos com fallbacks múltiplos
        file_path = None
        content = None
        # Default permissivo: quando o usuário pede para alterar/criar um
        # arquivo, a tool não pergunta — apenas garante fidelidade total via
        # escrita atômica + read-back (ver _atomic_write_text abaixo).
        overwrite = context.parsed_args.get("overwrite", True)
        
        # 1. Tenta argumentos nomeados primeiro
        if 'file_path' in context.parsed_args:
            file_path = context.parsed_args['file_path']
        elif 'filename' in context.parsed_args:
            file_path = context.parsed_args['filename']
        elif 'path' in context.parsed_args:
            file_path = context.parsed_args['path']
        elif 'file' in context.parsed_args:
            file_path = context.parsed_args['file']
        elif 'filepath' in context.parsed_args:
            file_path = context.parsed_args['filepath']
            
        if 'content' in context.parsed_args:
            content = context.parsed_args['content']
        elif 'text' in context.parsed_args:
            content = context.parsed_args['text']
        elif 'data' in context.parsed_args:
            content = context.parsed_args['data']
        
        # 2. Fallback para argumentos posicionais
        if not file_path and len(context.parsed_args) >= 2:
            args_values = list(context.parsed_args.values())
            file_path = args_values[0]
            content = args_values[1]
        
        # 3. Fallback para parsing do user_input
        if not file_path or content is None:
            user_input = context.user_input.lower()
            
            # Padrões para extrair file_path
            path_patterns = [
                r"write\s+(?:file\s+)?['\"]?([^'\"]+?)['\"]?\s+with",
                r"create\s+(?:file\s+)?['\"]?([^'\"]+?)['\"]?\s+with",
                r"(?:file|arquivo)\s+['\"]?([^'\"]+?)['\"]?",
                r"['\"]([^'\"]+?\.\w+)['\"]"
            ]
            
            for pattern in path_patterns:
                match = re.search(pattern, user_input)
                if match and not file_path:
                    file_path = match.group(1).strip()
                    logger.debug(f"WriteFileTool: Extracted file_path from user_input: {file_path}")
                    break
            
            # Padrões para extrair content
            content_patterns = [
                r"with\s+content\s+['\"]([^'\"]*)['\"]",
                r"with\s+['\"]([^'\"]*)['\"]",
                r"content\s+['\"]([^'\"]*)['\"]"
            ]
            
            for pattern in content_patterns:
                match = re.search(pattern, context.user_input)
                if match and content is None:
                    content = match.group(1)
                    logger.debug(f"WriteFileTool: Extracted content from user_input: {content[:50] if content else 'None'}...")
                    break
        
        # Valida que content foi fornecido
        if content is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                message="No content provided",
                error=ValidationError("content is required")
            )
        
        if not file_path:
            return ToolResult(
                status=ToolStatus.ERROR,
                message="No file path provided. Please specify a file to write.",
                error=ValidationError("file_path is required")
            )
        
        try:
            # Resolve and validate the path. The resolver normalizes common
            # LLM-confusion patterns (leading '/', '@', '~', backslashes,
            # Windows drives) and returns a structured result we surface to
            # the caller so the model can never alucinar where the file went.
            resolved = _resolve_project_path(file_path, context.working_directory)
            full_path = Path(resolved.absolute)

            # Verifica se arquivo já existe
            existed_before = full_path.exists()
            if existed_before and not overwrite:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=(
                        f"File already exists: {resolved.relative_to_cwd}. "
                        f"Use overwrite=True to replace."
                    ),
                    error=FileExistsError(
                        f"File '{resolved.relative_to_cwd}' already exists"
                    ),
                )

            # Cria diretório pai se necessário
            full_path.parent.mkdir(parents=True, exist_ok=True)

            # Escreve atomicamente: garante que o destino só é substituído
            # depois que o conteúdo está completo no disco e que os bytes
            # gravados batem byte-a-byte com o que o caller pediu. Em caso
            # de qualquer falha no caminho, o arquivo original (se existia)
            # permanece íntegro.
            self._atomic_write_text(full_path, content)

            # Prepara display rico
            lines = content.splitlines()
            line_count = len(lines)

            # Preview do arquivo criado (primeiras 10 linhas)
            preview_lines = []
            for i, line in enumerate(lines[:10]):
                line_num = str(i + 1)
                preview_lines.append(f"        {line_num}     {line}")

            status_color = "green" if not existed_before else "yellow"
            action = "Created" if not existed_before else "Updated"
            rich_display = (
                f"● write_file({resolved.relative_to_cwd})\n"
                f"  ⎿ {action} [{status_color}]{resolved.relative_to_cwd}[/{status_color}] "
                f"with {line_count} lines\n" + "\n".join(preview_lines)
            )
            if line_count > 10:
                rich_display += "\n        ..."

            message_parts = [
                f"Wrote {len(content)} characters ({line_count} lines) to:",
                f"  file_path: {resolved.absolute}",
                f"  project_relative: {resolved.relative_to_cwd}",
                f"  input_given: {resolved.input}",
            ]
            if resolved.note:
                message_parts.append(
                    f"  ⚠️  PATH_NORMALIZED: {resolved.note} "
                    f"The file is at the resolved_path above — use THAT path "
                    f"in subsequent tool calls (read_file, bash_execute, …), "
                    f"NOT the input you originally sent."
                )

            metadata: Dict[str, Any] = {
                "function_name": "write_file",
                "file_path": resolved.absolute,
                "project_relative_path": resolved.relative_to_cwd,
                "input_path": resolved.input,
                "path_normalization_note": resolved.note,
                "content_length": len(content),
                "overwrite": overwrite,
                "encoding": "utf-8",
                "rich_display": rich_display,
            }

            validation_hint = _post_write_validation_hint(resolved.relative_to_cwd)
            if validation_hint is not None:
                metadata["post_write_validation_required"] = True
                metadata["post_write_validation_command"] = validation_hint["command"]
                metadata["post_write_validation_kind"] = validation_hint["kind"]
                message_parts.append(
                    f"\n⚠️  POST_WRITE_VALIDATION_REQUIRED: per the Definition of Done, "
                    f"your next action MUST validate this file. Suggested command:\n"
                    f"    {validation_hint['command']}\n"
                    f"Do NOT declare the task complete until validation succeeds "
                    f"(exit 0) or you have explicitly diagnosed and reported a "
                    f"failure to the user."
                )

            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=resolved.absolute,
                message="\n".join(message_parts),
                metadata=metadata,
            )

        except LocalFileAccessViolation as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=str(e),
                error=e
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Error writing file {file_path}: {str(e)}",
                error=e
            )
    
    @staticmethod
    def _atomic_write_text(target: Path, content: str) -> None:
        """Escreve ``content`` em ``target`` de forma atômica e fiel.

        Garantias:

        * **Atomicidade**: o conteúdo é gravado primeiro num arquivo temporário
          no mesmo diretório (mesmo filesystem) e só então renomeado para o
          destino via ``os.replace``. Se algo falhar no meio, o arquivo
          original permanece intacto e o temporário é removido.

        * **Fidelidade**: o ``content`` é codificado como UTF-8 sem BOM e sem
          tradução de newlines, e os bytes do arquivo final são re-lidos e
          comparados byte-a-byte com o que foi pedido. Qualquer divergência
          aborta a operação com :class:`IOError` antes do arquivo final ser
          publicado.

        * **Durabilidade**: ``fsync`` é chamado no temporário antes do rename,
          então um crash de SO pós-rename não deixa o arquivo zero-byte.
        """
        import os
        import tempfile

        payload = content.encode("utf-8")
        target_dir = target.parent
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target_dir), prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            # Read-back validation antes do replace: o temporário é descartável
            # e o original ainda existe se falharmos aqui.
            with open(tmp_path, "rb") as fh:
                written = fh.read()
            if written != payload:
                raise IOError(
                    f"Atomic write integrity check failed for {target}: "
                    f"requested {len(payload)} bytes, found {len(written)} on disk"
                )
            os.replace(str(tmp_path), str(target))
        except BaseException:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise

    async def can_handle(self, user_input: str) -> bool:
        """Verifica se pode processar a entrada"""
        input_lower = user_input.lower()
        return any(keyword in input_lower for keyword in [
            "write", "create", "save", "generate", "update", "modify"
        ])


class ListFilesTool(SyncTool):
    """Ferramenta para listar arquivos"""
    
    @property
    def name(self) -> str:
        return "list_files"
    
    @property
    def description(self) -> str:
        return "Lists files and directories in a given path"
    
    @property
    def category(self) -> str:
        return "file"
    
    def _load_gitignore_patterns(self, working_directory: Path) -> List[str]:
        """Carrega padrões do .gitignore"""
        gitignore_path = working_directory / ".gitignore"
        patterns = []
        
        if gitignore_path.exists():
            try:
                content = gitignore_path.read_text(encoding='utf-8')
                for line in content.splitlines():
                    line = line.strip()
                    # Ignora linhas vazias e comentários
                    if line and not line.startswith('#'):
                        patterns.append(line)
            except Exception:
                # Se não conseguir ler o .gitignore, continua sem padrões
                pass
        
        return patterns
    
    def _should_ignore(self, file_path: Path, patterns: List[str], working_directory: Path) -> bool:
        """Verifica se um arquivo deve ser ignorado baseado nos padrões do .gitignore"""
        if not patterns:
            return False
        
        try:
            # Caminho relativo ao diretório de trabalho
            relative_path = file_path.relative_to(working_directory)
            path_str = str(relative_path).replace('\\', '/')
            
            # Verifica cada padrão
            for pattern in patterns:
                # Remove / no final para diretórios
                clean_pattern = pattern.rstrip('/')
                
                # Verifica match direto
                if fnmatch.fnmatch(path_str, clean_pattern):
                    return True
                
                # Verifica match com padrão de diretório
                if fnmatch.fnmatch(path_str, clean_pattern + '/*'):
                    return True
                
                # Verifica se está dentro de um diretório ignorado
                parts = path_str.split('/')
                for i in range(len(parts)):
                    partial_path = '/'.join(parts[:i+1])
                    if fnmatch.fnmatch(partial_path, clean_pattern):
                        return True
            
            return False
        except ValueError:
            # Se não conseguir calcular caminho relativo, não ignora
            return False
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa listagem de arquivos"""
        import re

        # Extração robusta dos argumentos com fallbacks múltiplos
        target_path = "."
        recursive = False
        show_hidden = False
        pattern = None
        
        # 1. Tenta argumentos nomeados primeiro
        if 'path' in context.parsed_args:
            target_path = context.parsed_args['path']
        elif 'directory' in context.parsed_args:
            target_path = context.parsed_args['directory']
        elif 'folder' in context.parsed_args:
            target_path = context.parsed_args['folder']
        elif 'dir' in context.parsed_args:
            target_path = context.parsed_args['dir']
            
        recursive = context.parsed_args.get("recursive", False)
        show_hidden = context.parsed_args.get("show_hidden", False)
        pattern = context.parsed_args.get("pattern")
        
        # 2. Fallback para argumentos posicionais
        if target_path == "." and len(context.parsed_args) >= 1:
            args_values = list(context.parsed_args.values())
            potential_path = args_values[0]
            if isinstance(potential_path, str) and potential_path != "True" and potential_path != "False":
                target_path = potential_path
        
        # 3. Fallback para parsing do user_input
        if target_path == ".":
            user_input = context.user_input.lower()
            
            # Padrões para extrair path
            path_patterns = [
                r"list\s+files?\s+in\s+['\"]?([^'\"]+?)['\"]?",
                r"list\s+['\"]?([^'\"]+?)['\"]?",
                r"files?\s+in\s+['\"]?([^'\"]+?)['\"]?",
                r"directory\s+['\"]?([^'\"]+?)['\"]?",
                r"folder\s+['\"]?([^'\"]+?)['\"]?"
            ]
            
            for pattern_regex in path_patterns:
                match = re.search(pattern_regex, user_input)
                if match:
                    extracted_path = match.group(1).strip()
                    # Reject regex-capture artifacts: lazy `[^'"]+?` can match a
                    # single letter from a downstream word (e.g. "f" from "for").
                    # Real paths are either explicit (./, /, ~) or have length>1.
                    is_explicit_root = extracted_path in {".", "/", "~"}
                    is_too_short = len(extracted_path) < 2 and not is_explicit_root
                    is_filler_word = extracted_path.lower() in {"files", "file", "directory", "folder"}
                    if not (is_too_short or is_filler_word):
                        target_path = extracted_path
                        logger.debug(f"ListFilesTool: Extracted path from user_input: {target_path}")
                        break
        
        # ROBUSTEZ: Múltiplas tentativas de validação de path
        validation_attempts = [
            target_path,
            ".",  # Fallback para diretório atual
            context.working_directory,  # Fallback para working_directory explícito
        ]
        
        full_path = None
        working_dir = Path(context.working_directory).resolve()
        
        logger.debug(f"ListFilesTool - target_path: {target_path}, working_directory: {context.working_directory}")
        
        for attempt_path in validation_attempts:
            try:
                logger.debug(f"ListFilesTool - trying validation for: {attempt_path}")
                validated_path = _validate_path_within_working_directory(
                    attempt_path, context.working_directory
                )
                full_path = Path(validated_path)
                logger.debug(f"ListFilesTool - validation successful: {full_path}")
                break
            except LocalFileAccessViolation as e:
                logger.debug(f"ListFilesToool - validation failed for {attempt_path}: {e}")
                continue
            except Exception as e:
                logger.debug(f"ListFilesTool - unexpected error for {attempt_path}: {e}")
                continue
        
        if full_path is None:
            # ÚLTIMO RECURSO: Usa working_directory diretamente (sem validação)
            logger.warning("All path validations failed, using working_directory directly")
            full_path = working_dir
        
        try:
            
            if not full_path.exists():
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=f"Path not found: {target_path}",
                    error=FileNotFoundError(f"Path '{target_path}' not found")
                )
            
            files_info = []
            
            if full_path.is_file():
                # Se é um arquivo específico, não aplica filtros do .gitignore
                stat = full_path.stat()
                files_info.append({
                    "name": full_path.name,
                    "type": "file",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "path": str(full_path.relative_to(context.working_directory))
                })
            else:
                # Se é um diretório, carrega padrões do .gitignore
                gitignore_patterns = self._load_gitignore_patterns(working_dir)
                
                # ``recursive`` and ``pattern`` come from the LLM as either
                # native bool/str or stringified ("True"/"False"). Coerce both
                # so the rglob/glob branch is reachable.
                recursive_flag = recursive
                if isinstance(recursive_flag, str):
                    recursive_flag = recursive_flag.strip().lower() in {"true", "1", "yes"}

                if recursive_flag:
                    if pattern:
                        entries = full_path.rglob(pattern)
                    else:
                        entries = full_path.rglob("*")
                else:
                    if pattern:
                        entries = full_path.glob(pattern)
                    else:
                        entries = full_path.iterdir()

                for entry in entries:
                    # Pula arquivos ocultos se não solicitado
                    if not show_hidden and entry.name.startswith('.'):
                        continue
                    
                    # Verifica se deve ser ignorado pelo .gitignore
                    if self._should_ignore(entry, gitignore_patterns, working_dir):
                        continue
                    
                    try:
                        stat = entry.stat()
                        files_info.append({
                            "name": entry.name,
                            "type": "directory" if entry.is_dir() else "file",
                            "size": stat.st_size if entry.is_file() else None,
                            "modified": stat.st_mtime,
                            "path": str(entry.relative_to(working_dir))
                        })
                    except (PermissionError, OSError):
                        # Pula arquivos sem permissão
                        continue
            
            # Ordena por nome
            files_info.sort(key=lambda x: x["name"].lower())
            
            # Prepara display rico com quebras de linha FORÇADAS
            rich_display_lines = [
                f"● list_files({target_path})",
                "⎿ Estrutura do projeto:"
            ]
            
            # Cria tree structure visual
            if files_info:
                # Agrupa por diretórios e arquivos
                dirs = [f for f in files_info if f["type"] == "directory"]
                files = [f for f in files_info if f["type"] == "file"]
                
                rich_display_lines.append(f"   {target_path}/")
                
                # Mostra diretórios primeiro (máximo 8)
                for i, dir_info in enumerate(dirs[:8]):
                    is_last_dir = i == len(dirs[:8]) - 1 and not files
                    prefix = "└── " if is_last_dir else "├── "
                    rich_display_lines.append(f"   {prefix}📁 {dir_info['name']}/")
                
                # Mostra arquivos (máximo 15) 
                for i, file_info in enumerate(files[:15]):
                    is_last_file = i == len(files[:15]) - 1
                    prefix = "└── " if is_last_file else "├── "
                    rich_display_lines.append(f"   {prefix}📄 {file_info['name']}")
                
                # Indica se há mais arquivos
                total_remaining = len(files_info) - len(dirs[:8]) - len(files[:15])
                if total_remaining > 0:
                    rich_display_lines.append(f"   └── ... e mais {total_remaining} itens")
            else:
                rich_display_lines.append("   (pasta vazia)")
            
            # FORÇA quebras de linha duplas para garantir formatação
            rich_display = "\n".join(rich_display_lines) + "\n"
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=files_info,
                message=f"Found {len(files_info)} items in {target_path}",
                metadata={
                    "target_path": str(full_path),
                    "recursive": recursive,
                    "show_hidden": show_hidden,
                    "pattern": pattern,
                    "total_items": len(files_info),
                    "rich_display": rich_display
                }
            )
            
        except LocalFileAccessViolation as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=str(e),
                error=e
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Error listing files in {target_path}: {str(e)}",
                error=e
            )
    
    async def can_handle(self, user_input: str) -> bool:
        """Verifica se pode processar a entrada"""
        input_lower = user_input.lower()
        return any(keyword in input_lower for keyword in [
            "list", "show", "files", "directory", "folder", "ls", "dir"
        ])


class DeleteFileTool(SyncTool):
    """Ferramenta para deletar arquivos (com precauções)"""
    
    @property
    def name(self) -> str:
        return "delete_file"
    
    @property
    def description(self) -> str:
        return "Deletes a file or directory (use with caution)"
    
    @property
    def category(self) -> str:
        return "file"
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa deleção de arquivo"""
        file_path = context.parsed_args.get("file_path") or context.parsed_args.get("path")
        force = context.parsed_args.get("force", False)
        
        # Fallback para argumentos alternativos
        if not file_path:
            for key in ["file", "filename", "filepath"]:
                if context.parsed_args.get(key):
                    file_path = context.parsed_args.get(key)
                    break
        
        if not file_path:
            return ToolResult(
                status=ToolStatus.ERROR,
                message="No file path provided. Please specify a file to delete.",
                error=ValidationError("file_path is required")
            )
        
        # Medidas de segurança
        if not force:
            # Verifica se não é um arquivo crítico
            dangerous_patterns = ['.env', 'config', '.git', '__pycache__']
            if any(pattern in file_path.lower() for pattern in dangerous_patterns):
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=f"Refusing to delete potentially important file: {file_path}. Use force=True if needed.",
                    error=PermissionError("Safety check failed")
                )
        
        try:
            # Valida segurança do caminho
            validated_path = _validate_path_within_working_directory(
                file_path, context.working_directory
            )
            full_path = Path(validated_path)
            
            if not full_path.exists():
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=f"File not found: {file_path}",
                    error=FileNotFoundError(f"File '{file_path}' not found")
                )
            
            # Registra informações antes de deletar
            was_directory = full_path.is_dir()
            size = full_path.stat().st_size if full_path.is_file() else 0
            
            # Deleta
            if was_directory:
                # Remove diretório recursivamente
                import shutil
                shutil.rmtree(full_path)
            else:
                full_path.unlink()
            
            # Prepara display rico
            item_type = "directory" if was_directory else "file"
            rich_display = f"● delete_file({file_path})\n  ⎿ Deleted [red]{file_path}[/red] ({item_type})"
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                message=f"Successfully deleted {'directory' if was_directory else 'file'}: {file_path}",
                metadata={
                    "deleted_path": str(full_path),
                    "was_directory": was_directory,
                    "size": size,
                    "force": force,
                    "rich_display": rich_display
                }
            )
            
        except LocalFileAccessViolation as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=str(e),
                error=e
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Error deleting {file_path}: {str(e)}",
                error=e
            )
    
    async def can_handle(self, user_input: str) -> bool:
        """Verifica se pode processar a entrada"""
        input_lower = user_input.lower()
        return any(keyword in input_lower for keyword in [
            "delete", "remove", "rm", "del"
        ])