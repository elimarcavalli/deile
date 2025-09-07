"""
Lint/Format Tool for DEILE v4.0
===============================

Multi-language code quality tool with linting, formatting, and auto-fixing
capabilities with dry-run support for safe code modifications.

Author: DEILE
Version: 4.0
"""

import logging
import subprocess
import json
import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import shutil
from datetime import datetime

from .base import SyncTool, ToolContext, ToolResult, ToolStatus, DisplayPolicy
from ..core.exceptions import ToolError

logger = logging.getLogger(__name__)


@dataclass
class LintIssue:
    """Single linting issue"""
    file_path: str
    line: int
    column: int
    rule: str
    severity: str  # error, warning, info
    message: str
    fixable: bool
    category: str  # style, security, performance, error


@dataclass
class FormatResult:
    """Code formatting result"""
    file_path: str
    original_content: str
    formatted_content: str
    changes_made: bool
    diff_lines: List[str]


@dataclass
class LintResult:
    """Complete lint/format operation result"""
    files_processed: int
    total_issues: int
    issues_by_severity: Dict[str, int]
    fixable_issues: int
    issues: List[LintIssue]
    format_results: List[FormatResult]
    execution_time_ms: float


class LintFormatTool(SyncTool):
    """
    Multi-language code quality and formatting tool
    
    Supported Languages & Tools:
    - Python: flake8, pylint, black, autopep8, isort
    - JavaScript/TypeScript: eslint, prettier
    - Go: gofmt, golint, staticcheck
    - Rust: cargo fmt, cargo clippy
    - Java: checkstyle, google-java-format
    - C/C++: clang-format, cppcheck
    - HTML/CSS: prettier, htmlhint, csslint
    - JSON/YAML: prettier, yamllint
    """
    
    def __init__(self):
        super().__init__(
            name="lint_format",
            description="Multi-language code linting and formatting with auto-fix capabilities",
            category="code_quality",
            security_level="safe"
        )
        
        # Language configurations
        self.language_configs = {
            'python': {
                'extensions': ['.py'],
                'linters': ['flake8', 'pylint'],
                'formatters': ['black', 'autopep8'],
                'import_sorters': ['isort']
            },
            'javascript': {
                'extensions': ['.js', '.jsx', '.mjs'],
                'linters': ['eslint'],
                'formatters': ['prettier']
            },
            'typescript': {
                'extensions': ['.ts', '.tsx'],
                'linters': ['eslint', '@typescript-eslint/parser'],
                'formatters': ['prettier']
            },
            'go': {
                'extensions': ['.go'],
                'linters': ['golint', 'staticcheck'],
                'formatters': ['gofmt']
            },
            'rust': {
                'extensions': ['.rs'],
                'linters': ['cargo clippy'],
                'formatters': ['cargo fmt']
            },
            'java': {
                'extensions': ['.java'],
                'linters': ['checkstyle'],
                'formatters': ['google-java-format']
            },
            'cpp': {
                'extensions': ['.c', '.cpp', '.h', '.hpp'],
                'linters': ['cppcheck'],
                'formatters': ['clang-format']
            },
            'json': {
                'extensions': ['.json'],
                'formatters': ['prettier']
            },
            'yaml': {
                'extensions': ['.yml', '.yaml'],
                'linters': ['yamllint'],
                'formatters': ['prettier']
            }
        }

    def get_schema(self) -> Dict[str, Any]:
        """Get tool schema for function calling"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "path": {
                        "type": "STRING",
                        "description": "File or directory path to lint/format"
                    },
                    "languages": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Languages to process (python, javascript, typescript, etc.)"
                    },
                    "action": {
                        "type": "STRING",
                        "enum": ["lint", "format", "both"],
                        "description": "Action to perform (default: both)"
                    },
                    "auto_fix": {
                        "type": "BOOLEAN",
                        "description": "Auto-fix issues when possible (default: false)"
                    },
                    "dry_run": {
                        "type": "BOOLEAN", 
                        "description": "Show what would be changed without modifying files (default: true)"
                    },
                    "severity_filter": {
                        "type": "STRING",
                        "enum": ["error", "warning", "info", "all"],
                        "description": "Minimum severity level to report (default: warning)"
                    },
                    "exclude_patterns": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Patterns to exclude from processing"
                    },
                    "config_file": {
                        "type": "STRING",
                        "description": "Path to custom configuration file"
                    },
                    "show_cli": {
                        "type": "BOOLEAN",
                        "description": "Display results in terminal (default: true)"
                    }
                },
                "required": ["path"]
            }
        }

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Execute lint/format operations with dry-run support"""
        try:
            start_time = datetime.now()
            
            # Extract parameters
            path = context.get_parameter("path")
            languages = context.get_parameter("languages", [])
            action = context.get_parameter("action", "both")
            auto_fix = context.get_parameter("auto_fix", False)
            dry_run = context.get_parameter("dry_run", True)  # Safe default
            severity_filter = context.get_parameter("severity_filter", "warning")
            exclude_patterns = context.get_parameter("exclude_patterns", [])
            config_file = context.get_parameter("config_file")
            show_cli = context.get_parameter("show_cli", True)
            
            # Validate path
            target_path = Path(path).resolve()
            if not target_path.exists():
                raise ToolError(f"Path does not exist: {path}")
            
            # Find files to process
            files_to_process = self._find_files_to_process(
                target_path, languages, exclude_patterns
            )
            
            if not files_to_process:
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    data={"message": "No files found to process"},
                    message="No files found matching criteria",
                    display_policy=DisplayPolicy.SYSTEM,
                    show_cli=show_cli
                )
            
            # Process files
            lint_result = self._process_files(
                files_to_process, action, auto_fix, dry_run, 
                severity_filter, config_file
            )
            
            execution_time = (datetime.now() - start_time).total_seconds() * 1000
            lint_result.execution_time_ms = execution_time
            
            # Prepare result data
            result_data = asdict(lint_result)
            
            # Create display data
            display_data = self._prepare_display_data(lint_result, dry_run)
            
            # Determine success status
            has_errors = lint_result.issues_by_severity.get('error', 0) > 0
            status = ToolStatus.SUCCESS if not has_errors else ToolStatus.WARNING
            
            message = f"Processed {lint_result.files_processed} files"
            if lint_result.total_issues > 0:
                message += f", found {lint_result.total_issues} issues"
            if dry_run and lint_result.fixable_issues > 0:
                message += f" ({lint_result.fixable_issues} fixable)"
            
            return ToolResult(
                status=status,
                data=result_data,
                message=message,
                display_policy=DisplayPolicy.SYSTEM,
                show_cli=show_cli,
                display_data=display_data if show_cli else None,
                execution_time=execution_time / 1000
            )
            
        except Exception as e:
            logger.error(f"LintFormatTool error: {e}")
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Lint/format failed: {str(e)}",
                error=e,
                display_policy=DisplayPolicy.SYSTEM
            )

    def _find_files_to_process(self, target_path: Path, languages: List[str], 
                              exclude_patterns: List[str]) -> Dict[str, List[Path]]:
        """Find files grouped by language"""
        files_by_language = {}
        
        # Determine which languages to process
        if not languages:
            # Auto-detect from files
            languages = list(self.language_configs.keys())
        
        # Build extension to language mapping
        ext_to_lang = {}
        for lang in languages:
            if lang in self.language_configs:
                for ext in self.language_configs[lang]['extensions']:
                    ext_to_lang[ext] = lang
        
        # Find files
        if target_path.is_file():
            # Single file
            ext = target_path.suffix.lower()
            if ext in ext_to_lang:
                lang = ext_to_lang[ext]
                files_by_language[lang] = [target_path]
        else:
            # Directory traversal
            for file_path in target_path.rglob('*'):
                if file_path.is_file():
                    # Check exclude patterns
                    if any(file_path.match(pattern) for pattern in exclude_patterns):
                        continue
                    
                    # Check if file extension matches any language
                    ext = file_path.suffix.lower()
                    if ext in ext_to_lang:
                        lang = ext_to_lang[ext]
                        if lang not in files_by_language:
                            files_by_language[lang] = []
                        files_by_language[lang].append(file_path)
        
        return files_by_language

    def _process_files(self, files_by_language: Dict[str, List[Path]], 
                      action: str, auto_fix: bool, dry_run: bool,
                      severity_filter: str, config_file: Optional[str]) -> LintResult:
        """Process files for linting and formatting"""
        all_issues = []
        all_format_results = []
        total_files = 0
        
        for language, files in files_by_language.items():
            total_files += len(files)
            
            # Process linting
            if action in ['lint', 'both']:
                issues = self._run_linters(language, files, severity_filter, config_file)
                all_issues.extend(issues)
            
            # Process formatting
            if action in ['format', 'both']:
                format_results = self._run_formatters(
                    language, files, auto_fix, dry_run, config_file
                )
                all_format_results.extend(format_results)
        
        # Aggregate results
        issues_by_severity = {'error': 0, 'warning': 0, 'info': 0}
        fixable_issues = 0
        
        for issue in all_issues:
            issues_by_severity[issue.severity] += 1
            if issue.fixable:
                fixable_issues += 1
        
        return LintResult(
            files_processed=total_files,
            total_issues=len(all_issues),
            issues_by_severity=issues_by_severity,
            fixable_issues=fixable_issues,
            issues=all_issues,
            format_results=all_format_results,
            execution_time_ms=0.0  # Set by caller
        )

    def _run_linters(self, language: str, files: List[Path], 
                    severity_filter: str, config_file: Optional[str]) -> List[LintIssue]:
        """Run language-specific linters"""
        issues = []
        
        if language not in self.language_configs:
            return issues
        
        linters = self.language_configs[language].get('linters', [])
        
        for linter in linters:
            if self._is_tool_available(linter):
                try:
                    linter_issues = self._run_specific_linter(
                        linter, language, files, severity_filter, config_file
                    )
                    issues.extend(linter_issues)
                except Exception as e:
                    logger.warning(f"Error running {linter}: {e}")
        
        return issues

    def _run_formatters(self, language: str, files: List[Path],
                       auto_fix: bool, dry_run: bool, 
                       config_file: Optional[str]) -> List[FormatResult]:
        """Run language-specific formatters"""
        format_results = []
        
        if language not in self.language_configs:
            return format_results
        
        formatters = self.language_configs[language].get('formatters', [])
        
        for formatter in formatters:
            if self._is_tool_available(formatter):
                try:
                    formatter_results = self._run_specific_formatter(
                        formatter, language, files, auto_fix, dry_run, config_file
                    )
                    format_results.extend(formatter_results)
                except Exception as e:
                    logger.warning(f"Error running {formatter}: {e}")
        
        return format_results

    def _is_tool_available(self, tool: str) -> bool:
        """Check if a tool is available in the system"""
        try:
            # Handle special cases
            if tool in ['cargo fmt', 'cargo clippy']:
                result = subprocess.run(['cargo', '--version'], 
                                      capture_output=True, text=True, timeout=5)
                return result.returncode == 0
            
            # Standard tool check
            result = subprocess.run([tool, '--version'], 
                                  capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except:
            return False

    def _run_specific_linter(self, linter: str, language: str, files: List[Path],
                            severity_filter: str, config_file: Optional[str]) -> List[LintIssue]:
        """Run specific linter and parse results"""
        issues = []
        
        try:
            if linter == 'flake8':
                issues = self._run_flake8(files, config_file)
            elif linter == 'pylint':
                issues = self._run_pylint(files, config_file)
            elif linter == 'eslint':
                issues = self._run_eslint(files, config_file)
            # Add more linters as needed
            
        except Exception as e:
            logger.error(f"Error running {linter}: {e}")
        
        # Filter by severity
        if severity_filter != 'all':
            severity_order = {'info': 0, 'warning': 1, 'error': 2}
            min_severity = severity_order.get(severity_filter, 1)
            issues = [issue for issue in issues 
                     if severity_order.get(issue.severity, 0) >= min_severity]
        
        return issues

    def _run_flake8(self, files: List[Path], config_file: Optional[str]) -> List[LintIssue]:
        """Run flake8 Python linter"""
        issues = []
        
        cmd = ['flake8', '--format=json']
        if config_file:
            cmd.extend(['--config', config_file])
        cmd.extend([str(f) for f in files])
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.stdout:
                # Parse flake8 JSON output (if available) or custom parsing
                for line in result.stdout.strip().split('\n'):
                    if ':' in line:
                        parts = line.split(':', 4)
                        if len(parts) >= 4:
                            issues.append(LintIssue(
                                file_path=parts[0],
                                line=int(parts[1]) if parts[1].isdigit() else 0,
                                column=int(parts[2]) if parts[2].isdigit() else 0,
                                rule=parts[3].strip().split()[0],
                                severity='error' if 'E' in parts[3] else 'warning',
                                message=parts[4].strip() if len(parts) > 4 else parts[3],
                                fixable=False,  # flake8 doesn't auto-fix
                                category='style'
                            ))
        except subprocess.TimeoutExpired:
            logger.warning("flake8 timed out")
        except Exception as e:
            logger.error(f"Error running flake8: {e}")
        
        return issues

    def _run_specific_formatter(self, formatter: str, language: str, files: List[Path],
                               auto_fix: bool, dry_run: bool, 
                               config_file: Optional[str]) -> List[FormatResult]:
        """Run specific formatter"""
        format_results = []
        
        try:
            if formatter == 'black':
                format_results = self._run_black(files, auto_fix, dry_run, config_file)
            elif formatter == 'prettier':
                format_results = self._run_prettier(files, auto_fix, dry_run, config_file)
            # Add more formatters as needed
            
        except Exception as e:
            logger.error(f"Error running {formatter}: {e}")
        
        return format_results

    def _run_black(self, files: List[Path], auto_fix: bool, dry_run: bool, 
                  config_file: Optional[str]) -> List[FormatResult]:
        """Run Black Python formatter"""
        format_results = []
        
        for file_path in files:
            try:
                # Read original content
                with open(file_path, 'r', encoding='utf-8') as f:
                    original_content = f.read()
                
                # Run black
                cmd = ['black']
                if dry_run or not auto_fix:
                    cmd.append('--diff')
                if config_file:
                    cmd.extend(['--config', config_file])
                cmd.append(str(file_path))
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                
                if dry_run or not auto_fix:
                    # Parse diff output
                    diff_lines = result.stdout.split('\n') if result.stdout else []
                    changes_made = bool(result.stdout and result.stdout.strip())
                    
                    format_results.append(FormatResult(
                        file_path=str(file_path),
                        original_content=original_content,
                        formatted_content="",  # Not available in diff mode
                        changes_made=changes_made,
                        diff_lines=diff_lines
                    ))
                else:
                    # Read formatted content
                    with open(file_path, 'r', encoding='utf-8') as f:
                        formatted_content = f.read()
                    
                    format_results.append(FormatResult(
                        file_path=str(file_path),
                        original_content=original_content,
                        formatted_content=formatted_content,
                        changes_made=original_content != formatted_content,
                        diff_lines=[]
                    ))
                    
            except Exception as e:
                logger.error(f"Error formatting {file_path} with black: {e}")
        
        return format_results

    def _prepare_display_data(self, lint_result: LintResult, dry_run: bool) -> Dict[str, Any]:
        """Prepare data for CLI display"""
        return {
            "type": "lint_format_results",
            "summary": {
                "files_processed": lint_result.files_processed,
                "total_issues": lint_result.total_issues,
                "issues_by_severity": lint_result.issues_by_severity,
                "fixable_issues": lint_result.fixable_issues,
                "execution_time_ms": lint_result.execution_time_ms,
                "dry_run": dry_run
            },
            "top_issues": [
                {
                    "file": issue.file_path,
                    "line": issue.line,
                    "rule": issue.rule,
                    "severity": issue.severity,
                    "message": issue.message[:100] + "..." if len(issue.message) > 100 else issue.message,
                    "fixable": issue.fixable
                }
                for issue in sorted(lint_result.issues, 
                                  key=lambda x: {'error': 3, 'warning': 2, 'info': 1}.get(x.severity, 0),
                                  reverse=True)[:10]
            ],
            "format_changes": len([r for r in lint_result.format_results if r.changes_made])
        }


# Register the tool
from deile.tools.registry import ToolRegistry
ToolRegistry.register("lint_format", LintFormatTool)