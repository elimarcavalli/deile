"""Parser inteligente para detectar arquivos mencionados (com ou sem @)"""

import re
import asyncio
import logging
from typing import List, Optional, Set, Tuple
from pathlib import Path

from .base import Parser, ParseResult, ParseStatus, ParsedCommand
from .file_parser import FileParser  
from ..core.exceptions import ParserError
from ..infrastructure.google_file_api import GoogleFileUploader, get_file_uploader

logger = logging.getLogger(__name__)


class IntelligentFileParser(Parser):
    """Parser inteligente que detecta arquivos mencionados com e sem @
    
    Funcionalidades:
    1. Detecta @arquivo.txt (padrão original)
    2. Detecta arquivos mencionados no texto (requirements.txt, config.json, etc.)
    3. Cross-verifica com arquivos reais no working directory
    4. Auto-upload via Google File API
    """
    
    def __init__(self, file_uploader: Optional[GoogleFileUploader] = None):
        super().__init__()
        self.file_uploader = file_uploader or get_file_uploader()
        self.base_file_parser = FileParser(file_uploader)
        
        # Extensões comuns para detecção automática
        self.common_extensions = {
            '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.scss', 
            '.json', '.yaml', '.yml', '.xml', '.md', '.txt', '.cfg', '.ini',
            '.conf', '.config', '.env', '.log', '.sql', '.sh', '.bat', '.ps1',
            '.dockerfile', '.gitignore', '.gitconfig', '.editorconfig',
            '.requirements', '.lock', '.toml', '.properties'
        }
        
        # Padrões para detectar nomes de arquivos sem @
        self.file_patterns = [
            r'\b([a-zA-Z0-9_\-]+\.(?:py|js|ts|jsx|tsx|html|css|scss|json|yaml|yml|xml|md|txt|cfg|ini|conf|config|env|log|sql|sh|bat|ps1|dockerfile|gitignore|gitconfig|editorconfig|requirements|lock|toml|properties))\b',
            r'\b(requirements\.txt|package\.json|Dockerfile|Makefile|README\.md|setup\.py|__init__\.py|main\.py|index\.html|style\.css|app\.js|config\.yaml|docker-compose\.yml)\b',
            r'\b([A-Z][a-zA-Z0-9_]*\.py)\b',  # PascalCase Python files
            r'\b([a-z][a-zA-Z0-9_]*\.[a-z]{2,4})\b',  # camelCase files with extensions
        ]
        
        self.compiled_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in self.file_patterns]
        
        # Cache para evitar múltiplas verificações
        self._file_exists_cache = {}
        self._working_directory_cache = None
    
    @property
    def name(self) -> str:
        return "intelligent_file_parser"
    
    @property 
    def description(self) -> str:
        return "Intelligently detects file references with or without @ syntax"
    
    @property
    def priority(self) -> int:
        return 90  # Ligeiramente menor que FileParser original para não interferir
    
    def can_parse(self, input_text: str) -> bool:
        """Verifica se há referências de arquivo na entrada"""
        # Delega para FileParser se tem @
        if '@' in input_text and self.base_file_parser.can_parse(input_text):
            return True
        
        # Verifica se há padrões de arquivo sem @
        return any(pattern.search(input_text) for pattern in self.compiled_patterns)
    
    async def parse_async(self, input_text: str, working_directory: Optional[str] = None) -> ParseResult:
        """Parse inteligente com detecção automática de arquivos"""
        self._parse_count += 1
        
        try:
            # 1. Primeiro, processa referências explícitas com @
            explicit_result = await self.base_file_parser.parse_async(input_text)
            
            # 2. Em seguida, detecta arquivos implícitos (sem @)
            implicit_files = await self._detect_implicit_files(input_text, working_directory)
            
            # 3. Combina os resultados
            combined_result = await self._combine_results(explicit_result, implicit_files, input_text, working_directory)
            
            return combined_result
            
        except Exception as e:
            logger.error(f"Error in intelligent file parsing: {e}")
            return ParseResult(
                status=ParseStatus.FAILED,
                error_message=f"Intelligent file parsing failed: {str(e)}",
                confidence=0.0,
                metadata={"parser": self.name, "error": str(e)}
            )
    
    async def _detect_implicit_files(self, input_text: str, working_directory: Optional[str] = None) -> List[Tuple[str, Path]]:
        """Detecta arquivos mencionados implicitamente no texto"""
        if not working_directory:
            working_directory = "."
        
        work_dir = Path(working_directory)
        detected_files = []
        
        # Cache working directory scan
        if self._working_directory_cache != str(work_dir):
            self._file_exists_cache.clear()
            self._working_directory_cache = str(work_dir)
        
        # Encontra potenciais nomes de arquivos
        potential_files = set()
        for pattern in self.compiled_patterns:
            matches = pattern.finditer(input_text)
            for match in matches:
                filename = match.group(1) if match.groups() else match.group(0)
                potential_files.add(filename.lower())
        
        # Verifica quais arquivos realmente existem
        for filename in potential_files:
            if filename in self._file_exists_cache:
                if self._file_exists_cache[filename]:
                    detected_files.append((filename, self._file_exists_cache[filename]))
                continue
            
            # Procura arquivo no working directory (recursive)
            found_path = await self._find_file_in_directory(filename, work_dir)
            
            if found_path:
                self._file_exists_cache[filename] = found_path
                detected_files.append((filename, found_path))
                logger.debug(f"IntelligentFileParser: Auto-detected file {filename} -> {found_path}")
            else:
                self._file_exists_cache[filename] = None
        
        return detected_files
    
    async def _find_file_in_directory(self, filename: str, work_dir: Path, max_depth: int = 3) -> Optional[Path]:
        """Busca arquivo no diretório (com profundidade limitada)"""
        try:
            # Busca exata primeiro
            exact_path = work_dir / filename
            if exact_path.exists() and exact_path.is_file():
                return exact_path
            
            # Busca case-insensitive
            for item in work_dir.iterdir():
                if item.is_file() and item.name.lower() == filename.lower():
                    return item
            
            # Busca recursiva (limitada)
            if max_depth > 0:
                for item in work_dir.iterdir():
                    if item.is_dir() and not item.name.startswith('.'):
                        found = await self._find_file_in_directory(filename, item, max_depth - 1)
                        if found:
                            return found
            
            return None
            
        except (OSError, PermissionError):
            return None
    
    async def _combine_results(self, explicit_result: ParseResult, implicit_files: List[Tuple[str, Path]], 
                               input_text: str, working_directory: Optional[str]) -> ParseResult:
        """Combina resultados de arquivos explícitos e implícitos"""
        
        # Se arquivos explícitos falharam E não há implícitos, retorna falha
        if not explicit_result.is_success and not implicit_files:
            return explicit_result
        
        # Prepara resultado combinado
        combined_commands = list(explicit_result.commands) if explicit_result.is_success else []
        combined_file_refs = list(explicit_result.file_references) if explicit_result.is_success else []
        uploaded_files = []
        upload_stats = {"total_files_processed": 0, "successful_uploads": 0, "failed_uploads": 0, "cache_hits": 0}
        
        # Copia stats dos arquivos explícitos
        if explicit_result.metadata and "upload_stats" in explicit_result.metadata:
            upload_stats.update(explicit_result.metadata["upload_stats"])
        
        # Copia uploaded_files dos arquivos explícitos
        if explicit_result.commands:
            for cmd in explicit_result.commands:
                if "uploaded_files" in cmd.arguments:
                    uploaded_files.extend(cmd.arguments["uploaded_files"])
        
        # Processa arquivos implícitos
        for filename, file_path in implicit_files:
            # Evita duplicatas
            if filename in combined_file_refs:
                continue
            
            try:
                upload_stats["total_files_processed"] += 1
                
                # Faz upload do arquivo
                upload_result = await self.file_uploader.upload_file_async(str(file_path))
                
                if upload_result:
                    upload_stats["successful_uploads"] += 1
                    
                    # Cria comando para arquivo implícito
                    implicit_command = ParsedCommand(
                        action="read",
                        target=filename,
                        arguments={
                            "files": [filename],
                            "file_count": 1,
                            "uploaded_files": [{
                                "original_path": filename,
                                "upload_result": upload_result,
                                "file_data": {
                                    "file_data": {
                                        "mime_type": upload_result.mime_type,
                                        "file_uri": upload_result.file_uri
                                    }
                                }
                            }],
                            "upload_method": "google_file_api",
                            "auto_detected": True  # Marca como auto-detectado
                        },
                        raw_text=f"(auto-detected: {filename})"
                    )
                    
                    combined_commands.append(implicit_command)
                    combined_file_refs.append(filename)
                    uploaded_files.append({
                        "original_path": filename,
                        "upload_result": upload_result,
                        "file_data": {
                            "file_data": {
                                "mime_type": upload_result.mime_type,
                                "file_uri": upload_result.file_uri
                            }
                        }
                    })
                    
                    logger.info(f"Auto-detected and uploaded file: {filename} -> {upload_result.file_uri}")
                    
                else:
                    upload_stats["failed_uploads"] += 1
                    logger.warning(f"Failed to upload auto-detected file: {filename}")
                    
            except Exception as e:
                upload_stats["failed_uploads"] += 1
                logger.error(f"Error processing implicit file {filename}: {e}")
        
        # Determina status final
        if combined_commands or combined_file_refs:
            status = ParseStatus.SUCCESS
            confidence = 0.95 if implicit_files else explicit_result.confidence
        else:
            status = ParseStatus.FAILED
            confidence = 0.0
        
        return ParseResult(
            status=status,
            commands=combined_commands,
            file_references=combined_file_refs,
            confidence=confidence,
            metadata={
                "parser": self.name,
                "explicit_files": len(explicit_result.file_references) if explicit_result.is_success else 0,
                "implicit_files": len(implicit_files),
                "upload_method": "google_file_api",
                "uploaded_files": uploaded_files,
                "upload_stats": upload_stats,
                "combined_parsing": True
            }
        )

    def parse_sync(self, input_text: str) -> ParseResult:
        """Fallback síncrono - executa versão assíncrona"""
        return asyncio.run(self.parse_async(input_text))