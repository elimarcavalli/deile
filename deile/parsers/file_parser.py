"""Parser para referências de arquivos (@arquivo.txt) - Google File API Integration"""

import re
import asyncio
import logging
from typing import List, Optional
from pathlib import Path

from .base import RegexParser, ParseResult, ParseStatus, ParsedCommand
from ..core.exceptions import ParserError
from ..infrastructure.google_file_api import GoogleFileUploader, get_file_uploader, UploadError


class ApiFileNotFoundError(ParserError):
    """Exceção para arquivo não encontrado na Google File API"""
    pass


class ApiPermissionError(ParserError):
    """Exceção para problemas de permissão na Google File API"""
    pass


class ApiAuthenticationError(ParserError):
    """Exceção para problemas de autenticação na Google File API"""
    pass


logger = logging.getLogger(__name__)


class FileParser(RegexParser):
    """Parser para referências de arquivos usando sintaxe @arquivo.txt
    
    NOVA IMPLEMENTAÇÃO: Upload via Google File API em vez de injeção de conteúdo
    """
    
    def __init__(self, file_uploader: Optional[GoogleFileUploader] = None):
        # Dependency Injection para GoogleFileUploader
        self.file_uploader = file_uploader or get_file_uploader()
        
        # Padrões para diferentes tipos de referência de arquivo
        file_patterns = [
            r'@([a-zA-Z0-9_\-/\\\.]+)(?=[\s\.,;:!?]|$)',  # @arquivo.txt, @pasta/arquivo.py (parada em pontuação)
            r'@@([^@\s]+)@@',                              # @@arquivo.txt@@ (formato legacy)
        ]
        
        super().__init__([re.compile(pattern, re.IGNORECASE) for pattern in file_patterns])
        
        # Estatísticas de upload
        self._upload_stats = {
            "total_files_processed": 0,
            "successful_uploads": 0,
            "failed_uploads": 0,
            "cache_hits": 0
        }
    
    @property
    def name(self) -> str:
        return "file_parser"
    
    @property
    def description(self) -> str:
        return "Parses file references using @filename syntax"
    
    @property
    def patterns(self) -> List[str]:
        return [
            r'@([a-zA-Z0-9_\-/\\\.]+)(?=[\s\.,;:!?]|$)',
            r'@@([^@\s]+)@@'
        ]
    
    @property
    def priority(self) -> int:
        return 100  # Alta prioridade para referências de arquivo
    
    def can_parse(self, input_text: str) -> bool:
        """Verifica se há referências de arquivo na entrada"""
        return '@' in input_text and bool(self._compiled_patterns[0].search(input_text))
    
    async def parse_async(self, input_text: str) -> ParseResult:
        """
        IMPLEMENTAÇÃO ASYNC: Parseia referências e faz upload via Google File API
        
        EM VEZ DE: Ler conteúdo e injetar no prompt  
        AGORA: Upload arquivo de forma assíncrona e retorna file_uri para Context Manager
        """
        self._parse_count += 1
        self._upload_stats["total_files_processed"] += 1
        
        try:
            file_references = []
            commands = []
            uploaded_files = []  # NOVO: Lista de arquivos uploadados
            
            # Encontra todas as referências de arquivo
            all_matches = []
            for pattern in self._compiled_patterns:
                matches = pattern.finditer(input_text)
                all_matches.extend(matches)
            
            if not all_matches:
                return self._create_error_result("No file references found")
            
            # Processa cada match e faz upload
            for match in all_matches:
                file_path = match.group(1)
                
                # Valida o caminho do arquivo
                if not self._is_valid_file_path(file_path):
                    logger.warning(f"Invalid file path skipped: {file_path}")
                    continue
                
                # NOVA LÓGICA: Upload do arquivo em vez de leitura
                try:
                    # Upload assíncrono nativo - sem asyncio.run()
                    upload_result = await self.file_uploader.upload_file(file_path)
                    
                    uploaded_files.append({
                        "original_path": file_path,
                        "upload_result": upload_result,
                        "file_data": upload_result.to_gemini_file_data()
                    })
                    
                    file_references.append(file_path)
                    self._upload_stats["successful_uploads"] += 1
                    
                    logger.info(f"File uploaded successfully: {file_path} -> {upload_result.file_uri}")
                    
                except UploadError as e:
                    self._upload_stats["failed_uploads"] += 1
                    error_msg = str(e).lower()
                    
                    # Mapeia erros específicos da API para exceções customizadas
                    if "not found" in error_msg or "404" in error_msg:
                        logger.error(f"File not found in API: {file_path}")
                        raise ApiFileNotFoundError(f"Arquivo '{file_path}' não foi encontrado na API de arquivos")
                    elif "permission" in error_msg or "403" in error_msg:
                        logger.error(f"Permission error uploading: {file_path}")  
                        raise ApiPermissionError(f"Sem permissão para acessar o arquivo '{file_path}'")
                    elif "auth" in error_msg or "401" in error_msg:
                        logger.error(f"Authentication error uploading: {file_path}")
                        raise ApiAuthenticationError(f"Erro de autenticação ao acessar '{file_path}'")
                    else:
                        logger.error(f"Upload failed for file {file_path}: {e}")
                        # Continua processando outros arquivos em erros genéricos
                        continue
                    
                except FileNotFoundError as e:
                    self._upload_stats["failed_uploads"] += 1
                    logger.error(f"Local file not found: {file_path}")
                    raise ApiFileNotFoundError(f"Arquivo '{file_path}' não encontrado localmente para upload")
                    
                except Exception as e:
                    self._upload_stats["failed_uploads"] += 1
                    logger.error(f"Unexpected error uploading {file_path}: {e}")
                    # Para erros inesperados, continua processando
                    continue
            
            if not uploaded_files:
                return self._create_error_result("No files could be uploaded")
            
            # Remove duplicatas preservando ordem
            unique_files = []
            seen = set()
            for file_info in uploaded_files:
                file_path = file_info["original_path"]
                if file_path not in seen:
                    unique_files.append(file_info)
                    seen.add(file_path)
            
            # Determina ação baseada no contexto
            action = self._determine_action(input_text, [f["original_path"] for f in unique_files])
            
            # Cria comando principal com metadados de upload
            primary_command = ParsedCommand(
                action=action,
                target=unique_files[0]["original_path"] if unique_files else None,
                arguments={
                    "files": [f["original_path"] for f in unique_files],
                    "file_count": len(unique_files),
                    "uploaded_files": unique_files,  # NOVO: Inclui dados de upload
                    "upload_method": "google_file_api"  # NOVO: Identifica método
                },
                raw_text=input_text
            )
            commands.append(primary_command)
            
            # NOVA LÓGICA: Não precisa mais de tools para read_file
            # O arquivo já foi disponibilizado via File API
            tool_requests = []
            
            confidence = 0.95  # Alta confiança para uploads bem-sucedidos
            
            return ParseResult(
                status=ParseStatus.SUCCESS,
                commands=commands,
                file_references=[f["original_path"] for f in unique_files],
                tool_requests=tool_requests,  # Vazio - não precisa mais de tools
                confidence=confidence,
                metadata={
                    "parser": self.name,
                    "matched_patterns": len(all_matches),
                    "unique_files": len(unique_files),
                    "action": action,
                    "upload_method": "google_file_api",
                    "uploaded_files": unique_files,
                    "upload_stats": self._upload_stats.copy()
                }
            )
            
        except Exception as e:
            self._upload_stats["failed_uploads"] += 1
            logger.error(f"Error in FileParser.parse: {e}", exc_info=True)
            return ParseResult(
                status=ParseStatus.FAILED,
                error_message=f"Error parsing and uploading file references: {str(e)}",
                metadata={
                    "parser": self.name, 
                    "error": str(e),
                    "upload_stats": self._upload_stats.copy()
                }
            )
    
    async def get_suggestions(self, partial_input: str) -> List[str]:
        """Retorna sugestões de arquivos para autocompletar"""
        suggestions = []
        
        # Procura por @ no final da entrada para sugerir arquivos
        if partial_input.endswith('@') or '@' in partial_input:
            try:
                # Obtém lista de arquivos do diretório atual
                current_dir = Path.cwd()
                
                # Se há um @ seguido de texto parcial
                at_match = re.search(r'@([^@\s]*)$', partial_input)
                if at_match:
                    partial_name = at_match.group(1)
                    
                    # Busca arquivos que começam com o texto parcial
                    for file_path in current_dir.rglob(f"{partial_name}*"):
                        if file_path.is_file():
                            relative_path = file_path.relative_to(current_dir)
                            suggestions.append(f"@{relative_path}")
                            
                            # Limita sugestões
                            if len(suggestions) >= 10:
                                break
                
                # Se é apenas @ no final, sugere alguns arquivos comuns
                elif partial_input.endswith('@'):
                    common_files = ['README.md', 'main.py', 'app.py', 'index.js', 'package.json']
                    for filename in common_files:
                        file_path = current_dir / filename
                        if file_path.exists():
                            suggestions.append(f"@{filename}")
                    
            except Exception:
                # Em caso de erro, retorna lista vazia
                pass
        
        return suggestions
    
    def _is_valid_file_path(self, file_path: str) -> bool:
        """Valida se o caminho do arquivo é válido - PRIMEIRA BARREIRA DE DEFESA"""
        if not file_path or file_path.isspace():
            return False
        
        # NOVA VALIDAÇÃO: Rejeita caminhos absolutos (começam com / ou \ )
        if file_path.startswith('/') or file_path.startswith('\\'):
            return False
        
        # ROBUSTECIDA: Verifica se não contém .. (path traversal)
        if '..' in file_path:
            return False
        
        # Verifica caracteres proibidos básicos
        invalid_chars = ['<', '>', ':', '"', '|', '?', '*']
        if any(char in file_path for char in invalid_chars):
            return False
        
        # Verifica se não é muito longo
        if len(file_path) > 260:  # Limite do Windows
            return False
        
        return True
    
    def _determine_action(self, input_text: str, files: List[str]) -> str:
        """Determina a ação baseada no contexto da entrada"""
        input_lower = input_text.lower()
        
        # Ações de leitura
        read_keywords = ['read', 'show', 'display', 'view', 'see', 'content', 'open', 'examine', 'analyze']
        if any(keyword in input_lower for keyword in read_keywords):
            return "read"
        
        # Ações de escrita/criação
        write_keywords = ['write', 'create', 'make', 'generate', 'save', 'update', 'modify', 'edit', 'change']
        if any(keyword in input_lower for keyword in write_keywords):
            return "write"
        
        # Ações de listagem
        list_keywords = ['list', 'ls', 'dir', 'files', 'directory']
        if any(keyword in input_lower for keyword in list_keywords):
            return "list"
        
        # Ações de deleção
        delete_keywords = ['delete', 'remove', 'rm', 'del', 'erase']
        if any(keyword in input_lower for keyword in delete_keywords):
            return "delete"
        
        # Baseado na extensão do arquivo
        if files:
            first_file = files[0]
            if first_file.endswith(('.md', '.txt', '.py', '.js', '.html', '.css')):
                # Arquivos que tipicamente são lidos
                return "read"
        
        # Default: read
        return "read"
    
    def get_confidence(self, input_text: str) -> float:
        """Calcula confiança baseada na qualidade das referências"""
        if not self.can_parse(input_text):
            return 0.0
        
        confidence = 0.0
        
        # Conta referências válidas
        valid_refs = 0
        total_refs = 0
        
        for pattern in self._compiled_patterns:
            for match in pattern.finditer(input_text):
                total_refs += 1
                file_path = match.group(1)
                if self._is_valid_file_path(file_path):
                    valid_refs += 1
        
        if total_refs > 0:
            base_confidence = valid_refs / total_refs
            
            # Bonus por contexto claro
            if any(keyword in input_text.lower() for keyword in ['read', 'write', 'show', 'create']):
                base_confidence += 0.1
            
            # Bonus por extensões de arquivo válidas
            if any(match.group(1).count('.') == 1 for pattern in self._compiled_patterns for match in pattern.finditer(input_text)):
                base_confidence += 0.1
            
            confidence = min(1.0, base_confidence)
        
        return confidence
    
    def parse(self, input_text: str) -> ParseResult:
        """Wrapper síncrono para compatibilidade - deprecado, use parse_async()"""
        # Para compatibilidade com testes, mas gera warning
        logger.warning("FileParser.parse() síncrono está deprecado, use parse_async()")
        
        # Retorna resultado de falha orientando para uso assíncrono
        return ParseResult(
            status=ParseStatus.FAILED,
            error_message="FileParser requires async execution. Use parse_async() method.",
            metadata={"parser": self.name, "requires_async": True}
        )
    
    def get_upload_stats(self) -> dict:
        """Retorna estatísticas de upload do parser"""
        uploader_stats = self.file_uploader.get_stats()
        return {
            "parser_stats": self._upload_stats.copy(),
            "uploader_stats": uploader_stats,
            "total_success_rate": (
                self._upload_stats["successful_uploads"] / 
                max(1, self._upload_stats["total_files_processed"])
            )
        }
    
    def clear_upload_cache(self) -> None:
        """Limpa cache de upload"""
        self.file_uploader.clear_cache()
        logger.info("FileParser upload cache cleared")