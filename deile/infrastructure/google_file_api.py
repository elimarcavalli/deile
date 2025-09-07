"""Google File API Integration - Enterprise Grade Implementation"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional, List
import mimetypes
import hashlib

from google import genai
from google.genai import errors as genai_errors

from ..core.exceptions import DEILEError


logger = logging.getLogger(__name__)


class UploadError(DEILEError):
    """Erro específico de upload de arquivo"""
    def __init__(self, message: str, file_path: str = "", error_code: str = "UPLOAD_FAILED"):
        super().__init__(message)
        self.file_path = file_path
        self.error_code = error_code


@dataclass
class FileUploadResult:
    """Resultado completo do upload de arquivo"""
    file_uri: str
    mime_type: str
    display_name: str
    file_size: int
    upload_time: float
    file_hash: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_gemini_file_data(self) -> Dict[str, Any]:
        """Converte para formato file_data da Gemini API"""
        return {
            "file_data": {
                "mime_type": self.mime_type,
                "file_uri": self.file_uri
            }
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """Converte para dicionário para logging/debug"""
        return {
            "file_uri": self.file_uri,
            "mime_type": self.mime_type,
            "display_name": self.display_name,
            "file_size": self.file_size,
            "upload_time": self.upload_time,
            "file_hash": self.file_hash,
            "metadata": self.metadata
        }


class GoogleFileUploader:
    """Enterprise-grade Google File API uploader com caching e retry logic"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        max_file_size: int = 20 * 1024 * 1024,  # 20MB default
        allowed_mime_types: Optional[List[str]] = None,
        cache_enabled: bool = True,
        max_retries: int = 3,
        retry_delay: float = 1.0
    ):
        # Configura cliente com novo SDK
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        
        self.max_file_size = max_file_size
        self.allowed_mime_types = allowed_mime_types or [
            'text/plain',
            'text/markdown',
            'text/csv',
            'application/json',
            'application/javascript',
            'text/x-python',
            'text/html',
            'text/css',
            'application/xml',
            'text/x-typescript'
        ]
        self.cache_enabled = cache_enabled
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # Cache de uploads (file_hash -> FileUploadResult)
        self._upload_cache: Dict[str, FileUploadResult] = {}
        self._upload_stats = {
            "total_uploads": 0,
            "cache_hits": 0,
            "upload_errors": 0,
            "total_bytes_uploaded": 0
        }
    
    async def upload_file(
        self,
        file_path: str,
        display_name: Optional[str] = None,
        force_upload: bool = False
    ) -> FileUploadResult:
        """
        Upload de arquivo com retry logic e caching inteligente
        
        Args:
            file_path: Caminho para o arquivo
            display_name: Nome para exibição (opcional)
            force_upload: Força upload ignorando cache
            
        Returns:
            FileUploadResult: Resultado completo do upload
            
        Raises:
            UploadError: Em caso de erro de upload
        """
        start_time = time.time()
        
        try:
            # Valida e prepara arquivo
            validated_path = self._validate_file(file_path)
            file_hash = await self._calculate_file_hash(validated_path)
            
            # Verifica cache se habilitado
            if self.cache_enabled and not force_upload:
                cached_result = self._get_cached_upload(file_hash)
                if cached_result:
                    self._upload_stats["cache_hits"] += 1
                    logger.debug(f"Cache hit for file: {file_path}")
                    return cached_result
            
            # Determina mime type
            mime_type = self._get_mime_type(validated_path)
            if not self._is_mime_type_allowed(mime_type):
                raise UploadError(
                    f"MIME type '{mime_type}' not allowed",
                    file_path=file_path,
                    error_code="INVALID_MIME_TYPE"
                )
            
            # Realiza upload com retry logic
            upload_result = await self._upload_with_retry(
                validated_path,
                display_name or validated_path.name,
                mime_type,
                file_hash,
                start_time
            )
            
            # Cache resultado se habilitado
            if self.cache_enabled:
                self._cache_upload_result(file_hash, upload_result)
            
            # Atualiza estatísticas
            self._upload_stats["total_uploads"] += 1
            self._upload_stats["total_bytes_uploaded"] += upload_result.file_size
            
            logger.info(f"File uploaded successfully: {file_path} -> {upload_result.file_uri}")
            return upload_result
            
        except UploadError:
            raise
        except Exception as e:
            self._upload_stats["upload_errors"] += 1
            raise UploadError(
                f"Unexpected error uploading file: {str(e)}",
                file_path=file_path,
                error_code="UNEXPECTED_ERROR"
            ) from e
    
    async def _upload_with_retry(
        self,
        file_path: Path,
        display_name: str,
        mime_type: str,
        file_hash: str,
        start_time: float
    ) -> FileUploadResult:
        """Executa upload com retry logic"""
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                # Realiza upload via novo Google GenAI SDK
                # CORREÇÃO DEFINITIVA: Usar keyword argument 'file' conforme assinatura
                uploaded_file = await asyncio.to_thread(
                    self.client.files.upload,
                    file=str(file_path)
                )
                
                # Constrói resultado
                file_size = file_path.stat().st_size
                upload_time = time.time() - start_time
                
                result = FileUploadResult(
                    file_uri=uploaded_file.uri,
                    mime_type=uploaded_file.mime_type,
                    display_name=uploaded_file.display_name,
                    file_size=file_size,
                    upload_time=upload_time,
                    file_hash=file_hash,
                    metadata={
                        "original_path": str(file_path),
                        "upload_attempt": attempt + 1,
                        "api_state": uploaded_file.state.name if hasattr(uploaded_file, 'state') else "unknown"
                    }
                )
                
                return result
                
            except genai_errors.ClientError as e:
                # Verifica se é InvalidArgument baseado na mensagem
                if "invalid" in str(e).lower():
                    # Não faz retry para argumentos inválidos
                    raise UploadError(
                        f"Invalid file or arguments: {str(e)}",
                        file_path=str(file_path),
                        error_code="INVALID_ARGUMENT"
                    ) from e
                else:
                    # Rate limit ou outros erros - faz retry
                    last_exception = e
                    if attempt < self.max_retries - 1:
                        wait_time = self.retry_delay * (2 ** attempt)  # Exponential backoff
                        logger.warning(f"Client error, retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries}): {e}")
                        await asyncio.sleep(wait_time)
                    continue
                
            except Exception as e:
                last_exception = e
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (attempt + 1)
                    logger.warning(f"Upload failed, retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries}): {e}")
                    await asyncio.sleep(wait_time)
                continue
        
        # Todas as tentativas falharam
        raise UploadError(
            f"Upload failed after {self.max_retries} attempts: {str(last_exception)}",
            file_path=str(file_path),
            error_code="MAX_RETRIES_EXCEEDED"
        ) from last_exception
    
    def _validate_file(self, file_path: str) -> Path:
        """Valida arquivo antes do upload"""
        path = Path(file_path)
        
        if not path.exists():
            raise UploadError(
                f"File does not exist: {file_path}",
                file_path=file_path,
                error_code="FILE_NOT_FOUND"
            )
        
        if not path.is_file():
            raise UploadError(
                f"Path is not a file: {file_path}",
                file_path=file_path,
                error_code="NOT_A_FILE"
            )
        
        file_size = path.stat().st_size
        if file_size > self.max_file_size:
            raise UploadError(
                f"File too large: {file_size} bytes (max: {self.max_file_size})",
                file_path=file_path,
                error_code="FILE_TOO_LARGE"
            )
        
        if file_size == 0:
            raise UploadError(
                f"File is empty: {file_path}",
                file_path=file_path,
                error_code="EMPTY_FILE"
            )
        
        return path
    
    async def _calculate_file_hash(self, file_path: Path) -> str:
        """Calcula hash MD5 do arquivo para cache"""
        try:
            def _hash_file():
                hash_md5 = hashlib.md5()
                with open(file_path, "rb") as f:
                    for chunk in iter(lambda: f.read(4096), b""):
                        hash_md5.update(chunk)
                return hash_md5.hexdigest()
            
            return await asyncio.to_thread(_hash_file)
        except Exception as e:
            logger.warning(f"Failed to calculate file hash for {file_path}: {e}")
            return str(hash(str(file_path) + str(time.time())))  # Fallback hash
    
    def _get_mime_type(self, file_path: Path) -> str:
        """Determina MIME type do arquivo"""
        mime_type, _ = mimetypes.guess_type(str(file_path))
        
        if mime_type is None:
            # Fallback baseado na extensão
            extension = file_path.suffix.lower()
            mime_mapping = {
                '.py': 'text/x-python',
                '.js': 'application/javascript',
                '.ts': 'text/x-typescript',
                '.md': 'text/markdown',
                '.txt': 'text/plain',
                '.json': 'application/json',
                '.html': 'text/html',
                '.css': 'text/css',
                '.xml': 'application/xml',
                '.csv': 'text/csv'
            }
            mime_type = mime_mapping.get(extension, 'application/octet-stream')
        
        return mime_type
    
    def _is_mime_type_allowed(self, mime_type: str) -> bool:
        """Verifica se MIME type é permitido"""
        return mime_type in self.allowed_mime_types
    
    def _get_cached_upload(self, file_hash: str) -> Optional[FileUploadResult]:
        """Busca upload no cache"""
        return self._upload_cache.get(file_hash)
    
    def _cache_upload_result(self, file_hash: str, result: FileUploadResult) -> None:
        """Armazena resultado no cache"""
        self._upload_cache[file_hash] = result
        
        # Limita tamanho do cache (LRU básico)
        if len(self._upload_cache) > 100:
            oldest_key = next(iter(self._upload_cache))
            del self._upload_cache[oldest_key]
    
    def clear_cache(self) -> None:
        """Limpa cache de uploads"""
        self._upload_cache.clear()
        logger.info("Upload cache cleared")
    
    def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas de upload"""
        return {
            **self._upload_stats,
            "cache_size": len(self._upload_cache),
            "cache_hit_rate": (
                self._upload_stats["cache_hits"] / max(1, self._upload_stats["total_uploads"])
            )
        }
    
    def add_allowed_mime_type(self, mime_type: str) -> None:
        """Adiciona MIME type permitido"""
        if mime_type not in self.allowed_mime_types:
            self.allowed_mime_types.append(mime_type)
            logger.info(f"Added allowed MIME type: {mime_type}")
    
    def remove_allowed_mime_type(self, mime_type: str) -> None:
        """Remove MIME type permitido"""
        if mime_type in self.allowed_mime_types:
            self.allowed_mime_types.remove(mime_type)
            logger.info(f"Removed allowed MIME type: {mime_type}")


# Singleton instance para uso global
_file_uploader: Optional[GoogleFileUploader] = None


def get_file_uploader() -> GoogleFileUploader:
    """Retorna instância singleton do GoogleFileUploader"""
    global _file_uploader
    if _file_uploader is None:
        _file_uploader = GoogleFileUploader()
    return _file_uploader


def reset_file_uploader() -> None:
    """Reseta instância singleton (para testes)"""
    global _file_uploader
    _file_uploader = None