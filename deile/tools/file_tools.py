"""Ferramentas para manipulação de arquivos"""

from typing import List
from pathlib import Path
import logging
import fnmatch

from .base import SyncTool, ToolContext, ToolResult, ToolStatus
from ..core.exceptions import ValidationError


logger = logging.getLogger(__name__)


class LocalFileAccessViolation(ValidationError):
    """Exceção para violações de acesso a arquivos locais"""
    pass


def _validate_path_within_working_directory(file_path: str, working_directory: str) -> str:
    """Valida se o caminho do arquivo está dentro do working_directory de forma robusta.
    
    Args:
        file_path: Caminho do arquivo a ser validado
        working_directory: Diretório de trabalho base
        
    Returns:
        str: Caminho absoluto validado
        
    Raises:
        LocalFileAccessViolation: Se o caminho é inválido ou inseguro
    """
    try:
        # ROBUSTEZ: Normaliza working_directory
        work_dir = Path(working_directory).resolve()
        file_path_obj = Path(file_path)
        
        # DEBUG: Log para troubleshooting
        logger.debug(f"Validating path - file_path: {file_path}, working_directory: {working_directory}")
        logger.debug(f"Resolved work_dir: {work_dir}")
        
        # FLEXIBILIDADE: Múltiplas estratégias de resolução
        target_paths_to_try = []
        
        # Estratégia 1: Path como fornecido
        if file_path_obj.is_absolute():
            target_paths_to_try.append(file_path_obj.resolve())
        else:
            target_paths_to_try.append((work_dir / file_path).resolve())
        
        # Estratégia 2: Se path original falhar, tenta só o nome do arquivo
        if file_path_obj.is_absolute() or '/' in file_path or '\\' in file_path:
            filename_only = file_path_obj.name
            target_paths_to_try.append((work_dir / filename_only).resolve())
        
        # Estratégia 3: Se path contém diretórios, tenta relativo ao work_dir
        if file_path != "." and not file_path_obj.is_absolute():
            try:
                alt_path = work_dir / file_path_obj
                if alt_path != target_paths_to_try[0]:  # Evita duplicatas
                    target_paths_to_try.append(alt_path.resolve())
            except:
                pass
        
        # Tenta cada estratégia
        for target_path in target_paths_to_try:
            try:
                # Verifica se está dentro do working directory
                relative_path = target_path.relative_to(work_dir)
                logger.debug(f"Path validation successful: {target_path} -> {relative_path}")
                
                # Validações de segurança básicas (só para paths obviamente perigosos)
                path_str = str(relative_path)
                if '..' in path_str and ('..' in file_path or '../' in file_path):
                    logger.warning(f"Path traversal detected in: {file_path}")
                    continue  # Tenta próxima estratégia
                
                # Caracteres perigosos (apenas os mais críticos)
                critical_chars = ['<', '>', '|', '*', '?']
                if any(char in file_path for char in critical_chars):
                    logger.warning(f"Dangerous characters in path: {file_path}")
                    continue  # Tenta próxima estratégia
                
                return str(target_path)
                
            except ValueError as e:
                logger.debug(f"Path outside working directory: {target_path} not in {work_dir}")
                continue  # Tenta próxima estratégia
            except Exception as e:
                logger.debug(f"Path validation error: {e}")
                continue  # Tenta próxima estratégia
        
        # Se todas as estratégias falharam
        raise LocalFileAccessViolation(
            f"Could not resolve secure path for '{file_path}' within working directory '{working_directory}'"
        )
        
    except LocalFileAccessViolation:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in path validation: {e}")
        raise LocalFileAccessViolation(
            f"Path validation failed for '{file_path}': {str(e)}"
        )


class ReadFileTool(SyncTool):
    """Ferramenta para leitura de arquivos"""
    
    @property
    def name(self) -> str:
        return "read_file"
    
    def _read_file_with_encoding_detection(self, file_path: Path) -> str:
        """Lê arquivo com detecção robusta de encoding"""
        import chardet
        
        # Tenta detectar encoding automaticamente
        try:
            with open(file_path, 'rb') as f:
                raw_data = f.read()
                
            # Detecta encoding
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
        except Exception as e:
            logger.error(f"Error in encoding detection for {file_path}: {e}")
            # Fallback final
            return file_path.read_text(encoding='utf-8', errors='replace')
    
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
            # Procura por padrões de arquivo no user_input
            file_patterns = [
                r'(?:file|arquivo)\s+([^\s]+)',
                r'([^\s]+\.(?:txt|py|md|json|js|html|css|xml|csv))',
                r'@([^\s]+)'
            ]
            for pattern in file_patterns:
                match = re.search(pattern, context.user_input, re.IGNORECASE)
                if match:
                    file_path = match.group(1)
                    logger.debug(f"Extracted file_path from user_input: {file_path}")
                    break
        
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
            # Valida segurança do caminho
            validated_path = _validate_path_within_working_directory(
                file_path, context.working_directory
            )
            full_path = Path(validated_path)
            
            # Verifica se arquivo existe
            if not full_path.exists():
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=f"File not found: {file_path}",
                    error=FileNotFoundError(f"File '{file_path}' not found")
                )
            
            # Verifica se é um arquivo
            if not full_path.is_file():
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=f"Path is not a file: {file_path}",
                    error=ValueError(f"'{file_path}' is not a file")
                )
            
            # Lê conteúdo do arquivo com detecção robusta de encoding
            content = self._read_file_with_encoding_detection(full_path)
            
            # Prepara display rico
            lines = content.splitlines()
            line_count = len(lines)
            
            # Cria preview do arquivo (primeiras 10 linhas)
            preview_lines = []
            for i, line in enumerate(lines[:10]):
                line_num = str(i + 1).zfill(3)
                preview_lines.append(f"        {line_num}        {line}")
            
            rich_display = f"● read_file({file_path})\n  ⎿ Read {line_count} lines\n" + "\n".join(preview_lines)
            if line_count > 10:
                rich_display += "\n        ..."
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=content,
                message=f"Successfully read {len(content)} characters from {file_path}",
                metadata={
                    "file_path": str(full_path),
                    "file_size": len(content),
                    "encoding": "utf-8",
                    "rich_display": rich_display
                }
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
        
        # Extração robusta dos argumentos com fallbacks múltiplos
        file_path = None
        content = None
        overwrite = context.parsed_args.get("overwrite", False)
        
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
            # Valida segurança do caminho
            validated_path = _validate_path_within_working_directory(
                file_path, context.working_directory
            )
            full_path = Path(validated_path)
            
            # Verifica se arquivo já existe
            existed_before = full_path.exists()
            if existed_before and not overwrite:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=f"File already exists: {file_path}. Use overwrite=True to replace.",
                    error=FileExistsError(f"File '{file_path}' already exists")
                )
            
            # Cria diretório pai se necessário
            full_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Escreve conteúdo
            full_path.write_text(content, encoding='utf-8')
            
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
            rich_display = f"● write_file({file_path})\n  ⎿ {action} [{status_color}]{file_path}[/{status_color}] with {line_count} lines\n" + "\n".join(preview_lines)
            
            if line_count > 10:
                rich_display += "\n        ..."
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=str(full_path),
                message=f"Successfully wrote {len(content)} characters to {file_path}",
                metadata={
                    "file_path": str(full_path),
                    "content_length": len(content),
                    "overwrite": overwrite,
                    "encoding": "utf-8",
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
                message=f"Error writing file {file_path}: {str(e)}",
                error=e
            )
    
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
                    if extracted_path not in ["files", "file", "directory", "folder"]:
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
                
                if recursive:
                    if pattern:
                        entries = full_path.rglob(pattern)
                    else:
                        entries = full_path.rglob("*")
                else:
                    if pattern:
                        entries = full_path.glob(pattern)
                    else:
                        entries = full_path.iterdir()
                
                for entry in full_path.iterdir():
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
                f"⎿ Estrutura do projeto:"
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