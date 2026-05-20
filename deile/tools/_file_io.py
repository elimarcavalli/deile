"""Universal file reading with binary detection and encoding fallback.

Extracted from ``ReadFileTool`` so the tool class focuses on the
pipeline (parse args → resolve path → read → format result) and the
encoding/binary heuristics live with the I/O policy. None of the helpers
here touch ``ToolContext`` or any tool-shaped state — they take a path
and a small configuration block.

Public entry point: :func:`read_file_universal`. The two helpers
``_handle_binary_file`` and ``_read_file_manual_encoding`` are module-
private because the caller never needs to invoke them directly.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


_MAGIC_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "PNG Image"),
    (b"\xff\xd8\xff", "JPEG Image"),
    (b"GIF8", "GIF Image"),
    (b"%PDF", "PDF Document"),
    (b"PK\x03\x04", "ZIP Archive (or Office Document)"),
    (b"\x50\x4b\x05\x06", "ZIP Archive (empty)"),
    (b"\x50\x4b\x07\x08", "ZIP Archive (spanned)"),
    (b"RIFF", "RIFF Media File (WAV/AVI)"),
    (b"\x00\x00\x01\x00", "ICO Icon"),
    (b"BM", "Bitmap Image"),
    (b"\x1f\x8b", "GZIP Archive"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip Archive"),
)


def _is_binary_file(data: bytes) -> bool:
    """Heuristic: NUL byte in the first 1KB ⇒ binary."""
    return b"\x00" in data[:1024]


def _read_file_manual_encoding(file_path: Path) -> str:
    """Try a small list of common encodings; final fallback is utf-8 with replace."""
    encodings = [
        "utf-8", "utf-16", "utf-16-le", "utf-16-be",
        "latin-1", "cp1252", "iso-8859-1",
    ]
    for encoding in encodings:
        try:
            content = file_path.read_text(encoding=encoding)
            if content.startswith("\ufeff"):
                content = content[1:]
            logger.debug(
                "Successfully read %s with manual encoding: %s", file_path, encoding,
            )
            return content
        except (UnicodeDecodeError, UnicodeError):
            continue
    logger.warning(
        "All encodings failed for %s, using utf-8 with errors='replace'", file_path,
    )
    return file_path.read_text(encoding="utf-8", errors="replace")


def _handle_binary_file(file_path: Path, raw_data: bytes) -> str:
    """Render a human-readable summary for a binary file."""
    file_extension = file_path.suffix.lower()
    file_size = len(raw_data)

    file_type = "Binary File"
    for signature, type_name in _MAGIC_SIGNATURES:
        if raw_data.startswith(signature):
            file_type = type_name
            break

    if "image" in file_type.lower():
        return f"""[ARQUIVO DE IMAGEM]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Extensão: {file_extension}
Caminho: {file_path}

Este é um arquivo de imagem binário. Para visualizar:
- Use um visualizador de imagens
- Converta para base64 se necessário para incorporação
- Primeira linha de bytes: {raw_data[:32].hex()}
"""

    if "pdf" in file_type.lower():
        return f"""[DOCUMENTO PDF]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Caminho: {file_path}

Este é um documento PDF. Para extrair texto:
- Use bibliotecas como PyPDF2, pdfplumber ou pdfminer
- Primeira linha de bytes: {raw_data[:64].decode('ascii', errors='replace')}
"""

    if "archive" in file_type.lower() or "zip" in file_type.lower():
        return f"""[ARQUIVO COMPACTADO]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Extensão: {file_extension}
Caminho: {file_path}

Este é um arquivo compactado. Para extrair:
- Use bibliotecas como zipfile, tarfile, ou 7z
- Primeira linha de bytes: {raw_data[:32].hex()}
"""

    sample_text = raw_data[:512].decode("utf-8", errors="replace")
    readable_chars = sum(1 for c in sample_text if c.isprintable())
    if readable_chars > len(sample_text) * 0.7:
        return f"""[ARQUIVO MISTO/BINÁRIO COM TEXTO]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Extensão: {file_extension}
Caminho: {file_path}

Amostra de texto encontrada:
{sample_text[:200]}...

[Resto do arquivo contém dados binários]
"""
    return f"""[ARQUIVO BINÁRIO]
Tipo: {file_type}
Tamanho: {file_size:,} bytes ({file_size / 1024:.1f} KB)
Extensão: {file_extension}
Caminho: {file_path}

Este arquivo contém dados binários não-texto.
Primeira linha de bytes (hex): {raw_data[:32].hex()}
Primeira linha de bytes (ascii): {raw_data[:32].decode('ascii', errors='replace')}
"""


def read_file_universal(
    file_path: Path,
    *,
    max_size_bytes: int,
    encoding_detection: bool,
) -> str:
    """Read ``file_path`` as text, falling back through binary detection and
    multi-encoding heuristics.

    Raises ``ValueError`` when the file is larger than ``max_size_bytes``.
    Other I/O errors are swallowed and a placeholder string is returned —
    callers rely on the text-shaped return value rather than exceptions.
    """
    try:
        file_size = file_path.stat().st_size
        if file_size > max_size_bytes:
            size_mb = file_size / (1024 * 1024)
            max_mb = max_size_bytes / (1024 * 1024)
            raise ValueError(
                f"Arquivo muito grande ({size_mb:.2f}MB). Limite: {max_mb:.2f}MB"
            )

        with open(file_path, "rb") as f:
            raw_data = f.read()

        if _is_binary_file(raw_data):
            return _handle_binary_file(file_path, raw_data)

        if not encoding_detection:
            return raw_data.decode("utf-8", errors="replace")

        try:
            import chardet
        except ImportError:
            logger.debug("chardet not available, using manual encoding detection")
            return _read_file_manual_encoding(file_path)

        detected = chardet.detect(raw_data)
        encoding = detected.get("encoding", "utf-8")
        confidence = detected.get("confidence", 0)
        logger.debug(
            "Detected encoding for %s: %s (confidence: %.2f)",
            file_path, encoding, confidence,
        )

        if confidence < 0.7:
            encodings_to_try = ["utf-8", "utf-16", "latin-1", "cp1252", "iso-8859-1"]
        else:
            encodings_to_try = [encoding, "utf-8", "utf-16", "latin-1"]

        for enc in encodings_to_try:
            try:
                content = raw_data.decode(enc)
                if content.startswith("\ufeff"):
                    content = content[1:]
                logger.debug("Successfully read %s with encoding: %s", file_path, enc)
                return content
            except (UnicodeDecodeError, UnicodeError):
                continue

        content = raw_data.decode("utf-8", errors="replace")
        logger.warning(
            "Used fallback utf-8 with errors='replace' for %s", file_path,
        )
        return content

    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Error in universal file reading for %s: %s", file_path, exc)
        try:
            return file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"[ERRO: Não foi possível ler o arquivo {file_path}: {exc}]"
