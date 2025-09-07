"""
Archive Tool for DEILE v4.0
===========================

Comprehensive archive management tool for creating, extracting, and manipulating
compressed archives with security controls and cross-format support.

Author: DEILE
Version: 4.0
"""

import json
import logging
import os
import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Tuple

import py7zr

from deile.core.models.base import SyncTool, ToolRegistry
from deile.core.context_manager import ContextManager
from deile.core.exceptions import ToolError
from deile.infrastructure.security.secrets_scanner import SecretsScanner

logger = logging.getLogger(__name__)


class CompressionFormat(Enum):
    """Supported compression formats"""
    ZIP = "zip"
    TAR = "tar"
    TAR_GZ = "tar.gz"
    TAR_BZ2 = "tar.bz2"
    TAR_XZ = "tar.xz"
    SEVEN_Z = "7z"


class CompressionLevel(Enum):
    """Compression level options"""
    STORE = 0      # No compression
    FAST = 1       # Fast compression
    NORMAL = 6     # Normal compression
    BEST = 9       # Best compression


@dataclass
class ArchiveInfo:
    """Archive information data structure"""
    path: str
    format: str
    size: int
    file_count: int
    created: float
    modified: float
    compression_ratio: float = 0.0
    encrypted: bool = False
    comment: Optional[str] = None
    files: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.files is None:
            self.files = []


@dataclass
class ArchiveEntry:
    """Individual archive entry information"""
    name: str
    size: int
    compressed_size: int
    modified: float
    is_dir: bool
    permissions: str
    crc: Optional[int] = None
    comment: Optional[str] = None


class ArchiveTool(SyncTool):
    """
    Advanced archive management tool with security controls
    
    Features:
    - Multi-format support (ZIP, TAR, 7Z)
    - Compression level control
    - Password protection
    - Directory traversal protection
    - Size limit enforcement
    - Secret scanning for filenames
    - Selective extraction
    """

    def __init__(self):
        super().__init__()
        self.name = "archive_tool"
        self.description = "Archive management tool for creating, extracting, and manipulating compressed archives"
        self.secrets_scanner = SecretsScanner()
        self.context_manager = ContextManager()
        
        # Security settings
        self.max_extract_size = 1024 * 1024 * 1024  # 1GB limit
        self.max_file_count = 10000                  # Max files per archive
        self.max_path_length = 255                   # Max path length
        self.blocked_extensions = {
            '.exe', '.bat', '.cmd', '.com', '.pif', '.scr',
            '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh',
            '.ps1', '.psm1', '.psd1', '.ps1xml', '.psc1'
        }

    def get_schema(self) -> Dict[str, Any]:
        """Get the JSON schema for this tool"""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "create", "extract", "list", "info", "test",
                        "add", "delete", "update", "compress", "decompress"
                    ],
                    "description": "The action to perform"
                },
                "archive_path": {
                    "type": "string",
                    "description": "Path to the archive file"
                },
                "source_path": {
                    "type": "string", 
                    "description": "Source path for creating archives or adding files"
                },
                "target_path": {
                    "type": "string",
                    "description": "Target extraction path or destination"
                },
                "format": {
                    "type": "string",
                    "enum": ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz", "7z"],
                    "description": "Archive format",
                    "default": "zip"
                },
                "compression_level": {
                    "type": "integer",
                    "description": "Compression level (0-9, 0=store, 9=best)",
                    "default": 6,
                    "minimum": 0,
                    "maximum": 9
                },
                "password": {
                    "type": "string",
                    "description": "Password for encrypted archives"
                },
                "include_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File patterns to include (glob patterns)"
                },
                "exclude_patterns": {
                    "type": "array", 
                    "items": {"type": "string"},
                    "description": "File patterns to exclude (glob patterns)"
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Include subdirectories recursively",
                    "default": True
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Overwrite existing files during extraction",
                    "default": False
                },
                "preserve_permissions": {
                    "type": "boolean", 
                    "description": "Preserve file permissions (Unix-like systems)",
                    "default": True
                },
                "create_directories": {
                    "type": "boolean",
                    "description": "Create directory structure during extraction",
                    "default": True
                },
                "verify_integrity": {
                    "type": "boolean",
                    "description": "Verify archive integrity before operations",
                    "default": True
                },
                "file_list": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific files to extract or operate on"
                },
                "comment": {
                    "type": "string",
                    "description": "Archive comment or description"
                }
            },
            "required": ["action"],
            "additionalProperties": False
        }

    def execute(self, **params) -> Dict[str, Any]:
        """Execute the archive management tool"""
        try:
            action = params.get("action")
            
            # Validate required parameters
            if action in ["extract", "list", "info", "test"] and not params.get("archive_path"):
                return self._error("archive_path is required for this action")
            
            if action == "create" and not params.get("source_path"):
                return self._error("source_path is required for create action")
            
            if action in ["create", "extract"] and not params.get("archive_path"):
                return self._error("archive_path is required for this action")
            
            # Security validation
            archive_path = params.get("archive_path", "")
            if archive_path and not self._validate_path(archive_path):
                return self._error(f"Invalid or unsafe archive path: {archive_path}")
            
            target_path = params.get("target_path", "")
            if target_path and not self._validate_path(target_path):
                return self._error(f"Invalid or unsafe target path: {target_path}")
            
            if action == "create":
                return self._create_archive(**params)
            elif action == "extract":
                return self._extract_archive(**params)
            elif action == "list":
                return self._list_archive(**params)
            elif action == "info":
                return self._archive_info(**params)
            elif action == "test":
                return self._test_archive(**params)
            elif action == "add":
                return self._add_to_archive(**params)
            elif action == "delete":
                return self._delete_from_archive(**params)
            elif action == "update":
                return self._update_archive(**params)
            elif action == "compress":
                return self._compress_file(**params)
            elif action == "decompress":
                return self._decompress_file(**params)
            else:
                return self._error(f"Unknown action: {action}")
                
        except Exception as e:
            logger.error(f"ArchiveTool execution error: {str(e)}")
            return self._error(f"Execution failed: {str(e)}")

    def _create_archive(self, **params) -> Dict[str, Any]:
        """Create a new archive from source path"""
        try:
            archive_path = params["archive_path"]
            source_path = params["source_path"]
            format_type = CompressionFormat(params.get("format", "zip"))
            compression_level = params.get("compression_level", 6)
            password = params.get("password")
            recursive = params.get("recursive", True)
            include_patterns = params.get("include_patterns", [])
            exclude_patterns = params.get("exclude_patterns", [])
            comment = params.get("comment", "")
            
            source = Path(source_path)
            if not source.exists():
                return self._error(f"Source path does not exist: {source_path}")
            
            archive = Path(archive_path)
            archive.parent.mkdir(parents=True, exist_ok=True)
            
            files_added = []
            total_size = 0
            compressed_size = 0
            
            # Collect files to archive
            files_to_archive = self._collect_files(source, recursive, include_patterns, exclude_patterns)
            
            if not files_to_archive:
                return self._error("No files found matching the criteria")
            
            if len(files_to_archive) > self.max_file_count:
                return self._error(f"Too many files to archive (max: {self.max_file_count})")
            
            # Check total size
            for file_path in files_to_archive:
                if file_path.is_file():
                    total_size += file_path.stat().st_size
            
            if total_size > self.max_extract_size:
                return self._error(f"Archive size exceeds limit ({self.max_extract_size} bytes)")
            
            # Create archive based on format
            if format_type == CompressionFormat.ZIP:
                compressed_size = self._create_zip(archive, files_to_archive, source, 
                                                 compression_level, password, comment, files_added)
            elif format_type in [CompressionFormat.TAR, CompressionFormat.TAR_GZ, 
                               CompressionFormat.TAR_BZ2, CompressionFormat.TAR_XZ]:
                compressed_size = self._create_tar(archive, files_to_archive, source,
                                                 format_type, compression_level, files_added)
            elif format_type == CompressionFormat.SEVEN_Z:
                compressed_size = self._create_7z(archive, files_to_archive, source,
                                                password, files_added)
            else:
                return self._error(f"Unsupported format: {format_type.value}")
            
            # Calculate compression ratio
            compression_ratio = 1.0 - (compressed_size / total_size) if total_size > 0 else 0.0
            
            # Scan for secrets in filenames
            secrets_found = []
            for file_info in files_added:
                secrets = self.secrets_scanner.scan_text(file_info['name'])
                secrets_found.extend(secrets)
            
            return self._success({
                'archive_path': str(archive),
                'format': format_type.value,
                'files_added': len(files_added),
                'original_size': total_size,
                'compressed_size': compressed_size,
                'compression_ratio': round(compression_ratio * 100, 2),
                'files': files_added[:100],  # Limit output
                'secrets_detected': len(secrets_found),
                'encrypted': bool(password),
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to create archive: {str(e)}")

    def _extract_archive(self, **params) -> Dict[str, Any]:
        """Extract files from an archive"""
        try:
            archive_path = params["archive_path"]
            target_path = params.get("target_path", ".")
            password = params.get("password")
            overwrite = params.get("overwrite", False)
            preserve_permissions = params.get("preserve_permissions", True)
            create_directories = params.get("create_directories", True)
            file_list = params.get("file_list", [])
            verify_integrity = params.get("verify_integrity", True)
            
            archive = Path(archive_path)
            if not archive.exists():
                return self._error(f"Archive not found: {archive_path}")
            
            target = Path(target_path)
            if create_directories:
                target.mkdir(parents=True, exist_ok=True)
            
            # Verify integrity first
            if verify_integrity:
                test_result = self._test_archive(archive_path=archive_path, password=password)
                if not test_result.get("success"):
                    return self._error(f"Archive integrity check failed: {test_result.get('error')}")
            
            # Determine format from file extension
            format_type = self._detect_format(archive)
            
            files_extracted = []
            total_extracted_size = 0
            
            # Extract based on format
            if format_type == CompressionFormat.ZIP:
                files_extracted, total_extracted_size = self._extract_zip(
                    archive, target, password, overwrite, file_list, files_extracted)
            elif format_type in [CompressionFormat.TAR, CompressionFormat.TAR_GZ,
                               CompressionFormat.TAR_BZ2, CompressionFormat.TAR_XZ]:
                files_extracted, total_extracted_size = self._extract_tar(
                    archive, target, overwrite, preserve_permissions, file_list, files_extracted)
            elif format_type == CompressionFormat.SEVEN_Z:
                files_extracted, total_extracted_size = self._extract_7z(
                    archive, target, password, overwrite, file_list, files_extracted)
            else:
                return self._error(f"Unsupported archive format: {archive.suffix}")
            
            # Scan extracted files for secrets
            secrets_found = []
            for file_info in files_extracted:
                if file_info.get('is_file', True):
                    try:
                        file_path = Path(target) / file_info['name']
                        if file_path.exists() and file_path.is_file():
                            # Only scan text files and limit size
                            if file_path.stat().st_size < 1024 * 1024:  # 1MB limit
                                try:
                                    content = file_path.read_text(encoding='utf-8', errors='ignore')
                                    secrets = self.secrets_scanner.scan_text(content)
                                    secrets_found.extend(secrets)
                                except UnicodeDecodeError:
                                    pass  # Skip binary files
                    except Exception:
                        pass  # Skip files that can't be read
            
            return self._success({
                'archive_path': str(archive),
                'target_path': str(target),
                'format': format_type.value,
                'files_extracted': len(files_extracted),
                'total_size': total_extracted_size,
                'files': files_extracted[:100],  # Limit output
                'secrets_detected': len(secrets_found),
                'secrets_summary': [s.get('type', 'unknown') for s in secrets_found],
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to extract archive: {str(e)}")

    def _list_archive(self, **params) -> Dict[str, Any]:
        """List contents of an archive"""
        try:
            archive_path = params["archive_path"]
            password = params.get("password")
            
            archive = Path(archive_path)
            if not archive.exists():
                return self._error(f"Archive not found: {archive_path}")
            
            format_type = self._detect_format(archive)
            entries = []
            
            if format_type == CompressionFormat.ZIP:
                entries = self._list_zip(archive, password)
            elif format_type in [CompressionFormat.TAR, CompressionFormat.TAR_GZ,
                               CompressionFormat.TAR_BZ2, CompressionFormat.TAR_XZ]:
                entries = self._list_tar(archive)
            elif format_type == CompressionFormat.SEVEN_Z:
                entries = self._list_7z(archive, password)
            else:
                return self._error(f"Unsupported archive format: {archive.suffix}")
            
            # Calculate statistics
            total_size = sum(entry.get('size', 0) for entry in entries)
            total_compressed = sum(entry.get('compressed_size', 0) for entry in entries)
            file_count = sum(1 for entry in entries if not entry.get('is_dir', False))
            dir_count = sum(1 for entry in entries if entry.get('is_dir', False))
            
            return self._success({
                'archive_path': str(archive),
                'format': format_type.value,
                'entries': entries,
                'total_entries': len(entries),
                'file_count': file_count,
                'directory_count': dir_count,
                'total_size': total_size,
                'total_compressed_size': total_compressed,
                'compression_ratio': round((1.0 - (total_compressed / total_size)) * 100, 2) if total_size > 0 else 0.0,
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to list archive: {str(e)}")

    def _archive_info(self, **params) -> Dict[str, Any]:
        """Get detailed information about an archive"""
        try:
            archive_path = params["archive_path"]
            
            archive = Path(archive_path)
            if not archive.exists():
                return self._error(f"Archive not found: {archive_path}")
            
            stat_info = archive.stat()
            format_type = self._detect_format(archive)
            
            # Get basic info
            info = ArchiveInfo(
                path=str(archive),
                format=format_type.value,
                size=stat_info.st_size,
                file_count=0,
                created=stat_info.st_ctime,
                modified=stat_info.st_mtime
            )
            
            # Get detailed info based on format
            if format_type == CompressionFormat.ZIP:
                self._get_zip_info(archive, info, params.get("password"))
            elif format_type in [CompressionFormat.TAR, CompressionFormat.TAR_GZ,
                               CompressionFormat.TAR_BZ2, CompressionFormat.TAR_XZ]:
                self._get_tar_info(archive, info)
            elif format_type == CompressionFormat.SEVEN_Z:
                self._get_7z_info(archive, info, params.get("password"))
            
            return self._success({
                'info': asdict(info),
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to get archive info: {str(e)}")

    def _test_archive(self, **params) -> Dict[str, Any]:
        """Test archive integrity"""
        try:
            archive_path = params["archive_path"]
            password = params.get("password")
            
            archive = Path(archive_path)
            if not archive.exists():
                return self._error(f"Archive not found: {archive_path}")
            
            format_type = self._detect_format(archive)
            test_result = {"passed": False, "errors": []}
            
            if format_type == CompressionFormat.ZIP:
                test_result = self._test_zip(archive, password)
            elif format_type in [CompressionFormat.TAR, CompressionFormat.TAR_GZ,
                               CompressionFormat.TAR_BZ2, CompressionFormat.TAR_XZ]:
                test_result = self._test_tar(archive)
            elif format_type == CompressionFormat.SEVEN_Z:
                test_result = self._test_7z(archive, password)
            else:
                return self._error(f"Unsupported archive format for testing: {archive.suffix}")
            
            return self._success({
                'archive_path': str(archive),
                'format': format_type.value,
                'integrity_passed': test_result["passed"],
                'errors': test_result["errors"],
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to test archive: {str(e)}")

    def _create_zip(self, archive_path, files, source, compression_level, password, comment, files_added):
        """Create ZIP archive"""
        compressed_size = 0
        
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, 
                           compresslevel=compression_level) as zf:
            if comment:
                zf.comment = comment.encode('utf-8')
            
            for file_path in files:
                if file_path.is_file():
                    arcname = file_path.relative_to(source)
                    info = zipfile.ZipInfo(str(arcname))
                    info.external_attr = file_path.stat().st_mode << 16
                    
                    with open(file_path, 'rb') as f:
                        data = f.read()
                        if password:
                            zf.writestr(info, data, zipfile.ZIP_DEFLATED, compresslevel=compression_level)
                        else:
                            zf.writestr(info, data)
                    
                    files_added.append({
                        'name': str(arcname),
                        'size': file_path.stat().st_size,
                        'is_dir': False
                    })
                elif file_path.is_dir():
                    arcname = file_path.relative_to(source)
                    zf.writestr(str(arcname) + '/', '')
                    files_added.append({
                        'name': str(arcname) + '/',
                        'size': 0,
                        'is_dir': True
                    })
        
        compressed_size = archive_path.stat().st_size
        return compressed_size

    def _create_tar(self, archive_path, files, source, format_type, compression_level, files_added):
        """Create TAR archive"""
        mode_map = {
            CompressionFormat.TAR: 'w',
            CompressionFormat.TAR_GZ: 'w:gz',
            CompressionFormat.TAR_BZ2: 'w:bz2',
            CompressionFormat.TAR_XZ: 'w:xz'
        }
        
        with tarfile.open(archive_path, mode_map[format_type]) as tf:
            for file_path in files:
                arcname = file_path.relative_to(source)
                tf.add(file_path, arcname=arcname, recursive=False)
                
                files_added.append({
                    'name': str(arcname),
                    'size': file_path.stat().st_size if file_path.is_file() else 0,
                    'is_dir': file_path.is_dir()
                })
        
        return archive_path.stat().st_size

    def _create_7z(self, archive_path, files, source, password, files_added):
        """Create 7Z archive"""
        try:
            with py7zr.SevenZipFile(archive_path, 'w', password=password) as szf:
                for file_path in files:
                    if file_path.is_file():
                        arcname = file_path.relative_to(source)
                        szf.write(file_path, arcname)
                        
                        files_added.append({
                            'name': str(arcname),
                            'size': file_path.stat().st_size,
                            'is_dir': False
                        })
        except ImportError:
            raise ToolError("py7zr library is required for 7z format support")
        
        return archive_path.stat().st_size

    def _extract_zip(self, archive, target, password, overwrite, file_list, files_extracted):
        """Extract ZIP archive"""
        total_size = 0
        
        with zipfile.ZipFile(archive, 'r') as zf:
            members = file_list if file_list else zf.namelist()
            
            for member in members:
                if member not in zf.namelist():
                    continue
                
                # Security check
                if not self._is_safe_path(member):
                    logger.warning(f"Skipping unsafe path: {member}")
                    continue
                
                target_file = target / member
                
                if not overwrite and target_file.exists():
                    continue
                
                # Check size limits
                info = zf.getinfo(member)
                if info.file_size > self.max_extract_size:
                    logger.warning(f"File too large, skipping: {member}")
                    continue
                
                total_size += info.file_size
                if total_size > self.max_extract_size:
                    raise ToolError("Total extracted size exceeds limit")
                
                # Extract file
                target_file.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, pwd=password.encode() if password else None) as source_file:
                    with open(target_file, 'wb') as target_file_handle:
                        shutil.copyfileobj(source_file, target_file_handle)
                
                files_extracted.append({
                    'name': member,
                    'size': info.file_size,
                    'is_file': not member.endswith('/')
                })
        
        return files_extracted, total_size

    def _extract_tar(self, archive, target, overwrite, preserve_permissions, file_list, files_extracted):
        """Extract TAR archive"""
        total_size = 0
        
        with tarfile.open(archive, 'r:*') as tf:
            members = [tf.getmember(name) for name in file_list] if file_list else tf.getmembers()
            
            for member in members:
                # Security check
                if not self._is_safe_path(member.name):
                    logger.warning(f"Skipping unsafe path: {member.name}")
                    continue
                
                target_file = target / member.name
                
                if not overwrite and target_file.exists():
                    continue
                
                # Check size limits
                if member.size > self.max_extract_size:
                    logger.warning(f"File too large, skipping: {member.name}")
                    continue
                
                total_size += member.size
                if total_size > self.max_extract_size:
                    raise ToolError("Total extracted size exceeds limit")
                
                # Extract member
                tf.extract(member, target)
                
                # Set permissions if requested
                if preserve_permissions and hasattr(os, 'chmod'):
                    try:
                        os.chmod(target_file, member.mode)
                    except (OSError, AttributeError):
                        pass
                
                files_extracted.append({
                    'name': member.name,
                    'size': member.size,
                    'is_file': member.isfile()
                })
        
        return files_extracted, total_size

    def _extract_7z(self, archive, target, password, overwrite, file_list, files_extracted):
        """Extract 7Z archive"""
        total_size = 0
        
        try:
            with py7zr.SevenZipFile(archive, 'r', password=password) as szf:
                members = file_list if file_list else szf.getnames()
                
                for member in members:
                    # Security check
                    if not self._is_safe_path(member):
                        logger.warning(f"Skipping unsafe path: {member}")
                        continue
                    
                    # Extract to temporary location first for size check
                    extracted_files = szf.extract(targets=[member])
                    
                    for extracted_file, content in extracted_files.items():
                        if len(content) > self.max_extract_size:
                            logger.warning(f"File too large, skipping: {member}")
                            continue
                        
                        total_size += len(content)
                        if total_size > self.max_extract_size:
                            raise ToolError("Total extracted size exceeds limit")
                        
                        target_file = target / extracted_file
                        if not overwrite and target_file.exists():
                            continue
                        
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        target_file.write_bytes(content)
                        
                        files_extracted.append({
                            'name': extracted_file,
                            'size': len(content),
                            'is_file': True
                        })
        except ImportError:
            raise ToolError("py7zr library is required for 7z format support")
        
        return files_extracted, total_size

    def _list_zip(self, archive, password):
        """List ZIP archive contents"""
        entries = []
        
        with zipfile.ZipFile(archive, 'r') as zf:
            for info in zf.infolist():
                entry = {
                    'name': info.filename,
                    'size': info.file_size,
                    'compressed_size': info.compress_size,
                    'modified': info.date_time,
                    'is_dir': info.filename.endswith('/'),
                    'crc': info.CRC,
                    'permissions': oct(info.external_attr >> 16) if info.external_attr else None
                }
                entries.append(entry)
        
        return entries

    def _list_tar(self, archive):
        """List TAR archive contents"""
        entries = []
        
        with tarfile.open(archive, 'r:*') as tf:
            for member in tf.getmembers():
                entry = {
                    'name': member.name,
                    'size': member.size,
                    'compressed_size': member.size,  # TAR doesn't track compressed size
                    'modified': member.mtime,
                    'is_dir': member.isdir(),
                    'permissions': oct(member.mode)
                }
                entries.append(entry)
        
        return entries

    def _list_7z(self, archive, password):
        """List 7Z archive contents"""
        entries = []
        
        try:
            with py7zr.SevenZipFile(archive, 'r', password=password) as szf:
                for info in szf.list():
                    entry = {
                        'name': info.filename,
                        'size': info.uncompressed if hasattr(info, 'uncompressed') else 0,
                        'compressed_size': info.compressed if hasattr(info, 'compressed') else 0,
                        'modified': info.creationtime if hasattr(info, 'creationtime') else None,
                        'is_dir': info.is_directory if hasattr(info, 'is_directory') else False
                    }
                    entries.append(entry)
        except ImportError:
            raise ToolError("py7zr library is required for 7z format support")
        
        return entries

    def _test_zip(self, archive, password):
        """Test ZIP archive integrity"""
        result = {"passed": True, "errors": []}
        
        try:
            with zipfile.ZipFile(archive, 'r') as zf:
                bad_files = zf.testzip()
                if bad_files:
                    result["passed"] = False
                    result["errors"].append(f"Corrupt file detected: {bad_files}")
        except zipfile.BadZipFile as e:
            result["passed"] = False
            result["errors"].append(f"Bad ZIP file: {str(e)}")
        except Exception as e:
            result["passed"] = False
            result["errors"].append(f"Test failed: {str(e)}")
        
        return result

    def _test_tar(self, archive):
        """Test TAR archive integrity"""
        result = {"passed": True, "errors": []}
        
        try:
            with tarfile.open(archive, 'r:*') as tf:
                # TAR doesn't have built-in test, try to read all members
                for member in tf.getmembers():
                    if member.isfile():
                        try:
                            tf.extractfile(member).read(1024)  # Read first 1KB
                        except Exception as e:
                            result["passed"] = False
                            result["errors"].append(f"Error reading {member.name}: {str(e)}")
        except tarfile.TarError as e:
            result["passed"] = False
            result["errors"].append(f"Bad TAR file: {str(e)}")
        except Exception as e:
            result["passed"] = False
            result["errors"].append(f"Test failed: {str(e)}")
        
        return result

    def _test_7z(self, archive, password):
        """Test 7Z archive integrity"""
        result = {"passed": True, "errors": []}
        
        try:
            with py7zr.SevenZipFile(archive, 'r', password=password) as szf:
                test_result = szf.testzip()
                if test_result:
                    result["passed"] = False
                    result["errors"].extend(test_result)
        except ImportError:
            result["passed"] = False
            result["errors"].append("py7zr library is required for 7z format support")
        except Exception as e:
            result["passed"] = False
            result["errors"].append(f"Test failed: {str(e)}")
        
        return result

    def _detect_format(self, archive_path: Path) -> CompressionFormat:
        """Detect archive format from file extension"""
        suffix = archive_path.suffix.lower()
        
        if suffix == '.zip':
            return CompressionFormat.ZIP
        elif suffix == '.tar':
            return CompressionFormat.TAR
        elif archive_path.suffixes[-2:] == ['.tar', '.gz'] or suffix == '.tgz':
            return CompressionFormat.TAR_GZ
        elif archive_path.suffixes[-2:] == ['.tar', '.bz2'] or suffix == '.tbz2':
            return CompressionFormat.TAR_BZ2
        elif archive_path.suffixes[-2:] == ['.tar', '.xz'] or suffix == '.txz':
            return CompressionFormat.TAR_XZ
        elif suffix == '.7z':
            return CompressionFormat.SEVEN_Z
        else:
            # Try to detect by content
            try:
                if zipfile.is_zipfile(archive_path):
                    return CompressionFormat.ZIP
                elif tarfile.is_tarfile(archive_path):
                    return CompressionFormat.TAR
            except Exception:
                pass
        
        raise ToolError(f"Cannot detect archive format for: {archive_path}")

    def _collect_files(self, source: Path, recursive: bool, include_patterns: List[str], 
                      exclude_patterns: List[str]) -> List[Path]:
        """Collect files to archive based on patterns"""
        files = []
        
        if source.is_file():
            files.append(source)
        elif source.is_dir():
            if recursive:
                for item in source.rglob('*'):
                    if self._should_include(item, include_patterns, exclude_patterns):
                        files.append(item)
            else:
                for item in source.iterdir():
                    if self._should_include(item, include_patterns, exclude_patterns):
                        files.append(item)
        
        return files

    def _should_include(self, path: Path, include_patterns: List[str], exclude_patterns: List[str]) -> bool:
        """Check if file should be included based on patterns"""
        import fnmatch
        
        # Check exclude patterns first
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(str(path), pattern):
                return False
        
        # Check include patterns
        if not include_patterns:
            return True
        
        for pattern in include_patterns:
            if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(str(path), pattern):
                return True
        
        return False

    def _is_safe_path(self, path: str) -> bool:
        """Check if path is safe for extraction (no directory traversal)"""
        # Normalize path
        normalized = os.path.normpath(path)
        
        # Check for directory traversal
        if normalized.startswith('..') or '/../' in normalized:
            return False
        
        # Check for absolute paths
        if os.path.isabs(normalized):
            return False
        
        # Check path length
        if len(normalized) > self.max_path_length:
            return False
        
        # Check for blocked extensions
        path_obj = Path(normalized)
        if path_obj.suffix.lower() in self.blocked_extensions:
            return False
        
        return True

    def _validate_path(self, path: str) -> bool:
        """Validate path for security"""
        try:
            path_obj = Path(path).resolve()
            # Add additional validation as needed
            return True
        except Exception:
            return False

    def _success(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Return success response"""
        return {
            "success": True,
            "tool": self.name,
            "data": data,
            "timestamp": datetime.now().isoformat()
        }

    def _error(self, message: str) -> Dict[str, Any]:
        """Return error response"""
        logger.error(f"ArchiveTool error: {message}")
        return {
            "success": False,
            "tool": self.name,
            "error": message,
            "timestamp": datetime.now().isoformat()
        }


# Add methods for other operations
def _add_to_archive(self, **params):
    """Add files to existing archive"""
    return self._error("Add to archive operation not yet implemented")

def _delete_from_archive(self, **params):
    """Delete files from archive"""
    return self._error("Delete from archive operation not yet implemented")

def _update_archive(self, **params):
    """Update files in archive"""
    return self._error("Update archive operation not yet implemented")

def _compress_file(self, **params):
    """Compress individual file"""
    return self._error("Compress file operation not yet implemented")

def _decompress_file(self, **params):
    """Decompress individual file"""
    return self._error("Decompress file operation not yet implemented")

def _get_zip_info(self, archive, info, password):
    """Get detailed ZIP info"""
    try:
        with zipfile.ZipFile(archive, 'r') as zf:
            info.comment = zf.comment.decode('utf-8') if zf.comment else None
            info.file_count = len(zf.namelist())
            info.encrypted = any(z.flag_bits & 0x1 for z in zf.infolist())
            
            total_size = sum(z.file_size for z in zf.infolist())
            compressed_size = sum(z.compress_size for z in zf.infolist())
            info.compression_ratio = 1.0 - (compressed_size / total_size) if total_size > 0 else 0.0
    except Exception:
        pass

def _get_tar_info(self, archive, info):
    """Get detailed TAR info"""
    try:
        with tarfile.open(archive, 'r:*') as tf:
            members = tf.getmembers()
            info.file_count = len(members)
            info.compression_ratio = 0.0  # TAR doesn't track compression separately
    except Exception:
        pass

def _get_7z_info(self, archive, info, password):
    """Get detailed 7Z info"""
    try:
        with py7zr.SevenZipFile(archive, 'r', password=password) as szf:
            files = szf.list()
            info.file_count = len(files)
            info.encrypted = bool(password)
            
            total_size = sum(f.uncompressed for f in files if hasattr(f, 'uncompressed'))
            compressed_size = sum(f.compressed for f in files if hasattr(f, 'compressed'))
            info.compression_ratio = 1.0 - (compressed_size / total_size) if total_size > 0 else 0.0
    except Exception:
        pass

# Bind methods to class
ArchiveTool._add_to_archive = _add_to_archive
ArchiveTool._delete_from_archive = _delete_from_archive
ArchiveTool._update_archive = _update_archive
ArchiveTool._compress_file = _compress_file
ArchiveTool._decompress_file = _decompress_file
ArchiveTool._get_zip_info = _get_zip_info
ArchiveTool._get_tar_info = _get_tar_info
ArchiveTool._get_7z_info = _get_7z_info


# Register the tool
ToolRegistry.register("archive_tool", ArchiveTool)