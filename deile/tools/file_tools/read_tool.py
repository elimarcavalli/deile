"""ReadFileTool — leitura de arquivos com detecção de encoding e path-extraction."""

import logging
import re
from pathlib import Path

from ...core.exceptions import ValidationError
from .._file_listing import _collect_entries, _render_tree
from .._path_resolution import (_PATH_ARG_KEYS_FALLBACK, _PATH_ARG_KEYS_PRIMARY,
                               LocalFileAccessViolation, ResolvedPath,
                               _extract_path_arg, _looks_like_outside_project,
                               _not_found_message, _resolve_project_path)
from ..base import SyncTool, ToolContext, ToolResult, ToolStatus

logger = logging.getLogger(__name__)

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

        from ...config.settings import get_settings

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
                            if content.startswith('﻿'):
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
                if content.startswith('﻿'):
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

        # Obtém caminho do arquivo dos argumentos parseados ou da file_list.
        # ReadFileTool's historical precedence is two-stage: PRIMARY keys
        # (file_path, path) beat file_list, but file_list beats
        # the FALLBACK synonyms (file, filename, filepath). Splitting
        # the lookup into two calls preserves that exact order.
        file_path = _extract_path_arg(context.parsed_args, keys=_PATH_ARG_KEYS_PRIMARY)

        # Fallback para file_list se disponível
        if not file_path and context.file_list:
            file_path = context.file_list[0]  # Primeiro arquivo da lista

        # Synonym fallback runs AFTER file_list — preserving the original
        # behavior where an explicit file_list argument beats the LLM's
        # near-miss synonyms.
        if not file_path:
            file_path = _extract_path_arg(context.parsed_args, keys=_PATH_ARG_KEYS_FALLBACK)

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
            # variations (e.g. "readme" -> README.md) and partial names.
            if file_path and context.working_directory:
                candidate = Path(context.working_directory) / file_path
                if not candidate.exists() and not Path(file_path).is_absolute():
                    logger.debug(
                        f"Regex-extracted path '{file_path}' does not exist; "
                        f"deferring to smart file resolution."
                    )
                    file_path = None

        # NOVO: Smart File Resolution - último fallback antes do erro
        if not file_path and context.user_input:
            try:
                from ...core.file_resolver import get_file_resolver

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
                return ToolResult.error_result(
                    message=_not_found_message(
                        resolved,
                        file_path,
                        detail="Use list_files to inspect the project tree "
                               "before assuming a path.",
                        include_bash_hint=True,
                        bash_verb="cat",
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
            from ...config.settings import get_settings
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
