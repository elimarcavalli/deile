"""Shared shell-command security screen used by `bash_tool` and `execution_tools`.

Single source of truth for the dangerous/moderate-risk pattern catalogue
(pilar 08 §1: security policy must have a single authoritative location).
"""

from __future__ import annotations

import re
from typing import List, Tuple

from .base import SecurityLevel

# Patterns whose match means: refuse to execute / mark DANGEROUS.
# Combined union of historical bash_tool + execution_tools lists; case-insensitive.
DANGEROUS_PATTERNS: Tuple[str, ...] = (
    r'rm\s+.*-rf\s*/',          # rm -rf /
    r'sudo\s+rm\s+-rf',         # sudo rm -rf
    r'mkfs',                    # filesystem format
    r'mkfs\.',                  # filesystem format (variant)
    r'dd\s+.*of=/dev/',         # write to raw device
    r'dd\s+if=.*of=/dev/',      # write to raw device (variant)
    r'fdisk',                   # partitioning
    r'fdisk.*--delete',         # partition deletion (variant)
    r'format\s+[c-z]:',         # Windows format
    r'del\s+.*\*\.\*',          # Windows wildcard delete
    r'del\s+/[fqs]\s+c:\\',     # Windows force delete root
    r'shutdown',                # system shutdown
    r'reboot',                  # system reboot
    r'poweroff',                # power off
    r'halt',                    # halt
    r'init\s+0',                # init runlevel 0
    r':\(\)\{\s*:\s*\|\s*:\s*&\s*\};:',  # fork bomb
    r':(){ :|:& };:',           # fork bomb (literal-spaced variant)
    r'curl.*\|\s*sh',           # curl-piped shell
    r'wget.*\|\s*sh',           # wget-piped shell
    r'chmod\s+777\s+/',         # world-writable root
    r'chown\s+.*\s+/',          # chown root
    r'>\s*/dev/(sd[a-z]|nvme|hd[a-z]|disk|mapper|dm-?\d|md\d|loop\d|xvd|vd|mmcblk)',  # redirect to block device
)

# Patterns whose match emits a warning but does NOT block.
MODERATE_PATTERNS: Tuple[str, ...] = (
    r'sudo',
    r'su\s+',
    r'rm\s+.*-r',
    r'chmod\s+.*7',
    r'chown',
    r'mount',
    r'umount',
    r'systemctl',
    r'service\s+',
    r'iptables',
    r'ufw',
    r'firewall',
    r'>.*\.sh',
    r'curl.*-s',
    r'wget.*-O',
    r'pip\s+install.*--user',
    r'npm\s+install.*-g',
)

_DANGEROUS_RE = tuple(re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS)
_MODERATE_RE = tuple(re.compile(p, re.IGNORECASE) for p in MODERATE_PATTERNS)


def assess_risk(command: str) -> Tuple[str, List[str]]:
    """Classify `command` and return `(level_value, warnings)`.

    `level_value` is one of `SecurityLevel.{SAFE,MODERATE,DANGEROUS}.value`.
    """
    for pattern in _DANGEROUS_RE:
        if pattern.search(command):
            return SecurityLevel.DANGEROUS.value, [
                f"Matches dangerous pattern: {pattern.pattern}"
            ]

    warnings: List[str] = []
    for pattern in _MODERATE_RE:
        if pattern.search(command):
            warnings.append(f"Potentially risky: {pattern.pattern}")

    if warnings:
        return SecurityLevel.MODERATE.value, warnings
    return SecurityLevel.SAFE.value, []
