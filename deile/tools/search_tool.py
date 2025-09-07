"""Search Tool - find_in_files implementation with context limits"""

from typing import List, Dict, Any, Optional
from pathlib import Path
import re
import fnmatch
import time
from dataclasses import dataclass
import logging

from .base import SyncTool, ToolContext, ToolResult, ToolStatus, DisplayPolicy
from ..core.exceptions import ToolError


logger = logging.getLogger(__name__)


@dataclass
class SearchMatch:
    """Single search match with context"""
    file: str
    line_number: int
    match_text: str
    context_before: List[str]
    context_after: List[str]
    match_score: float = 1.0


class FindInFilesTool(SyncTool):
    """Search for text patterns in files with context-limited results"""
    
    def __init__(self):
        super().__init__(
            name="find_in_files",
            description="Search for text patterns in files with context-limited results. Designed for efficient repository searching with token optimization.",
            category="search",
            security_level="safe"
        )
        
        # Default exclude patterns
        self.default_excludes = [
            '.git', '.svn', '.hg',
            'node_modules', '__pycache__', '.pytest_cache',
            'venv', '.venv', 'env', '.env',
            '.idea', '.vscode',
            '*.pyc', '*.pyo', '*.pyd',
            '*.class', '*.jar', '*.war',
            '*.exe', '*.dll', '*.so', '*.dylib',
            '*.jpg', '*.jpeg', '*.png', '*.gif', '*.bmp',
            '*.mp3', '*.mp4', '*.avi', '*.mkv',
            '*.zip', '*.rar', '*.7z', '*.tar', '*.gz'
        ]
    
    def get_schema(self) -> Dict[str, Any]:
        """Get tool schema for function calling"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Search pattern (regex supported)"
                    },
                    "path": {
                        "type": "STRING", 
                        "description": "Directory or file path to search. Default: current directory"
                    },
                    "file_pattern": {
                        "type": "STRING",
                        "description": "File pattern filter (glob). Example: '*.py'"
                    },
                    "max_context_lines": {
                        "type": "NUMBER",
                        "description": "Maximum context lines per match. Default: 25"
                    },
                    "max_matches": {
                        "type": "NUMBER",
                        "description": "Maximum number of matches to return. Default: 20"
                    },
                    "case_sensitive": {
                        "type": "BOOLEAN",
                        "description": "Case sensitive search. Default: false"
                    },
                    "include_binary": {
                        "type": "BOOLEAN", 
                        "description": "Include binary files in search. Default: false"
                    },
                    "exclude_dirs": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Additional directories to exclude"
                    },
                    "show_cli": {
                        "type": "BOOLEAN",
                        "description": "Display results in terminal. Default: true"
                    }
                },
                "required": ["query"]
            }
        }
    
    def _is_binary_file(self, file_path: Path) -> bool:
        """Check if file is binary"""
        try:
            with open(file_path, 'rb') as f:
                chunk = f.read(512)
                return b'\0' in chunk
        except:
            return True
    
    def _should_exclude_path(self, path: Path, exclude_patterns: List[str]) -> bool:
        """Check if path should be excluded"""
        path_str = str(path).replace('\\', '/')
        
        for pattern in exclude_patterns:
            # Directory patterns
            if pattern in path_str.split('/'):
                return True
            # Glob patterns
            if fnmatch.fnmatch(path.name, pattern):
                return True
            if fnmatch.fnmatch(path_str, pattern):
                return True
                
        return False
    
    def _find_files(self, 
                   search_path: Path, 
                   file_pattern: Optional[str],
                   exclude_patterns: List[str],
                   include_binary: bool) -> List[Path]:
        """Find files to search in"""
        files = []
        
        if search_path.is_file():
            if not self._should_exclude_path(search_path, exclude_patterns):
                if include_binary or not self._is_binary_file(search_path):
                    files.append(search_path)
        else:
            # Recursively find files
            try:
                for file_path in search_path.rglob('*'):
                    if file_path.is_file():
                        # Check exclusions
                        if self._should_exclude_path(file_path, exclude_patterns):
                            continue
                            
                        # Check file pattern
                        if file_pattern and not fnmatch.fnmatch(file_path.name, file_pattern):
                            continue
                            
                        # Check binary
                        if not include_binary and self._is_binary_file(file_path):
                            continue
                            
                        files.append(file_path)
                        
            except PermissionError as e:
                logger.warning(f"Permission denied accessing {search_path}: {e}")
                
        return files
    
    def _search_in_file(self, 
                       file_path: Path, 
                       pattern: re.Pattern,
                       max_context_lines: int) -> List[SearchMatch]:
        """Search for pattern in a single file"""
        matches = []
        
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                
            for line_num, line in enumerate(lines, 1):
                if pattern.search(line):
                    # Calculate context boundaries
                    context_start = max(0, line_num - 1 - max_context_lines // 2)
                    context_end = min(len(lines), line_num + max_context_lines // 2)
                    
                    # Extract context
                    context_before = []
                    context_after = []
                    
                    for i in range(context_start, line_num - 1):
                        context_before.append(lines[i].rstrip())
                        
                    for i in range(line_num, context_end):
                        context_after.append(lines[i].rstrip())
                    
                    match = SearchMatch(
                        file=str(file_path),
                        line_number=line_num,
                        match_text=line.rstrip(),
                        context_before=context_before,
                        context_after=context_after,
                        match_score=1.0
                    )
                    
                    matches.append(match)
                    
        except Exception as e:
            logger.warning(f"Error searching in file {file_path}: {e}")
            
        return matches
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Execute search with context limits"""
        try:
            start_time = time.time()
            
            # Extract parameters
            query = context.get_parameter("query")
            path = context.get_parameter("path", ".")
            file_pattern = context.get_parameter("file_pattern")
            max_context_lines = min(context.get_parameter("max_context_lines", 25), 50)  # SITUAÇÃO 6: Max 50 lines
            max_matches = context.get_parameter("max_matches", 20)
            case_sensitive = context.get_parameter("case_sensitive", False)
            include_binary = context.get_parameter("include_binary", False)
            exclude_dirs = context.get_parameter("exclude_dirs", [])
            show_cli = context.get_parameter("show_cli", True)
            
            # Validate query
            if not query or not query.strip():
                raise ToolError("Search query cannot be empty")
            
            # Prepare regex pattern
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                pattern = re.compile(query, flags)
            except re.error as e:
                raise ToolError(f"Invalid regex pattern: {e}")
            
            # Prepare search path
            search_path = Path(path).resolve()
            if not search_path.exists():
                raise ToolError(f"Search path does not exist: {path}")
            
            # Combine exclude patterns
            all_excludes = self.default_excludes + exclude_dirs
            
            # Find files to search
            files = self._find_files(search_path, file_pattern, all_excludes, include_binary)
            
            if not files:
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    data={
                        "matches": [],
                        "total_files_searched": 0,
                        "total_matches": 0,
                        "search_time": time.time() - start_time,
                        "truncated": False
                    },
                    message="No files found matching criteria",
                    display_policy=DisplayPolicy.SYSTEM,
                    show_cli=show_cli
                )
            
            # Search in files
            all_matches = []
            files_searched = 0
            
            for file_path in files:
                if len(all_matches) >= max_matches:
                    break
                    
                matches = self._search_in_file(file_path, pattern, max_context_lines)
                all_matches.extend(matches[:max_matches - len(all_matches)])
                files_searched += 1
            
            # Prepare results
            result_data = {
                "matches": [
                    {
                        "file": match.file,
                        "line_number": match.line_number,
                        "match_text": match.match_text,
                        "context_before": match.context_before,
                        "context_after": match.context_after,
                        "match_score": match.match_score
                    }
                    for match in all_matches
                ],
                "total_files_searched": files_searched,
                "total_matches": len(all_matches),
                "search_time": time.time() - start_time,
                "truncated": len(all_matches) >= max_matches
            }
            
            # Format display data for UI
            display_data = {
                "summary": f"Found {len(all_matches)} matches in {files_searched} files",
                "query": query,
                "search_path": str(search_path),
                "context_lines": max_context_lines,
                "truncated": result_data["truncated"]
            }
            
            message = f"Search completed: {len(all_matches)} matches found in {files_searched} files"
            if result_data["truncated"]:
                message += f" (limited to {max_matches} matches)"
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=result_data,
                message=message,
                display_policy=DisplayPolicy.SYSTEM,
                show_cli=show_cli,
                display_data=display_data,
                execution_time=time.time() - start_time
            )
            
        except Exception as e:
            logger.error(f"Search tool error: {e}")
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Search failed: {str(e)}",
                error=e,
                display_policy=DisplayPolicy.SYSTEM
            )