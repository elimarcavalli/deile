"""Secrets Scanner for DEILE - Detect and redact sensitive information"""

import re
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
import logging


logger = logging.getLogger(__name__)


class SecretType(Enum):
    """Types of secrets that can be detected"""
    API_KEY = "api_key"
    PASSWORD = "password"
    TOKEN = "token"
    PRIVATE_KEY = "private_key"
    CONNECTION_STRING = "connection_string"
    EMAIL = "email"
    CREDIT_CARD = "credit_card"
    SSN = "ssn"
    AWS_ACCESS_KEY = "aws_access_key"
    GITHUB_TOKEN = "github_token"
    SLACK_TOKEN = "slack_token"
    GENERIC_SECRET = "generic_secret"


@dataclass
class SecretMatch:
    """A detected secret match"""
    secret_type: SecretType
    line_number: int
    start_pos: int
    end_pos: int
    matched_text: str
    confidence: float
    context: str
    file_path: Optional[str] = None


class SecretsScanner:
    """Scanner for detecting secrets and sensitive information"""
    
    def __init__(self):
        self.patterns = self._load_detection_patterns()
        self.whitelist_patterns = self._load_whitelist_patterns()
        
    def _load_detection_patterns(self) -> Dict[SecretType, List[Tuple[re.Pattern, float]]]:
        """Load regex patterns for secret detection"""
        
        patterns = {
            SecretType.API_KEY: [
                (re.compile(r'[aA][pP][iI]_?[kK][eE][yY].*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', re.IGNORECASE), 0.9),
                (re.compile(r'[aA][pP][iI][kK][eE][yY].*[:=]\s*([A-Za-z0-9_\-]{32,})'), 0.8),
                (re.compile(r'key.*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', re.IGNORECASE), 0.7),
            ],
            
            SecretType.PASSWORD: [
                (re.compile(r'[pP][aA][sS][sS][wW][oO][rR][dD].*[:=]\s*["\']([^"\']{6,})["\']'), 0.8),
                (re.compile(r'pwd.*[:=]\s*["\']([^"\']{6,})["\']', re.IGNORECASE), 0.7),
                (re.compile(r'passwd.*[:=]\s*["\']([^"\']{6,})["\']', re.IGNORECASE), 0.7),
            ],
            
            SecretType.TOKEN: [
                (re.compile(r'[tT][oO][kK][eE][nN].*[:=]\s*["\']([A-Za-z0-9_\-\.]{20,})["\']'), 0.9),
                (re.compile(r'bearer\s+([A-Za-z0-9_\-\.]{20,})', re.IGNORECASE), 0.8),
                (re.compile(r'authorization.*[:=]\s*["\']?([A-Za-z0-9_\-\.]{20,})["\']?', re.IGNORECASE), 0.7),
            ],
            
            SecretType.PRIVATE_KEY: [
                (re.compile(r'-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----'), 0.95),
                (re.compile(r'-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----'), 0.95),
                (re.compile(r'private_key.*[:=]\s*["\']([^"\']+)["\']', re.IGNORECASE), 0.8),
            ],
            
            SecretType.AWS_ACCESS_KEY: [
                (re.compile(r'AKIA[0-9A-Z]{16}'), 0.95),
                (re.compile(r'aws_access_key_id.*[:=]\s*["\']?(AKIA[0-9A-Z]{16})["\']?', re.IGNORECASE), 0.9),
            ],
            
            SecretType.GITHUB_TOKEN: [
                (re.compile(r'ghp_[A-Za-z0-9_]{36}'), 0.95),
                (re.compile(r'github_token.*[:=]\s*["\']([A-Za-z0-9_]{20,})["\']', re.IGNORECASE), 0.8),
            ],
            
            SecretType.SLACK_TOKEN: [
                (re.compile(r'xox[baprs]-[0-9a-zA-Z\-]{10,48}'), 0.95),
                (re.compile(r'slack.*token.*[:=]\s*["\']([^"\']{20,})["\']', re.IGNORECASE), 0.8),
            ],
            
            SecretType.CONNECTION_STRING: [
                (re.compile(r'(?:mongodb|mysql|postgres|redis)://[^"\s]+', re.IGNORECASE), 0.9),
                (re.compile(r'connection_string.*[:=]\s*["\']([^"\']+://[^"\']+)["\']', re.IGNORECASE), 0.8),
            ],
            
            SecretType.EMAIL: [
                (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), 0.6),
            ],
            
            SecretType.CREDIT_CARD: [
                (re.compile(r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3[0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b'), 0.8),
            ],
            
            SecretType.GENERIC_SECRET: [
                (re.compile(r'secret.*[:=]\s*["\']([^"\']{10,})["\']', re.IGNORECASE), 0.6),
                (re.compile(r'["\'][A-Za-z0-9_\-]{32,}["\']'), 0.4),  # Generic long strings
            ]
        }
        
        return patterns
    
    def _load_whitelist_patterns(self) -> List[re.Pattern]:
        """Load patterns for whitelisted content that should not be flagged"""
        
        whitelist = [
            # Common placeholders
            re.compile(r'(your_api_key_here|replace_with_your_key|example_key|dummy_token)', re.IGNORECASE),
            re.compile(r'(lorem ipsum|sample|test|demo|placeholder)', re.IGNORECASE),
            re.compile(r'^[X\*]+$'),  # Masked values
            re.compile(r'(123456|password|secret|token)$', re.IGNORECASE),  # Common examples
            
            # File paths and URLs without sensitive info
            re.compile(r'^(https?://|file://|/[a-zA-Z0-9/_\-\.]+)$'),
            
            # Version numbers and hashes
            re.compile(r'^v?\d+\.\d+\.\d+'),
            re.compile(r'^[a-f0-9]{32,64}$'),  # Likely file hashes
        ]
        
        return whitelist
    
    def _is_whitelisted(self, text: str) -> bool:
        """Check if text matches whitelist patterns"""
        
        for pattern in self.whitelist_patterns:
            if pattern.search(text):
                return True
        return False
    
    def scan_text(self, text: str, file_path: Optional[str] = None) -> List[SecretMatch]:
        """Scan text for secrets"""
        
        matches = []
        lines = text.split('\n')
        
        for line_num, line in enumerate(lines, 1):
            for secret_type, pattern_list in self.patterns.items():
                for pattern, confidence in pattern_list:
                    for match in pattern.finditer(line):
                        matched_text = match.group(1) if match.groups() else match.group(0)
                        
                        # Skip if whitelisted
                        if self._is_whitelisted(matched_text):
                            continue
                            
                        # Skip very short matches for low confidence patterns
                        if confidence < 0.7 and len(matched_text) < 8:
                            continue
                        
                        secret_match = SecretMatch(
                            secret_type=secret_type,
                            line_number=line_num,
                            start_pos=match.start(),
                            end_pos=match.end(),
                            matched_text=matched_text,
                            confidence=confidence,
                            context=line.strip(),
                            file_path=file_path
                        )
                        
                        matches.append(secret_match)
        
        # Remove duplicates based on position
        unique_matches = []
        seen_positions = set()
        
        for match in sorted(matches, key=lambda x: (x.line_number, x.start_pos, -x.confidence)):
            pos_key = (match.line_number, match.start_pos, match.end_pos)
            if pos_key not in seen_positions:
                unique_matches.append(match)
                seen_positions.add(pos_key)
        
        return unique_matches
    
    def scan_file(self, file_path: Path) -> List[SecretMatch]:
        """Scan a single file for secrets"""
        
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            matches = self.scan_text(content, str(file_path))
            return matches
            
        except Exception as e:
            logger.warning(f"Failed to scan file {file_path}: {e}")
            return []
    
    def scan_directory(self, 
                      directory_path: Path,
                      file_pattern: str = '*',
                      max_files: int = 1000) -> Dict[str, List[SecretMatch]]:
        """Scan directory for secrets"""
        
        results = {}
        files_scanned = 0
        
        try:
            for file_path in directory_path.rglob(file_pattern):
                if files_scanned >= max_files:
                    logger.warning(f"Reached max files limit ({max_files}) for scanning")
                    break
                
                if file_path.is_file() and not self._should_skip_file(file_path):
                    matches = self.scan_file(file_path)
                    if matches:
                        results[str(file_path)] = matches
                    files_scanned += 1
                    
        except Exception as e:
            logger.error(f"Error scanning directory {directory_path}: {e}")
        
        return results
    
    def _should_skip_file(self, file_path: Path) -> bool:
        """Check if file should be skipped during scanning"""
        
        # Skip binary files
        if file_path.suffix.lower() in {'.exe', '.dll', '.so', '.dylib', '.bin', '.img', '.iso'}:
            return True
            
        # Skip image/media files
        if file_path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.gif', '.mp3', '.mp4', '.avi', '.mkv'}:
            return True
            
        # Skip archive files
        if file_path.suffix.lower() in {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2'}:
            return True
            
        # Skip cache directories
        cache_dirs = {'__pycache__', '.pytest_cache', 'node_modules', '.git', '.svn'}
        if any(cache_dir in file_path.parts for cache_dir in cache_dirs):
            return True
            
        # Skip very large files (>10MB)
        try:
            if file_path.stat().st_size > 10 * 1024 * 1024:
                return True
        except:
            pass
            
        return False
    
    def redact_text(self, text: str, redaction_char: str = '*') -> Tuple[str, List[SecretMatch]]:
        """Redact secrets in text and return redacted text with matches"""
        
        matches = self.scan_text(text)
        if not matches:
            return text, matches
        
        # Sort matches by position (reverse order to maintain positions during replacement)
        sorted_matches = sorted(matches, key=lambda x: (x.line_number, x.start_pos), reverse=True)
        
        lines = text.split('\n')
        
        for match in sorted_matches:
            line_idx = match.line_number - 1
            if 0 <= line_idx < len(lines):
                line = lines[line_idx]
                # Replace the matched text with redaction characters
                replacement = redaction_char * len(match.matched_text)
                lines[line_idx] = line[:match.start_pos] + replacement + line[match.end_pos:]
        
        redacted_text = '\n'.join(lines)
        return redacted_text, matches
    
    def redact_file(self, 
                   file_path: Path,
                   backup: bool = True,
                   redaction_char: str = '*') -> List[SecretMatch]:
        """Redact secrets in a file"""
        
        try:
            # Read original content
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                original_content = f.read()
            
            # Create backup if requested
            if backup:
                backup_path = file_path.with_suffix(file_path.suffix + '.backup')
                with open(backup_path, 'w', encoding='utf-8') as f:
                    f.write(original_content)
            
            # Redact content
            redacted_content, matches = self.redact_text(original_content, redaction_char)
            
            # Write redacted content if changes were made
            if matches:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(redacted_content)
                logger.info(f"Redacted {len(matches)} secrets in {file_path}")
            
            return matches
            
        except Exception as e:
            logger.error(f"Failed to redact file {file_path}: {e}")
            return []
    
    def get_summary(self, matches: List[SecretMatch]) -> Dict[str, Any]:
        """Get summary statistics of detected secrets"""
        
        if not matches:
            return {"total_secrets": 0}
        
        # Count by type
        type_counts = {}
        confidence_levels = {"high": 0, "medium": 0, "low": 0}
        
        for match in matches:
            # Count by type
            secret_type = match.secret_type.value
            type_counts[secret_type] = type_counts.get(secret_type, 0) + 1
            
            # Count by confidence
            if match.confidence >= 0.8:
                confidence_levels["high"] += 1
            elif match.confidence >= 0.6:
                confidence_levels["medium"] += 1
            else:
                confidence_levels["low"] += 1
        
        return {
            "total_secrets": len(matches),
            "by_type": type_counts,
            "by_confidence": confidence_levels,
            "unique_files": len(set(m.file_path for m in matches if m.file_path)),
            "avg_confidence": sum(m.confidence for m in matches) / len(matches)
        }