"""
Search Tool for DEILE v4.0 - SITUAÇÃO 6 COMPLIANT
==================================================

High-performance repository search tool with strict context limits
for optimal token usage (≤ 50 lines total per match).

Author: DEILE
Version: 4.0
Features: Context-limited search, performance optimization, smart filtering
"""

import logging
import re
import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import mimetypes
import fnmatch
from datetime import datetime
import time

from .base import SyncTool, ToolContext, ToolResult, ToolStatus, DisplayPolicy
from ..core.exceptions import ToolError

logger = logging.getLogger(__name__)


@dataclass
class SearchMatch:
    """Single search match with context - SITUAÇÃO 6 compliant"""
    file_path: str
    line_number: int
    line_content: str
    match_text: str
    context_lines: List[str]  # Combined context (before + match + after) ≤ 50 lines total
    match_score: float
    snippet_start_line: int
    snippet_end_line: int


@dataclass 
class SearchResult:
    """Search operation result with performance metadata"""
    query: str
    matches: List[SearchMatch]
    total_matches: int
    files_searched: int
    search_time_ms: float
    context_limited: bool
    performance_notes: List[str]


class SearchTool(SyncTool):
    """
    Repository search tool with strict SITUAÇÃO 6 compliance
    
    Key Features:
    - Context limited to ≤ 50 lines total per match
    - High-performance parallel search
    - Smart binary file detection
    - Token-optimized output format
    - Repository-aware exclusions
    """
    
    def __init__(self):
        super().__init__(
            name="find_in_files",
            description="Search for text patterns in repository files with context limits (SITUAÇÃO 6 compliant - max 50 lines per match)",
            category="search",
            security_level="safe"
        )
        
        # Enhanced exclude patterns for better performance
        self.default_excludes = [
            '.git/*', '.svn/*', '.hg/*', '.bzr/*',
            'node_modules/*', '__pycache__/*', '.pytest_cache/*',
            'venv/*', '.venv/*', 'env/*', '.env/*',
            '.idea/*', '.vscode/*', '.vs/*',
            'build/*', 'dist/*', 'target/*', 'bin/*', 'obj/*',
            '*.pyc', '*.pyo', '*.pyd', '*.class',
            '*.exe', '*.dll', '*.so', '*.dylib',
            '*.jpg', '*.jpeg', '*.png', '*.gif', '*.bmp', '*.svg', '*.ico',
            '*.mp3', '*.mp4', '*.avi', '*.mkv', '*.mov', '*.wmv',
            '*.zip', '*.rar', '*.7z', '*.tar', '*.gz', '*.bz2', '*.xz',
            '*.pdf', '*.doc', '*.docx', '*.xls', '*.xlsx', '*.ppt', '*.pptx',
            '*.min.js', '*.min.css', '*.bundle.*', 
            '*.log', '*.tmp', '*.temp', '*.cache'
        ]
        
        # Text file extensions for safe searching
        self.text_extensions = {
            '.py', '.js', '.ts', '.jsx', '.tsx', '.vue', '.svelte',
            '.java', '.c', '.cpp', '.h', '.hpp', '.cs', '.php', '.rb',
            '.go', '.rs', '.swift', '.kt', '.scala', '.clj', '.hs',
            '.html', '.htm', '.css', '.scss', '.sass', '.less',
            '.xml', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg',
            '.md', '.txt', '.rst', '.adoc', '.tex',
            '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd',
            '.sql', '.graphql', '.proto', '.dockerfile', '.makefile',
            '.r', '.R', '.m', '.pl', '.lua', '.vim'
        }
    
    def get_schema(self) -> Dict[str, Any]:
        """Get tool schema for function calling - SITUAÇÃO 6 compliant"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Search pattern or regex to find in files"
                    },
                    "path": {
                        "type": "STRING", 
                        "description": "Directory or file path to search (default: current directory)"
                    },
                    "file_patterns": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "File patterns to include (e.g., ['*.py', '*.js'])"
                    },
                    "max_context_lines": {
                        "type": "NUMBER",
                        "description": "Maximum total context lines per match (HARD LIMIT: 50)",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 25
                    },
                    "max_matches": {
                        "type": "NUMBER",
                        "description": "Maximum matches to return (default: 20)",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 20
                    },
                    "case_sensitive": {
                        "type": "BOOLEAN",
                        "description": "Case sensitive search (default: false)"
                    },
                    "regex_mode": {
                        "type": "BOOLEAN",
                        "description": "Enable regex pattern matching (default: false)"
                    },
                    "exclude_patterns": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Additional patterns to exclude"
                    },
                    "show_cli": {
                        "type": "BOOLEAN",
                        "description": "Display results in terminal (default: true)"
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
        """Execute search with SITUAÇÃO 6 compliance - max 50 lines per match"""
        try:
            start_time = time.time()
            
            # Extract and validate parameters
            query = context.get_parameter("query", "")
            if not query.strip():
                raise ToolError("Search query cannot be empty")
                
            path = context.get_parameter("path", ".")
            file_patterns = context.get_parameter("file_patterns", [])
            max_context_lines = min(context.get_parameter("max_context_lines", 25), 50)  # SITUAÇÃO 6: HARD LIMIT
            max_matches = min(context.get_parameter("max_matches", 20), 50)
            case_sensitive = context.get_parameter("case_sensitive", False)
            regex_mode = context.get_parameter("regex_mode", False)
            exclude_patterns = context.get_parameter("exclude_patterns", [])
            show_cli = context.get_parameter("show_cli", True)
            
            # Prepare search pattern
            if regex_mode:
                try:
                    flags = 0 if case_sensitive else re.IGNORECASE
                    pattern = re.compile(query, flags)
                except re.error as e:
                    raise ToolError(f"Invalid regex pattern: {e}")
            else:
                escaped_query = re.escape(query)
                flags = 0 if case_sensitive else re.IGNORECASE
                pattern = re.compile(escaped_query, flags)
            
            # Validate and prepare search path
            search_path = Path(path).resolve()
            if not search_path.exists():
                raise ToolError(f"Search path does not exist: {path}")
            
            # Perform search
            search_result = self._perform_search(
                pattern, search_path, file_patterns, 
                exclude_patterns + self.default_excludes,
                max_context_lines, max_matches
            )
            
            search_time = time.time() - start_time
            search_result.search_time_ms = search_time * 1000
            
            # Prepare result data
            result_data = {
                "query": search_result.query,
                "matches": [asdict(match) for match in search_result.matches],
                "total_files_searched": search_result.files_searched,
                "total_matches": search_result.total_matches,
                "search_time_ms": search_result.search_time_ms,
                "context_limited": search_result.context_limited,
                "performance_notes": search_result.performance_notes
            }
            
            # Create display data for CLI
            display_data = {
                "type": "search_results",
                "summary": f"Found {len(search_result.matches)} matches in {search_result.files_searched} files",
                "query": query,
                "search_path": str(search_path),
                "max_context_lines": max_context_lines,
                "matches": [
                    {
                        "file": match.file_path,
                        "line": match.line_number,
                        "content": match.line_content[:100] + "..." if len(match.line_content) > 100 else match.line_content,
                        "context_lines": len(match.context_lines)
                    }
                    for match in search_result.matches
                ]
            }
            
            message = f"Search completed: {len(search_result.matches)} matches in {search_result.files_searched} files"
            if search_result.context_limited:
                message += " (context limited to 50 lines per match)"
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=result_data,
                message=message,
                display_policy=DisplayPolicy.SYSTEM,
                show_cli=show_cli,
                display_data=display_data if show_cli else None,
                execution_time=search_time
            )
            
        except Exception as e:
            logger.error(f"SearchTool error: {e}")
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Search failed: {str(e)}",
                error=e,
                display_policy=DisplayPolicy.SYSTEM
            )

    def _perform_search(self, pattern: re.Pattern, search_path: Path, 
                       file_patterns: List[str], exclude_patterns: List[str],
                       max_context_lines: int, max_matches: int) -> SearchResult:
        """Perform the actual search with performance optimization"""
        
        # Find searchable files
        files = self._find_searchable_files(search_path, file_patterns, exclude_patterns)
        
        if not files:
            return SearchResult(
                query=pattern.pattern,
                matches=[],
                total_matches=0,
                files_searched=0,
                search_time_ms=0.0,
                context_limited=False,
                performance_notes=["No files found matching criteria"]
            )
        
        # Search files in parallel
        matches = []
        files_searched = 0
        context_limited = False
        
        # Use ThreadPoolExecutor for parallel search
        with ThreadPoolExecutor(max_workers=min(8, len(files))) as executor:
            future_to_file = {
                executor.submit(self._search_file_optimized, file_path, pattern, max_context_lines): file_path 
                for file_path in files[:1000]  # Limit files for performance
            }
            
            for future in as_completed(future_to_file):
                if len(matches) >= max_matches:
                    break
                    
                file_path = future_to_file[future]
                files_searched += 1
                
                try:
                    file_matches, file_context_limited = future.result()
                    matches.extend(file_matches)
                    if file_context_limited:
                        context_limited = True
                except Exception as e:
                    logger.debug(f"Error searching file {file_path}: {e}")
        
        # Sort by relevance and limit results
        matches.sort(key=lambda m: m.match_score, reverse=True)
        total_matches = len(matches)
        matches = matches[:max_matches]
        
        performance_notes = []
        if len(files) > 1000:
            performance_notes.append(f"Limited search to first 1000 files (found {len(files)} total)")
        if total_matches > max_matches:
            performance_notes.append(f"Limited results to {max_matches} matches (found {total_matches} total)")
        
        return SearchResult(
            query=pattern.pattern,
            matches=matches,
            total_matches=total_matches,
            files_searched=files_searched,
            search_time_ms=0.0,  # Set by caller
            context_limited=context_limited,
            performance_notes=performance_notes
        )

    def _find_searchable_files(self, search_path: Path, file_patterns: List[str], exclude_patterns: List[str]) -> List[Path]:
        """Find searchable files with smart filtering"""
        files = []
        
        try:
            for root, dirs, filenames in os.walk(search_path):
                # Filter out excluded directories
                dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, pattern) for pattern in exclude_patterns)]
                
                for filename in filenames:
                    file_path = Path(root) / filename
                    
                    # Skip if matches exclude patterns
                    if any(fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(str(file_path), pattern) 
                           for pattern in exclude_patterns):
                        continue
                    
                    # Check file patterns if specified
                    if file_patterns and not any(fnmatch.fnmatch(filename, pattern) for pattern in file_patterns):
                        continue
                    
                    # Check if it's a text file
                    if not self._is_text_file(file_path):
                        continue
                    
                    # Skip very large files
                    try:
                        if file_path.stat().st_size > 5 * 1024 * 1024:  # 5MB limit
                            continue
                    except OSError:
                        continue
                    
                    files.append(file_path)
                    
        except (OSError, PermissionError) as e:
            logger.warning(f"Error walking directory {search_path}: {e}")
        
        return files

    def _is_text_file(self, file_path: Path) -> bool:
        """Enhanced text file detection"""
        # Check by extension first
        if file_path.suffix.lower() in self.text_extensions:
            return True
        
        # Check MIME type
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if mime_type and mime_type.startswith('text/'):
            return True
        
        # For files without extension, sample content
        if not file_path.suffix:
            try:
                with open(file_path, 'rb') as f:
                    sample = f.read(512)
                    if sample and b'\x00' not in sample:
                        return True
            except (OSError, IOError):
                pass
        
        return False

    def _search_file_optimized(self, file_path: Path, pattern: re.Pattern, max_context_lines: int) -> Tuple[List[SearchMatch], bool]:
        """Search single file with SITUAÇÃO 6 compliance"""
        matches = []
        context_limited = False
        
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            
            for line_num, line in enumerate(lines, 1):
                match_obj = pattern.search(line)
                if match_obj:
                    # SITUAÇÃO 6: Limit context to ≤ 50 lines total
                    context_before_count = min(max_context_lines // 2, line_num - 1)
                    context_after_count = min(max_context_lines - context_before_count, len(lines) - line_num)
                    
                    if context_before_count + 1 + context_after_count > 50:
                        context_limited = True
                        # Rebalance to stay within 50 line limit
                        total_context = 49  # Leave 1 line for the match itself
                        context_before_count = min(context_before_count, total_context // 2)
                        context_after_count = min(context_after_count, total_context - context_before_count)
                    
                    # Extract context lines
                    context_lines = []
                    
                    # Add before context
                    start_idx = max(0, line_num - 1 - context_before_count)
                    for i in range(start_idx, line_num - 1):
                        context_lines.append(f"{i+1:4d}: {lines[i].rstrip()}")
                    
                    # Add match line
                    context_lines.append(f"{line_num:4d}: {line.rstrip()}")
                    
                    # Add after context
                    end_idx = min(len(lines), line_num + context_after_count)
                    for i in range(line_num, end_idx):
                        context_lines.append(f"{i+1:4d}: {lines[i].rstrip()}")
                    
                    search_match = SearchMatch(
                        file_path=str(file_path.relative_to(Path.cwd())),
                        line_number=line_num,
                        line_content=line.rstrip(),
                        match_text=match_obj.group(0),
                        context_lines=context_lines,
                        match_score=self._calculate_match_score(line, match_obj, file_path),
                        snippet_start_line=start_idx + 1,
                        snippet_end_line=end_idx
                    )
                    
                    matches.append(search_match)
                    
                    # Limit matches per file
                    if len(matches) >= 5:
                        break
                        
        except (OSError, IOError, UnicodeDecodeError) as e:
            logger.debug(f"Error reading file {file_path}: {e}")
        
        return matches, context_limited

    def _calculate_match_score(self, line: str, match_obj: re.Match, file_path: Path) -> float:
        """Calculate match relevance score"""
        score = 1.0
        
        # Boost for exact word matches
        if match_obj.group(0).strip() and (
            (match_obj.start() == 0 or not line[match_obj.start()-1].isalnum()) and
            (match_obj.end() == len(line) or not line[match_obj.end()].isalnum())
        ):
            score += 0.5
        
        # Boost for important file types
        if file_path.suffix in {'.py', '.js', '.ts', '.java', '.c', '.cpp', '.go', '.rs'}:
            score += 0.3
        
        # Penalize very long lines (likely minified)
        if len(line) > 150:
            score -= 0.2
        
        return max(0.1, score)