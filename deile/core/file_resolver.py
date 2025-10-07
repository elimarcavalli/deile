"""Smart file resolution with fuzzy matching and pattern recognition

Componente core para resolução inteligente de arquivos no DEILE,
seguindo a arquitetura enterprise-grade e padrões de segurança existentes.
"""

import re
import fnmatch
import time
from pathlib import Path
from typing import List, Optional, Dict, Set
from dataclasses import dataclass
from enum import Enum
from difflib import SequenceMatcher
import logging

from ..core.exceptions import ValidationError


logger = logging.getLogger(__name__)


class MatchType(Enum):
    """Types of file matches"""
    EXACT = "exact"           # Exact filename match
    PATTERN = "pattern"       # Common pattern match (readme -> README.md)
    FUZZY = "fuzzy"          # Fuzzy string match
    EXTENSION = "extension"   # Extension-based match
    PARTIAL = "partial"       # Partial substring match
    DIRECTORY = "directory"   # Directory match


@dataclass
class FileMatch:
    """Represents a potential file match with confidence scoring"""
    path: Path
    query: str
    confidence: float
    match_type: MatchType
    reason: str
    exists: bool

    def __post_init__(self):
        """Validate and normalize the file match"""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence must be between 0.0 and 1.0, got {self.confidence}")

        # Ensure path is absolute if it exists
        if self.exists and not self.path.is_absolute():
            self.path = self.path.resolve()


class CommonFilePatterns:
    """Database of common file patterns and their variations"""

    PATTERNS = {
        # Documentation files
        'readme': ['README.md', 'README.txt', 'readme.md', 'Readme.md', 'README.rst', 'README'],
        'license': ['LICENSE', 'LICENSE.txt', 'LICENSE.md', 'license.txt', 'COPYING', 'LICENCE'],
        'changelog': ['CHANGELOG.md', 'CHANGELOG.txt', 'HISTORY.md', 'CHANGES.md', 'NEWS.md'],
        'contributing': ['CONTRIBUTING.md', 'CONTRIBUTING.txt', 'CONTRIBUTORS.md'],
        'authors': ['AUTHORS', 'AUTHORS.md', 'AUTHORS.txt', 'CONTRIBUTORS'],

        # Configuration files
        'config': ['config.py', 'config.json', 'config.yaml', 'config.yml', '.config', 'settings.py'],
        'settings': ['settings.py', 'settings.json', 'settings.yaml', 'config.py'],
        'requirements': ['requirements.txt', 'requirements.in', 'pyproject.toml', 'Pipfile', 'poetry.lock'],
        'package': ['package.json', 'pyproject.toml', 'setup.py', 'setup.cfg'],
        'dockerfile': ['Dockerfile', 'dockerfile', 'Dockerfile.dev', 'Dockerfile.prod'],
        'docker-compose': ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml'],
        'makefile': ['Makefile', 'makefile', 'GNUmakefile'],

        # Source code entry points
        'main': ['main.py', 'main.js', 'main.ts', 'index.py', 'index.js', 'index.ts'],
        'app': ['app.py', 'app.js', 'application.py', 'server.py'],
        'cli': ['cli.py', 'command.py', 'commands.py', '__main__.py'],
        'server': ['server.py', 'server.js', 'app.py', 'main.py'],

        # Test files
        'test': ['test_*.py', 'tests.py', '*_test.py', '*.test.js', 'test.py'],
        'tests': ['tests/', 'test/', '__tests__/', 'spec/'],

        # Common directories
        'source': ['src/', 'source/', 'lib/', 'app/'],
        'src': ['src/', 'source/', 'lib/'],
        'docs': ['docs/', 'doc/', 'documentation/', 'wiki/'],
        'examples': ['examples/', 'example/', 'samples/', 'demo/'],

        # Environment and deployment
        'env': ['.env', '.env.local', '.env.development', '.env.production'],
        'environment': ['.env', '.env.local', 'environment.py', 'env.py'],
        'deploy': ['deploy.py', 'deployment.py', 'deploy/', 'deployment/'],

        # Version control and CI
        'gitignore': ['.gitignore', '.git/'],
        'ci': ['.github/', '.gitlab-ci.yml', '.travis.yml', 'ci/'],
    }

    @classmethod
    def get_patterns(cls, query: str) -> List[str]:
        """Get file patterns for a given query"""
        query_lower = query.lower().strip()
        return cls.PATTERNS.get(query_lower, [])

    @classmethod
    def find_matching_pattern(cls, query: str) -> Optional[str]:
        """Find the best matching pattern key for a query"""
        query_lower = query.lower().strip()

        # Exact match
        if query_lower in cls.PATTERNS:
            return query_lower

        # Partial match
        for pattern_key in cls.PATTERNS:
            if query_lower in pattern_key or pattern_key in query_lower:
                return pattern_key

        # Fuzzy match for common typos
        fuzzy_matches = {
            'requirments': 'requirements',
            'requirment': 'requirements',
            'confg': 'config',
            'cfg': 'config',
            'conf': 'config',
            'read': 'readme',
            'rm': 'readme',
            'lic': 'license',
            'doc': 'docs',
            'src': 'source',
        }

        if query_lower in fuzzy_matches:
            return fuzzy_matches[query_lower]

        return None


class SmartFileResolver:
    """Intelligent file name resolution with fuzzy matching capabilities"""

    def __init__(self, working_directory: Path):
        self.working_directory = Path(working_directory).resolve()
        self.logger = logging.getLogger(__name__)
        self._directory_cache = {}
        self._cache_ttl = 60  # Cache directory listings for 60 seconds

    def resolve_file(self, query: str, include_directories: bool = False) -> List[FileMatch]:
        """
        Resolve a natural language file query to potential matches

        Args:
            query: Natural language file reference (e.g., "readme", "config")
            include_directories: Whether to include directory matches

        Returns:
            List of FileMatch objects sorted by confidence
        """
        if not query or not query.strip():
            return []

        query = query.strip()
        matches = []

        try:
            # Strategy 1: Exact filename match
            matches.extend(self._find_exact_matches(query))

            # Strategy 2: Common pattern matching
            matches.extend(self._find_pattern_matches(query))

            # Strategy 3: Fuzzy matching
            matches.extend(self._find_fuzzy_matches(query))

            # Strategy 4: Extension-based matching
            matches.extend(self._find_extension_matches(query))

            # Strategy 5: Directory matching (if enabled)
            if include_directories:
                matches.extend(self._find_directory_matches(query))

            # Remove duplicates and sort by confidence
            unique_matches = self._deduplicate_matches(matches)
            unique_matches.sort(key=lambda m: m.confidence, reverse=True)

            return unique_matches

        except Exception as e:
            self.logger.error(f"Error in file resolution: {e}")
            return []

    def get_best_match(self, query: str, min_confidence: float = 0.7) -> Optional[FileMatch]:
        """
        Get the highest confidence match for a query

        Args:
            query: File query string
            min_confidence: Minimum confidence threshold

        Returns:
            Best FileMatch or None if no match meets threshold
        """
        matches = self.resolve_file(query)
        if matches and matches[0].confidence >= min_confidence:
            return matches[0]
        return None

    def suggest_alternatives(self, query: str, max_suggestions: int = 5) -> List[FileMatch]:
        """
        Provide alternative suggestions when no exact match found

        Args:
            query: Original query string
            max_suggestions: Maximum number of suggestions

        Returns:
            List of alternative FileMatch suggestions
        """
        matches = self.resolve_file(query, include_directories=True)
        return matches[:max_suggestions]

    def _find_exact_matches(self, query: str) -> List[FileMatch]:
        """Find exact filename matches"""
        matches = []
        files = self._get_directory_files()

        for file_path in files:
            if file_path.name == query:
                matches.append(FileMatch(
                    path=file_path,
                    query=query,
                    confidence=1.0,
                    match_type=MatchType.EXACT,
                    reason=f"Exact filename match: {file_path.name}",
                    exists=file_path.exists()
                ))

        return matches

    def _find_pattern_matches(self, query: str) -> List[FileMatch]:
        """Find matches using common file patterns"""
        matches = []
        pattern_key = CommonFilePatterns.find_matching_pattern(query)

        if not pattern_key:
            return matches

        patterns = CommonFilePatterns.get_patterns(pattern_key)
        files = self._get_directory_files()

        for pattern in patterns:
            for file_path in files:
                if fnmatch.fnmatch(file_path.name, pattern):
                    # Higher confidence for exact pattern matches
                    confidence = 0.9 if pattern == file_path.name else 0.85

                    # Boost confidence for common high-value files
                    if pattern_key in ['readme', 'config', 'main', 'requirements']:
                        confidence += 0.05

                    matches.append(FileMatch(
                        path=file_path,
                        query=query,
                        confidence=min(confidence, 0.95),
                        match_type=MatchType.PATTERN,
                        reason=f"Pattern match: {query} -> {pattern_key} -> {file_path.name}",
                        exists=file_path.exists()
                    ))

        return matches

    def _find_fuzzy_matches(self, query: str) -> List[FileMatch]:
        """Find fuzzy string matches"""
        matches = []
        files = self._get_directory_files()

        for file_path in files:
            # Compare with filename (without extension)
            filename_base = file_path.stem.lower()
            query_lower = query.lower()

            # Skip very short queries to avoid noise
            if len(query_lower) < 2:
                continue

            similarity = SequenceMatcher(None, query_lower, filename_base).ratio()

            if similarity >= 0.6:  # 60% similarity threshold
                # Scale down fuzzy matches and boost for longer queries
                confidence = similarity * 0.7
                if len(query_lower) >= 4:
                    confidence += 0.1

                matches.append(FileMatch(
                    path=file_path,
                    query=query,
                    confidence=min(confidence, 0.8),  # Cap fuzzy confidence
                    match_type=MatchType.FUZZY,
                    reason=f"Fuzzy match: {similarity:.2f} similarity with {filename_base}",
                    exists=file_path.exists()
                ))

        return matches

    def _find_extension_matches(self, query: str) -> List[FileMatch]:
        """Find matches based on file extensions"""
        matches = []

        # Extension mapping for common queries
        extension_map = {
            'python': ['.py'],
            'javascript': ['.js', '.ts'],
            'typescript': ['.ts'],
            'config': ['.json', '.yaml', '.yml', '.toml', '.ini', '.cfg'],
            'docs': ['.md', '.txt', '.rst'],
            'documentation': ['.md', '.txt', '.rst'],
            'image': ['.png', '.jpg', '.jpeg', '.gif', '.svg'],
            'data': ['.csv', '.json', '.xml', '.yaml'],
            'sql': ['.sql'],
            'shell': ['.sh', '.bash'],
        }

        query_lower = query.lower()
        if query_lower in extension_map:
            extensions = extension_map[query_lower]
            files = self._get_directory_files()

            for file_path in files:
                if file_path.suffix.lower() in extensions:
                    matches.append(FileMatch(
                        path=file_path,
                        query=query,
                        confidence=0.6,
                        match_type=MatchType.EXTENSION,
                        reason=f"Extension match: {file_path.suffix} for {query}",
                        exists=file_path.exists()
                    ))

        return matches

    def _find_directory_matches(self, query: str) -> List[FileMatch]:
        """Find directory matches"""
        matches = []
        directories = self._get_directory_subdirs()

        for dir_path in directories:
            query_lower = query.lower()
            dir_name_lower = dir_path.name.lower()

            # Exact directory name match
            if query_lower == dir_name_lower:
                matches.append(FileMatch(
                    path=dir_path,
                    query=query,
                    confidence=0.8,
                    match_type=MatchType.EXACT,
                    reason=f"Exact directory match: {dir_path.name}",
                    exists=dir_path.exists()
                ))
            # Partial directory name match
            elif query_lower in dir_name_lower:
                similarity = SequenceMatcher(None, query_lower, dir_name_lower).ratio()
                matches.append(FileMatch(
                    path=dir_path,
                    query=query,
                    confidence=similarity * 0.5,  # Lower confidence for directories
                    match_type=MatchType.DIRECTORY,
                    reason=f"Directory match: {dir_path.name}",
                    exists=dir_path.exists()
                ))

        return matches

    def _get_directory_files(self) -> List[Path]:
        """Get list of files in working directory with caching"""
        cache_key = str(self.working_directory)

        if cache_key in self._directory_cache:
            cached_time, files = self._directory_cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return files

        try:
            files = [p for p in self.working_directory.iterdir() if p.is_file()]
            self._directory_cache[cache_key] = (time.time(), files)
            return files
        except (OSError, PermissionError) as e:
            self.logger.warning(f"Could not read directory {self.working_directory}: {e}")
            return []

    def _get_directory_subdirs(self) -> List[Path]:
        """Get list of subdirectories in working directory"""
        try:
            return [p for p in self.working_directory.iterdir() if p.is_dir() and not p.name.startswith('.')]
        except (OSError, PermissionError) as e:
            self.logger.warning(f"Could not read directory {self.working_directory}: {e}")
            return []

    def _deduplicate_matches(self, matches: List[FileMatch]) -> List[FileMatch]:
        """Remove duplicate matches, keeping the highest confidence"""
        seen_paths = {}

        for match in matches:
            path_str = str(match.path)
            if path_str not in seen_paths or match.confidence > seen_paths[path_str].confidence:
                seen_paths[path_str] = match

        return list(seen_paths.values())

    def clear_cache(self):
        """Clear the directory cache"""
        self._directory_cache.clear()
        self.logger.debug("Directory cache cleared")


# Global instance management (thread-safe)
_file_resolver_instances = {}

def get_file_resolver(working_directory: Optional[Path] = None) -> SmartFileResolver:
    """Get or create a SmartFileResolver instance"""
    if working_directory is None:
        working_directory = Path.cwd()

    working_directory = Path(working_directory).resolve()
    cache_key = str(working_directory)

    if cache_key not in _file_resolver_instances:
        _file_resolver_instances[cache_key] = SmartFileResolver(working_directory)

    return _file_resolver_instances[cache_key]


def clear_resolver_cache():
    """Clear all resolver instances and their caches"""
    global _file_resolver_instances
    for resolver in _file_resolver_instances.values():
        resolver.clear_cache()
    _file_resolver_instances.clear()