"""Tiny async I/O helpers — generic read/write JSON and text.

These primitives exist to keep ``json.dump``/``json.load`` and other
blocking ``open()`` calls off the event loop (principle 03 §1). Callers
get ``await``-friendly versions without each subpackage re-defining its
own private ``_read_json``/``_write_json`` sync helpers.

Scope is intentionally narrow:
- Only JSON dict round-trip + plain text write are exposed.
- Domain-specific formats (JSONL append, YAML mutation of structured
  schemas) stay in their respective subpackages — they have semantics
  this module is not the right home for.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict


def _read_json_sync(path: Path) -> Dict[str, Any]:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _write_json_sync(path: Path, data: Dict[str, Any]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _write_text_sync(path: Path, text: str) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


async def read_json(path: Path) -> Dict[str, Any]:
    """Async-safe ``json.load`` — delegates the blocking read to a thread."""
    return await asyncio.to_thread(_read_json_sync, path)


async def write_json(path: Path, data: Dict[str, Any]) -> None:
    """Async-safe ``json.dump`` (indent=2, UTF-8) — non-atomic."""
    await asyncio.to_thread(_write_json_sync, path, data)


async def write_text(path: Path, text: str) -> None:
    """Async-safe text write — UTF-8, no trailing newline added."""
    await asyncio.to_thread(_write_text_sync, path, text)
