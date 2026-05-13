"""Ferramentas para manipulação de arquivos"""

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.exceptions import ValidationError
# `LocalFileAccessViolation`, `_post_write_validation_hint`, and
# `_validate_path_within_working_directory` are re-exported for existing
# test imports (`from deile.tools.file_tools import ...`); they have no
# direct caller in this module — see `__all__` below.
from ._path_resolution import (LocalFileAccessViolation, ResolvedPath,
                               _apply_post_write_hint,
                               _looks_like_outside_project,
                               _post_write_validation_hint,
                               _resolve_project_path,
                               _validate_path_within_working_directory)
from .base import SyncTool, ToolContext, ToolResult, ToolStatus

logger = logging.getLogger(__name__)

__all__ = [
    # Tool classes.
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "ListFilesTool",
    "DeleteFileTool",
    # Re-exports from `_path_resolution` (kept stable for tests/callers).
    "LocalFileAccessViolation",
    "ResolvedPath",
    "_looks_like_outside_project",
    "_post_write_validation_hint",
    "_resolve_project_path",
    "_validate_path_within_working_directory",
]


# Filler tokens (articles, pronouns) that may appear between a verb and the
# file reference, e.g. "read the readme". Used by ReadFileTool path-extraction
# regexes; shared so the vocabulary stays in one place.
_PATH_EXTRACTION_FILLERS = (
    r'(?:(?:the|a|an|me|my|this|that|o|a|os|as|um|uma|este|esta|esse|essa)\s+)*'
)


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
            # Procura por padrões de arquivo no user_input
            file_patterns = [
                r'(?:file|arquivo)\s+([^\s]+)',
                r'([^\s]+\.(?:txt|py|md|json|js|html|css|xml|csv|ya?ml|toml|conf|cfg|ini|sh|rst))',
                r'@([^\s]+)',  # Remove @ e usa só o nome do arquivo
                rf'(?:ler|read|abrir|open|examine)\s+{_PATH_EXTRACTION_FILLERS}(?:arquivo\s+)?(?:chamado\s+)?(?:@)?([^\s]+)',
                rf'(?:show|mostrar|exibir)\s+{_PATH_EXTRACTION_FILLERS}(?:arquivo\s+)?(?:@)?([^\s]+)'
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
                from ..core.file_resolver import get_file_resolver

                # Padrões para extrair referências naturais a arquivos
                query_patterns = [
                    rf'(?:leia?|read|examine?|show|mostrar|abrir|open)\s+{_PATH_EXTRACTION_FILLERS}(?:arquivo\s+)?(?:chamado\s+)?(?:@)?([^\s,\.!?]+)',
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
                            return ToolResult.error_result(
                                message=error_msg,
                                error=ValidationError("file not found with suggestions")
                            )
                        # No match and no suggestions: still surface a clear
                        # "not found" message rather than the generic
                        # "no file path provided" error below.
                        return ToolResult.error_result(
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
            return ToolResult.error_result(
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
                norm_hint = (
                    f" (input was {resolved.input!r} → {resolved.note})"
                    if resolved.note
                    else ""
                )
                bash_hint = (
                    " If the file lives OUTSIDE the project, use "
                    f"bash_execute(command=\"cat {resolved.absolute}\") instead — "
                    "bash_execute has no working-directory sandbox."
                    if (resolved.note or _looks_like_outside_project(file_path))
                    else ""
                )
                return ToolResult.error_result(
                    message=(
                        f"File not found: {resolved.relative_to_cwd}"
                        f"{norm_hint}. Use list_files to inspect the project tree "
                        f"before assuming a path.{bash_hint}"
                    ),
                    error=FileNotFoundError(
                        f"File '{resolved.relative_to_cwd}' not found"
                    ),
                )

            # Verifica se é um arquivo
            if not full_path.is_file():
                return ToolResult.error_result(
                    message=f"Path is not a file: {resolved.relative_to_cwd}",
                    error=ValueError(f"'{resolved.relative_to_cwd}' is not a file"),
                )

            # Verifica configurações de segurança (se habilitadas)
            from ..config.settings import get_settings
            settings = get_settings()

            if settings.enable_file_safety_checks and not settings.allow_all_file_types:
                file_extension = full_path.suffix.lower()
                if file_extension and file_extension not in settings.allowed_file_extensions:
                    return ToolResult.error_result(
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
            return ToolResult.error_result(
                message=str(e),
                error=e
            )
        except UnicodeDecodeError as e:
            return ToolResult.error_result(
                message=f"Error decoding file {file_path}: {str(e)}",
                error=e
            )
        except Exception as e:
            return ToolResult.error_result(
                message=f"Error reading file {file_path}: {str(e)}",
                error=e
            )
    
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
            return ToolResult.error_result(
                message="No content provided",
                error=ValidationError("content is required")
            )
        
        if not file_path:
            return ToolResult.error_result(
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
                return ToolResult.error_result(
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

            _apply_post_write_hint(resolved.relative_to_cwd, metadata, message_parts)

            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=resolved.absolute,
                message="\n".join(message_parts),
                metadata=metadata,
            )

        except LocalFileAccessViolation as e:
            return ToolResult.error_result(
                message=str(e),
                error=e
            )
        except Exception as e:
            return ToolResult.error_result(
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

class EditFileTool(SyncTool):
    """Edita arquivos existentes via lista ordenada de patches find/replace.

    Cada patch substitui ocorrência(s) de ``find`` por ``replace``. Patches são
    aplicados sequencialmente em memória; cada patch vê o resultado dos
    anteriores. No final, escreve atomicamente — ou todos os patches passam,
    ou nenhum byte do arquivo muda.

    **Garantias (não negociáveis)**:

    * **Atômico**: falha em qualquer patch aborta a operação inteira; o arquivo
      original permanece intacto. Múltiplas chamadas concorrentes nunca
      observam estado intermediário porque a publicação final usa ``os.replace``.
    * **Determinístico**: ordem dos patches importa. Patch ``i`` busca em
      *post-(i-1)-buffer*.
    * **Não-ambíguo por default**: ``find`` deve aparecer EXATAMENTE 1 vez no
      buffer corrente. 0 ocorrências → erro "find não encontrado". ≥2
      ocorrências → erro "find ambíguo, refine o contexto ou use replace_all".
    * **Replace-all opt-in**: ``replace_all=true`` aceita ≥1 ocorrência e
      substitui todas.
    * **Fidelidade byte-a-byte**: usa o mesmo ``_atomic_write_text`` do
      ``WriteFileTool`` — tempfile + ``fsync`` + read-back + ``os.replace``.

    **Quando usar `edit_file` em vez de `write_file`**:

    * Alterar trechos específicos de um arquivo existente (1 a N edits).
    * Múltiplas alterações no mesmo arquivo numa única chamada (transação).

    **Quando usar `write_file` em vez de `edit_file`**:

    * Criar arquivo novo (``edit_file`` exige que o arquivo já exista).
    * Reescrever totalmente (≳70% das linhas mudam).
    * Arquivo binário.

    O LLM deve escolher: ``edit_file`` para alterações pontuais é mais barato
    em tokens e mais seguro (não há risco de "esquecer" partes do arquivo).
    """

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Surgically edits an existing file via an ordered list of find/replace "
            "patches. Use for targeted modifications; use write_file for new files "
            "or full rewrites."
        )

    @property
    def category(self) -> str:
        return "file"

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Aplica patches ao arquivo, atomicamente."""
        logger.debug(f"EditFileTool - parsed_args keys: {list(context.parsed_args.keys())}")

        # 1. Extrai file_path (fallbacks consistentes com WriteFileTool)
        file_path = (
            context.parsed_args.get("file_path")
            or context.parsed_args.get("path")
            or context.parsed_args.get("filename")
            or context.parsed_args.get("file")
            or context.parsed_args.get("filepath")
        )
        if not file_path:
            return ToolResult.error_result(
                message=(
                    "No file path provided. edit_file requires `file_path` "
                    "(string) and `patches` (array of {find, replace})."
                ),
                error=ValidationError("file_path is required"),
            )

        # 2. Extrai e normaliza patches.
        # Aceita também o formato single-edit "old_string"/"new_string" para
        # ergonomia: o LLM pode mandar uma edição simples sem montar array.
        patches_raw = context.parsed_args.get("patches")
        if patches_raw is None:
            old = context.parsed_args.get("old_string")
            new = context.parsed_args.get("new_string")
            if old is not None and new is not None:
                patches_raw = [{
                    "find": old,
                    "replace": new,
                    "replace_all": bool(context.parsed_args.get("replace_all", False)),
                }]

        if not isinstance(patches_raw, list) or not patches_raw:
            return ToolResult.error_result(
                message=(
                    "`patches` must be a non-empty array of objects with "
                    "fields {find, replace, replace_all?}. Got: "
                    f"{type(patches_raw).__name__}"
                ),
                error=ValidationError("patches must be a non-empty list"),
            )

        # 3. Valida estrutura de cada patch UPFRONT (antes de tocar disco).
        normalized: List[Dict[str, Any]] = []
        for idx, raw in enumerate(patches_raw, start=1):
            if not isinstance(raw, dict):
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} is not an object: got {type(raw).__name__}. "
                        f"Each patch must be {{find: str, replace: str, replace_all?: bool}}."
                    ),
                    error=ValidationError(f"patch #{idx} must be an object"),
                )
            find = raw.get("find")
            replace = raw.get("replace")
            replace_all = bool(raw.get("replace_all", False))
            if not isinstance(find, str) or not isinstance(replace, str):
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} must have `find` (str) and `replace` (str). "
                        f"Got find={type(find).__name__}, replace={type(replace).__name__}."
                    ),
                    error=ValidationError(f"patch #{idx} requires string find/replace"),
                )
            if find == "":
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} has empty `find`. Empty strings match "
                        "everywhere — refusing. Use write_file to create or "
                        "fully rewrite a file."
                    ),
                    error=ValidationError(f"patch #{idx}: empty find"),
                )
            normalized.append({
                "find": find,
                "replace": replace,
                "replace_all": replace_all,
            })

        # 4. Resolve path e lê conteúdo atual.
        try:
            resolved = _resolve_project_path(file_path, context.working_directory)
        except LocalFileAccessViolation as exc:
            return ToolResult.error_result(message=str(exc), error=exc)

        full_path = Path(resolved.absolute)
        if not full_path.exists():
            norm_hint = (
                f" (input was {resolved.input!r} → {resolved.note})"
                if resolved.note
                else ""
            )
            return ToolResult.error_result(
                message=(
                    f"File not found: {resolved.relative_to_cwd}{norm_hint}. "
                    f"edit_file only modifies existing files — use write_file "
                    f"to create new ones."
                ),
                error=FileNotFoundError(
                    f"File '{resolved.relative_to_cwd}' not found"
                ),
            )
        if not full_path.is_file():
            return ToolResult.error_result(
                message=f"Path is not a file: {resolved.relative_to_cwd}",
                error=ValueError(f"'{resolved.relative_to_cwd}' is not a file"),
            )

        try:
            original_bytes = full_path.read_bytes()
        except OSError as exc:
            return ToolResult.error_result(
                message=f"Could not read file {resolved.relative_to_cwd}: {exc}",
                error=exc,
            )

        # UTF-8 só é exigido se o arquivo for textual válido. Decodificamos com
        # 'strict' para que arquivos binários falhem cedo e sejam roteados
        # para write_file/bash_execute.
        try:
            buffer = original_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return ToolResult.error_result(
                message=(
                    f"File {resolved.relative_to_cwd} is not valid UTF-8 "
                    f"(byte {exc.start}: {exc.reason}). edit_file works only "
                    f"on UTF-8 text files. For binary or other encodings, "
                    f"use write_file."
                ),
                error=exc,
            )

        # 5. Aplica patches em memória. Cada patch vê o resultado dos anteriores.
        applied_log: List[Dict[str, Any]] = []
        for idx, patch in enumerate(normalized, start=1):
            find = patch["find"]
            replace = patch["replace"]
            replace_all = patch["replace_all"]
            occurrences = buffer.count(find)

            if occurrences == 0:
                # Diagnóstico rico: mostra trecho próximo de uma substring
                # parecida quando for plausível (linha que contém o início do find).
                hint = self._suggest_near_match(buffer, find)
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} failed: `find` not present in current "
                        f"buffer (0 matches). Hint: read the file again and "
                        f"copy the exact substring (including whitespace and "
                        f"newlines).{hint} No change was written; original "
                        f"file is intact."
                    ),
                    error=ValidationError(
                        f"patch #{idx}: 'find' not found in buffer"
                    ),
                    metadata={
                        "function_name": "edit_file",
                        "file_path": resolved.absolute,
                        "failed_patch_index": idx,
                        "occurrences": 0,
                    },
                )
            if occurrences > 1 and not replace_all:
                preview = find[:80] + ("…" if len(find) > 80 else "")
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} failed: `find` is ambiguous — "
                        f"{occurrences} occurrences in the current buffer "
                        f"(found {preview!r}). Add more surrounding context "
                        f"to make `find` unique, or set `replace_all: true` "
                        f"to replace all occurrences. No change was written; "
                        f"original file is intact."
                    ),
                    error=ValidationError(
                        f"patch #{idx}: 'find' is ambiguous ({occurrences} matches)"
                    ),
                    metadata={
                        "function_name": "edit_file",
                        "file_path": resolved.absolute,
                        "failed_patch_index": idx,
                        "occurrences": occurrences,
                    },
                )

            if replace_all:
                replaced_count = occurrences
                buffer = buffer.replace(find, replace)
            else:
                # Exatamente 1 ocorrência confirmada acima — single replace.
                replaced_count = 1
                buffer = buffer.replace(find, replace, 1)

            applied_log.append({
                "index": idx,
                "occurrences": occurrences,
                "replaced": replaced_count,
                "find_preview": find[:60] + ("…" if len(find) > 60 else ""),
            })

        # 6. No-op? Se nada mudou (ex.: find == replace), não reescreve disco.
        new_bytes = buffer.encode("utf-8")
        if new_bytes == original_bytes:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=resolved.absolute,
                message=(
                    f"No bytes changed after applying {len(normalized)} patch(es) "
                    f"to {resolved.relative_to_cwd}. File left untouched. "
                    f"(This usually means find == replace; check the patches.)"
                ),
                metadata={
                    "function_name": "edit_file",
                    "file_path": resolved.absolute,
                    "project_relative_path": resolved.relative_to_cwd,
                    "patches_applied": applied_log,
                    "no_op": True,
                },
            )

        # 7. Escreve atomicamente. Reutiliza o helper já testado em isolação.
        try:
            WriteFileTool._atomic_write_text(full_path, buffer)
        except Exception as exc:
            return ToolResult.error_result(
                message=(
                    f"Atomic write failed for {resolved.relative_to_cwd}: {exc}. "
                    f"Original file is intact (atomic write guarantees no "
                    f"partial publish)."
                ),
                error=exc,
            )

        # 8. Display rico + validation hint pós-write quando aplicável.
        line_count = buffer.count("\n") + (0 if buffer.endswith("\n") else 1 if buffer else 0)
        rich_lines = [f"● edit_file({resolved.relative_to_cwd})"]
        rich_lines.append(
            f"  ⎿ Applied {len(applied_log)} patch(es), now {len(new_bytes)} bytes / {line_count} lines"
        )
        for entry in applied_log:
            rich_lines.append(
                f"     • patch #{entry['index']}: replaced {entry['replaced']}× → "
                f"{entry['find_preview']!r}"
            )
        rich_display = "\n".join(rich_lines)

        message_parts = [
            f"Applied {len(applied_log)} patch(es) to:",
            f"  file_path: {resolved.absolute}",
            f"  project_relative: {resolved.relative_to_cwd}",
            f"  bytes_before: {len(original_bytes)} → bytes_after: {len(new_bytes)}",
        ]
        if resolved.note:
            message_parts.append(f"  ⚠️  PATH_NORMALIZED: {resolved.note}")

        metadata: Dict[str, Any] = {
            "function_name": "edit_file",
            "file_path": resolved.absolute,
            "project_relative_path": resolved.relative_to_cwd,
            "input_path": resolved.input,
            "path_normalization_note": resolved.note,
            "patches_applied": applied_log,
            "bytes_before": len(original_bytes),
            "bytes_after": len(new_bytes),
            "encoding": "utf-8",
            "rich_display": rich_display,
        }

        _apply_post_write_hint(resolved.relative_to_cwd, metadata, message_parts)

        return ToolResult(
            status=ToolStatus.SUCCESS,
            data=resolved.absolute,
            message="\n".join(message_parts),
            metadata=metadata,
        )

    @staticmethod
    def _suggest_near_match(buffer: str, find: str) -> str:
        """Sugestão de diagnóstico quando ``find`` não bate.

        Heurística barata: pega a primeira linha não-vazia de ``find`` e
        verifica se aparece (após strip) no buffer. Se aparecer, sugere que
        o LLM provavelmente errou whitespace/newline. Retorna string vazia
        quando nada útil pode ser dito — não fabrica diagnóstico.
        """
        if not find:
            return ""
        first_line = next((ln for ln in find.splitlines() if ln.strip()), "")
        if not first_line:
            return ""
        stripped = first_line.strip()
        if len(stripped) < 6:
            return ""
        if stripped in buffer:
            return (
                f" The first line of `find` ({stripped!r}) DOES appear in the "
                f"buffer — the mismatch is likely whitespace, indentation, "
                f"or trailing newlines. Re-read the file and copy exactly."
            )
        return ""

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
        
        # Resolve the LLM-supplied target_path through the canonical resolver
        # so we capture the normalization ``note`` (e.g. "leading '/' stripped"
        # or "@ prefix stripped") AND surface sandbox violations cleanly.
        #
        # IMPORTANT: when the LLM EXPLICITLY supplies a path that violates the
        # sandbox (``../parent_repo/.github`` or system-absolute outside CWD),
        # we MUST surface that violation — never silently fall back to CWD,
        # which would list unrelated content and mislead the model into a
        # tighter loop. Fallbacks are only useful when the LLM omitted the
        # argument entirely (target_path stayed at default "." which always
        # resolves cleanly).
        full_path = None
        resolved_obj: Optional[ResolvedPath] = None
        working_dir = Path(context.working_directory).resolve()

        logger.debug(f"ListFilesTool - target_path: {target_path}, working_directory: {context.working_directory}")

        try:
            resolved_obj = _resolve_project_path(
                target_path, context.working_directory
            )
            full_path = Path(resolved_obj.absolute)
            logger.debug(f"ListFilesTool - validation successful: {full_path}")
        except LocalFileAccessViolation as e:
            logger.debug(f"ListFilesTool - sandbox violation for {target_path!r}: {e}")
            return ToolResult.error_result(
                message=str(e),
                error=e,
            )
        except Exception as e:
            logger.debug(f"ListFilesTool - unexpected resolver error for {target_path!r}: {e}")
            # Fall through to the working_directory fallback below — this
            # branch should be unreachable in normal operation, but keeping
            # the safety net avoids a hard 500 on resolver bugs.
            full_path = working_dir

        try:

            if not full_path.exists():
                # Surface the normalization note (e.g. "leading '/' stripped")
                # AND a bash-execute hint when the user clearly asked for a
                # system-absolute or parent-relative path. Without this, the
                # LLM gets only "Path not found: <garbage>" and loops on the
                # same broken call (observed in the second-run trace).
                hint = ""
                if resolved_obj is not None and resolved_obj.note:
                    hint += (
                        f" (input was {resolved_obj.input!r} → "
                        f"{resolved_obj.note})"
                    )
                if _looks_like_outside_project(target_path):
                    hint += (
                        ". For paths OUTSIDE the project working directory "
                        "(parent repo, sibling project, /etc/, ~/...), use "
                        "`bash_execute` (e.g. `ls <abs_path>` or "
                        "`cat <abs_path>`) — bash_execute has no "
                        "working-directory sandbox."
                    )
                return ToolResult.error_result(
                    message=f"Path not found: {target_path}{hint}",
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
            return ToolResult.error_result(
                message=str(e),
                error=e
            )
        except Exception as e:
            return ToolResult.error_result(
                message=f"Error listing files in {target_path}: {str(e)}",
                error=e
            )
    
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
            return ToolResult.error_result(
                message="No file path provided. Please specify a file to delete.",
                error=ValidationError("file_path is required")
            )
        
        # Medidas de segurança
        if not force:
            # Verifica se não é um arquivo crítico
            dangerous_patterns = ['.env', 'config', '.git', '__pycache__']
            if any(pattern in file_path.lower() for pattern in dangerous_patterns):
                return ToolResult.error_result(
                    message=f"Refusing to delete potentially important file: {file_path}. Use force=True if needed.",
                    error=PermissionError("Safety check failed")
                )
        
        try:
            resolved = _resolve_project_path(file_path, context.working_directory)
            full_path = Path(resolved.absolute)

            if not full_path.exists():
                norm_hint = (
                    f" (input was {resolved.input!r} → {resolved.note})"
                    if resolved.note
                    else ""
                )
                bash_hint = (
                    " If the file lives OUTSIDE the project, use "
                    f"bash_execute(command=\"rm {resolved.absolute}\") instead — "
                    "bash_execute has no working-directory sandbox."
                    if (resolved.note or _looks_like_outside_project(file_path))
                    else ""
                )
                return ToolResult.error_result(
                    message=(
                        f"File not found: {resolved.relative_to_cwd}"
                        f"{norm_hint}.{bash_hint}"
                    ),
                    error=FileNotFoundError(f"File '{resolved.relative_to_cwd}' not found"),
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
            return ToolResult.error_result(
                message=str(e),
                error=e
            )
        except Exception as e:
            return ToolResult.error_result(
                message=f"Error deleting {file_path}: {str(e)}",
                error=e
            )
    