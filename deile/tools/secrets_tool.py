"""
Secrets Detection Tool for DEILE v4.0
=====================================

Advanced secrets scanning and redaction tool with support for multiple
secret types, custom patterns, and safe handling of sensitive data.

Author: DEILE
Version: 4.0
"""

import logging
import re
import json
import hashlib
import base64
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set, Pattern
from dataclasses import dataclass, asdict
from datetime import datetime
import fnmatch

from .base import SyncTool, ToolContext, ToolResult, ToolStatus, DisplayPolicy
from ..core.exceptions import ToolError

logger = logging.getLogger(__name__)


@dataclass
class SecretDetection:
    """A detected secret with metadata"""
    file_path: str
    line_number: int
    column_start: int
    column_end: int
    secret_type: str
    confidence: float  # 0.0 to 1.0
    matched_text: str  # Partially masked for logging
    context_before: str
    context_after: str
    rule_id: str
    severity: str  # critical, high, medium, low


@dataclass
class RedactionResult:
    """Result of text redaction operation"""
    original_text: str
    redacted_text: str
    secrets_found: int
    redaction_map: Dict[str, str]  # original -> redacted mapping


@dataclass
class ScanResult:
    """Complete secrets scan result"""
    files_scanned: int
    secrets_found: int
    secrets_by_type: Dict[str, int]
    secrets_by_severity: Dict[str, int]
    detections: List[SecretDetection]
    scan_time_ms: float
    false_positive_rate: float


class SecretsTool(SyncTool):
    """
    Advanced secrets detection and redaction tool
    
    Features:
    - Multi-pattern secret detection (API keys, tokens, passwords, etc.)
    - Entropy-based detection for unknown secrets
    - Context-aware false positive reduction
    - Safe redaction with reversible mapping
    - File and text scanning capabilities
    - Custom pattern support
    """
    
    def __init__(self):
        super().__init__(
            name="secrets_scanner",
            description="Scan for secrets, API keys, tokens, and sensitive data with redaction capabilities",
            category="security",
            security_level="safe"
        )
        
        # Initialize secret patterns
        self._init_secret_patterns()
        
        # Common false positive patterns
        self.false_positive_patterns = [
            r'example\.com',
            r'localhost',
            r'127\.0\.0\.1',
            r'test[_-]?key',
            r'fake[_-]?token',
            r'dummy[_-]?secret',
            r'placeholder',
            r'REDACTED',
            r'\[FILTERED\]',
            r'<redacted>',
        ]
        
        # File patterns to exclude
        self.exclude_patterns = [
            '*.log', '*.tmp', '*.cache',
            '*.min.js', '*.bundle.js',
            '*.pyc', '*.pyo', '__pycache__/*',
            '.git/*', '.svn/*',
            'node_modules/*', 'venv/*', '.venv/*',
            '*.pdf', '*.doc', '*.docx',
            '*.jpg', '*.jpeg', '*.png', '*.gif'
        ]

    def _init_secret_patterns(self):
        """Initialize secret detection patterns"""
        self.secret_patterns = {
            # API Keys and Tokens
            'aws_access_key': {
                'pattern': r'(?i)aws[_-]?access[_-]?key[_-]?id["\'\s]*[:=]["\'\s]*([A-Z0-9]{20})',
                'confidence': 0.9,
                'severity': 'critical'
            },
            'aws_secret_key': {
                'pattern': r'(?i)aws[_-]?secret[_-]?access[_-]?key["\'\s]*[:=]["\'\s]*([A-Za-z0-9/+=]{40})',
                'confidence': 0.9,
                'severity': 'critical'
            },
            'github_token': {
                'pattern': r'(?i)github[_-]?token["\'\s]*[:=]["\'\s]*([A-Za-z0-9_]{40})',
                'confidence': 0.8,
                'severity': 'high'
            },
            'google_api_key': {
                'pattern': r'(?i)google[_-]?api[_-]?key["\'\s]*[:=]["\'\s]*([A-Za-z0-9_-]{39})',
                'confidence': 0.8,
                'severity': 'high'
            },
            'slack_token': {
                'pattern': r'xox[baprs]-[0-9]{10,12}-[0-9]{10,12}-[A-Za-z0-9]{24,32}',
                'confidence': 0.9,
                'severity': 'high'
            },
            'discord_token': {
                'pattern': r'[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27}',
                'confidence': 0.8,
                'severity': 'medium'
            },
            
            # Database Connections
            'postgres_url': {
                'pattern': r'postgres://[^:]+:[^@]+@[^/]+/\w+',
                'confidence': 0.9,
                'severity': 'critical'
            },
            'mysql_url': {
                'pattern': r'mysql://[^:]+:[^@]+@[^/]+/\w+',
                'confidence': 0.9,
                'severity': 'critical'
            },
            'mongodb_url': {
                'pattern': r'mongodb://[^:]+:[^@]+@[^/]+/\w+',
                'confidence': 0.9,
                'severity': 'critical'
            },
            
            # Generic Patterns
            'private_key': {
                'pattern': r'-----BEGIN [A-Z ]+ PRIVATE KEY-----',
                'confidence': 0.95,
                'severity': 'critical'
            },
            'jwt_token': {
                'pattern': r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',
                'confidence': 0.7,
                'severity': 'medium'
            },
            'base64_secret': {
                'pattern': r'(?i)(secret|password|token|key)["\'\s]*[:=]["\'\s]*([A-Za-z0-9+/]{20,}={0,2})',
                'confidence': 0.6,
                'severity': 'medium'
            },
            
            # High Entropy Strings (potential secrets)
            'high_entropy_hex': {
                'pattern': r'(?i)(secret|password|token|key|api)["\'\s]*[:=]["\'\s]*([a-f0-9]{32,})',
                'confidence': 0.5,
                'severity': 'low'
            },
            'high_entropy_base64': {
                'pattern': r'(?i)(secret|password|token|key|api)["\'\s]*[:=]["\'\s]*([A-Za-z0-9+/]{16,}={0,2})',
                'confidence': 0.4,
                'severity': 'low'
            }
        }
        
        # Compile patterns for performance
        self.compiled_patterns = {}
        for secret_type, config in self.secret_patterns.items():
            try:
                self.compiled_patterns[secret_type] = {
                    'pattern': re.compile(config['pattern']),
                    'confidence': config['confidence'],
                    'severity': config['severity']
                }
            except re.error as e:
                logger.warning(f"Invalid regex pattern for {secret_type}: {e}")

    def get_schema(self) -> Dict[str, Any]:
        """Get tool schema for function calling"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "action": {
                        "type": "STRING",
                        "enum": ["scan", "redact"],
                        "description": "Action to perform: scan for secrets or redact text"
                    },
                    "target": {
                        "type": "STRING",
                        "description": "File path, directory path, or text to scan/redact"
                    },
                    "text": {
                        "type": "STRING",
                        "description": "Text to scan/redact (alternative to target)"
                    },
                    "file_patterns": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "File patterns to include (e.g., ['*.py', '*.js'])"
                    },
                    "secret_types": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Specific secret types to look for"
                    },
                    "min_confidence": {
                        "type": "NUMBER",
                        "description": "Minimum confidence threshold (0.0-1.0, default: 0.5)",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 0.5
                    },
                    "include_low_confidence": {
                        "type": "BOOLEAN",
                        "description": "Include low confidence detections (default: false)"
                    },
                    "custom_patterns": {
                        "type": "OBJECT",
                        "description": "Custom regex patterns to search for",
                        "additionalProperties": {"type": "STRING"}
                    },
                    "redaction_char": {
                        "type": "STRING",
                        "description": "Character to use for redaction (default: *)",
                        "default": "*"
                    },
                    "preserve_length": {
                        "type": "BOOLEAN",
                        "description": "Preserve original text length when redacting (default: true)"
                    },
                    "show_cli": {
                        "type": "BOOLEAN",
                        "description": "Display results in terminal (default: true)"
                    }
                },
                "required": ["action"]
            }
        }

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Execute secrets scanning or redaction"""
        try:
            start_time = datetime.now()
            
            # Extract parameters
            action = context.get_parameter("action")
            target = context.get_parameter("target")
            text = context.get_parameter("text")
            file_patterns = context.get_parameter("file_patterns", [])
            secret_types = context.get_parameter("secret_types", [])
            min_confidence = context.get_parameter("min_confidence", 0.5)
            include_low_confidence = context.get_parameter("include_low_confidence", False)
            custom_patterns = context.get_parameter("custom_patterns", {})
            redaction_char = context.get_parameter("redaction_char", "*")
            preserve_length = context.get_parameter("preserve_length", True)
            show_cli = context.get_parameter("show_cli", True)
            
            # Validate parameters
            if not target and not text:
                raise ToolError("Either 'target' (file/directory) or 'text' must be provided")
            
            # Add custom patterns
            if custom_patterns:
                self._add_custom_patterns(custom_patterns)
            
            # Execute based on action
            if action == "scan":
                result = self._perform_scan(
                    target, text, file_patterns, secret_types, 
                    min_confidence, include_low_confidence
                )
                result_data = asdict(result)
                display_data = self._prepare_scan_display_data(result)
                
            elif action == "redact":
                if text:
                    result = self._redact_text(text, secret_types, min_confidence, 
                                             redaction_char, preserve_length)
                else:
                    result = self._redact_files(target, file_patterns, secret_types,
                                              min_confidence, redaction_char, preserve_length)
                result_data = asdict(result)
                display_data = self._prepare_redact_display_data(result)
            else:
                raise ToolError(f"Unknown action: {action}")
            
            execution_time = (datetime.now() - start_time).total_seconds()
            
            message = f"Secrets {action} completed"
            if action == "scan" and hasattr(result, 'secrets_found'):
                message += f": {result.secrets_found} secrets found"
            elif action == "redact" and hasattr(result, 'secrets_found'):
                message += f": {result.secrets_found} secrets redacted"
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=result_data,
                message=message,
                display_policy=DisplayPolicy.SYSTEM,
                show_cli=show_cli,
                display_data=display_data if show_cli else None,
                execution_time=execution_time
            )
            
        except Exception as e:
            logger.error(f"SecretsTool error: {e}")
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Secrets operation failed: {str(e)}",
                error=e,
                display_policy=DisplayPolicy.SYSTEM
            )

    def _add_custom_patterns(self, custom_patterns: Dict[str, str]):
        """Add custom regex patterns for detection"""
        for pattern_name, pattern_regex in custom_patterns.items():
            try:
                self.compiled_patterns[f"custom_{pattern_name}"] = {
                    'pattern': re.compile(pattern_regex),
                    'confidence': 0.7,  # Default confidence for custom patterns
                    'severity': 'medium'
                }
            except re.error as e:
                logger.warning(f"Invalid custom pattern {pattern_name}: {e}")

    def _perform_scan(self, target: Optional[str], text: Optional[str], 
                     file_patterns: List[str], secret_types: List[str],
                     min_confidence: float, include_low_confidence: bool) -> ScanResult:
        """Perform secrets scanning operation"""
        all_detections = []
        files_scanned = 0
        
        if text:
            # Scan provided text
            detections = self._scan_text(text, "inline_text", secret_types, min_confidence)
            all_detections.extend(detections)
            files_scanned = 1
        elif target:
            # Scan file(s)
            target_path = Path(target)
            if target_path.is_file():
                if self._should_scan_file(target_path, file_patterns):
                    detections = self._scan_file(target_path, secret_types, min_confidence)
                    all_detections.extend(detections)
                    files_scanned = 1
            elif target_path.is_dir():
                for file_path in self._find_scannable_files(target_path, file_patterns):
                    detections = self._scan_file(file_path, secret_types, min_confidence)
                    all_detections.extend(detections)
                    files_scanned += 1
        
        # Filter by confidence if not including low confidence
        if not include_low_confidence:
            all_detections = [d for d in all_detections if d.confidence >= min_confidence]
        
        # Aggregate statistics
        secrets_by_type = {}
        secrets_by_severity = {}
        
        for detection in all_detections:
            secrets_by_type[detection.secret_type] = secrets_by_type.get(detection.secret_type, 0) + 1
            secrets_by_severity[detection.severity] = secrets_by_severity.get(detection.severity, 0) + 1
        
        # Estimate false positive rate
        false_positive_rate = self._estimate_false_positive_rate(all_detections)
        
        return ScanResult(
            files_scanned=files_scanned,
            secrets_found=len(all_detections),
            secrets_by_type=secrets_by_type,
            secrets_by_severity=secrets_by_severity,
            detections=all_detections,
            scan_time_ms=0.0,  # Set by caller
            false_positive_rate=false_positive_rate
        )

    def _scan_file(self, file_path: Path, secret_types: List[str], 
                  min_confidence: float) -> List[SecretDetection]:
        """Scan single file for secrets"""
        detections = []
        
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            detections = self._scan_text(content, str(file_path), secret_types, min_confidence)
            
        except (OSError, IOError) as e:
            logger.debug(f"Error reading file {file_path}: {e}")
        
        return detections

    def _scan_text(self, text: str, source_identifier: str, 
                  secret_types: List[str], min_confidence: float) -> List[SecretDetection]:
        """Scan text content for secrets"""
        detections = []
        lines = text.split('\n')
        
        # Filter patterns by secret types if specified
        patterns_to_use = self.compiled_patterns
        if secret_types:
            patterns_to_use = {k: v for k, v in self.compiled_patterns.items() 
                             if k in secret_types or any(st in k for st in secret_types)}
        
        for pattern_name, pattern_config in patterns_to_use.items():
            pattern = pattern_config['pattern']
            confidence = pattern_config['confidence']
            severity = pattern_config['severity']
            
            for match in pattern.finditer(text):
                line_number = text[:match.start()].count('\n') + 1
                line_start = text.rfind('\n', 0, match.start()) + 1
                line_end = text.find('\n', match.end())
                if line_end == -1:
                    line_end = len(text)
                
                # Extract context
                line_content = lines[line_number - 1] if line_number <= len(lines) else ""
                context_before = lines[max(0, line_number - 2)] if line_number > 1 else ""
                context_after = lines[line_number] if line_number < len(lines) else ""
                
                # Check for false positives
                if self._is_false_positive(match.group(0), line_content):
                    continue
                
                # Create masked version for logging
                matched_text = match.group(0)
                if len(matched_text) > 8:
                    masked_text = matched_text[:4] + "***" + matched_text[-2:]
                else:
                    masked_text = "***" + matched_text[-2:]
                
                detection = SecretDetection(
                    file_path=source_identifier,
                    line_number=line_number,
                    column_start=match.start() - line_start,
                    column_end=match.end() - line_start,
                    secret_type=pattern_name,
                    confidence=confidence,
                    matched_text=masked_text,
                    context_before=context_before,
                    context_after=context_after,
                    rule_id=pattern_name,
                    severity=severity
                )
                
                detections.append(detection)
        
        return detections

    def _is_false_positive(self, matched_text: str, context: str) -> bool:
        """Check if a match is likely a false positive"""
        # Check against known false positive patterns
        for fp_pattern in self.false_positive_patterns:
            if re.search(fp_pattern, matched_text, re.IGNORECASE):
                return True
            if re.search(fp_pattern, context, re.IGNORECASE):
                return True
        
        # Additional heuristics
        # Check if it's in a comment
        if context.strip().startswith('#') or context.strip().startswith('//'):
            return True
        
        # Check if it's a common placeholder
        if 'example' in matched_text.lower() or 'test' in matched_text.lower():
            return True
        
        return False

    def _redact_text(self, text: str, secret_types: List[str], min_confidence: float,
                    redaction_char: str, preserve_length: bool) -> RedactionResult:
        """Redact secrets from text"""
        detections = self._scan_text(text, "redaction_target", secret_types, min_confidence)
        
        redacted_text = text
        redaction_map = {}
        
        # Sort detections by position (reverse order to maintain indices)
        detections.sort(key=lambda d: d.column_start, reverse=True)
        
        for detection in detections:
            # Find the actual match in text (need to re-match since we only have line/column)
            pattern = self.compiled_patterns[detection.secret_type]['pattern']
            for match in pattern.finditer(text):
                if (text[:match.start()].count('\n') + 1 == detection.line_number and
                    match.start() - text.rfind('\n', 0, match.start()) - 1 == detection.column_start):
                    
                    original_secret = match.group(0)
                    
                    if preserve_length:
                        replacement = redaction_char * len(original_secret)
                    else:
                        replacement = f"[REDACTED_{detection.secret_type.upper()}]"
                    
                    redaction_map[original_secret] = replacement
                    redacted_text = redacted_text[:match.start()] + replacement + redacted_text[match.end():]
                    break
        
        return RedactionResult(
            original_text=text,
            redacted_text=redacted_text,
            secrets_found=len(detections),
            redaction_map=redaction_map
        )

    def _find_scannable_files(self, directory: Path, file_patterns: List[str]) -> List[Path]:
        """Find files that should be scanned for secrets"""
        files = []
        
        for file_path in directory.rglob('*'):
            if file_path.is_file() and self._should_scan_file(file_path, file_patterns):
                files.append(file_path)
        
        return files

    def _should_scan_file(self, file_path: Path, file_patterns: List[str]) -> bool:
        """Determine if a file should be scanned"""
        # Check exclude patterns
        if any(fnmatch.fnmatch(str(file_path), pattern) for pattern in self.exclude_patterns):
            return False
        
        # Check include patterns if specified
        if file_patterns:
            return any(fnmatch.fnmatch(file_path.name, pattern) for pattern in file_patterns)
        
        # Default: scan text files
        return self._is_text_file(file_path)

    def _is_text_file(self, file_path: Path) -> bool:
        """Check if file is likely a text file"""
        text_extensions = {
            '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp',
            '.cs', '.php', '.rb', '.go', '.rs', '.swift', '.kt',
            '.html', '.css', '.xml', '.json', '.yaml', '.yml', '.toml',
            '.md', '.txt', '.ini', '.cfg', '.conf', '.env',
            '.sh', '.bash', '.zsh', '.ps1', '.sql'
        }
        
        return file_path.suffix.lower() in text_extensions

    def _estimate_false_positive_rate(self, detections: List[SecretDetection]) -> float:
        """Estimate false positive rate based on detection patterns"""
        if not detections:
            return 0.0
        
        # Simple heuristic based on confidence levels
        low_confidence_count = sum(1 for d in detections if d.confidence < 0.7)
        return low_confidence_count / len(detections)

    def _prepare_scan_display_data(self, result: ScanResult) -> Dict[str, Any]:
        """Prepare scan results for display"""
        return {
            "type": "secrets_scan",
            "summary": {
                "files_scanned": result.files_scanned,
                "secrets_found": result.secrets_found,
                "secrets_by_type": result.secrets_by_type,
                "secrets_by_severity": result.secrets_by_severity,
                "false_positive_rate": f"{result.false_positive_rate:.1%}"
            },
            "high_priority_secrets": [
                {
                    "file": detection.file_path,
                    "line": detection.line_number,
                    "type": detection.secret_type,
                    "severity": detection.severity,
                    "confidence": f"{detection.confidence:.1%}"
                }
                for detection in sorted(result.detections, 
                                      key=lambda x: (x.severity == 'critical', x.confidence),
                                      reverse=True)[:10]
            ]
        }

    def _prepare_redact_display_data(self, result: RedactionResult) -> Dict[str, Any]:
        """Prepare redaction results for display"""
        return {
            "type": "secrets_redaction",
            "summary": {
                "secrets_redacted": result.secrets_found,
                "original_length": len(result.original_text),
                "redacted_length": len(result.redacted_text),
                "redaction_count": len(result.redaction_map)
            }
        }


# Register the tool
from deile.tools.registry import ToolRegistry
ToolRegistry.register("secrets_scanner", SecretsTool)