"""EditFileTool — patches find/replace atômicos em arquivos existentes."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...core.exceptions import ValidationError
from .._path_resolution import (_PATH_ARG_KEYS_EDIT, LocalFileAccessViolation,
                               _apply_post_write_hint, _extract_path_arg,
                               _not_found_message, _resolve_project_path)
from ..base import SyncTool, ToolContext, ToolResult, ToolStatus
from .write_tool import WriteFileTool  # needed for WriteFileTool._atomic_write_text

logger = logging.getLogger(__name__)


class EditFileTool(SyncTool):
    """Edita arquivos existentes via lista ordenada de patches find/replace.

    Cada patch substitui ocorrência(s) de ``find`` por ``replace``. Patches são
    aplicados sequencialmente em memória; cada patch vê o resultado dos
    anteriores. No final, escreve atomicamente — ou todos os patches passam,
    ou nenhum byte do arquivo muda.

    **Garantias (não negociáveis)**:

    * **Atômico**: falha em qualquer patch aborta a operação inteira; o arquivo
      original permanece intacto. Múltiplas chamadas concorrentes nunca
      observam estado intermediário porque a publicação final usa ``os.replace``.
    * **Determinístico**: ordem dos patches importa. Patch ``i`` busca em
      *post-(i-1)-buffer*.
    * **Não-ambíguo por default**: ``find`` deve aparecer EXATAMENTE 1 vez no
      buffer corrente. 0 ocorrências → erro "find não encontrado". ≥2
      ocorrências → erro "find ambíguo, refine o contexto ou use replace_all".
    * **Replace-all opt-in**: ``replace_all=true`` aceita ≥1 ocorrência e
      substitui todas.
    * **Fidelidade byte-a-byte**: usa o mesmo ``_atomic_write_text`` do
      ``WriteFileTool`` — tempfile + ``fsync`` + read-back + ``os.replace``.

    **Quando usar `edit_file` em vez de `write_file`**:

    * Alterar trechos específicos de um arquivo existente (1 a N edits).
    * Múltiplas alterações no mesmo arquivo numa única chamada (transação).

    **Quando usar `write_file` em vez de `edit_file`**:

    * Criar arquivo novo (``edit_file`` exige que o arquivo já exista).
    * Reescrever totalmente (≳70% das linhas mudam).
    * Arquivo binário.

    O LLM deve escolher: ``edit_file`` para alterações pontuais é mais barato
    em tokens e mais seguro (não há risco de "esquecer" partes do arquivo).
    """

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Surgically edits an existing file via an ordered list of find/replace "
            "patches. Use for targeted modifications; use write_file for new files "
            "or full rewrites."
        )

    @property
    def category(self) -> str:
        return "file"

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Aplica patches ao arquivo, atomicamente."""
        logger.debug(f"EditFileTool - parsed_args keys: {list(context.parsed_args.keys())}")

        # 1. Extrai file_path. EditFileTool's canonical order is
        # ``file_path > path > filename > file > filepath`` — pinned via
        # ``_PATH_ARG_KEYS_EDIT``.
        file_path = _extract_path_arg(context.parsed_args, keys=_PATH_ARG_KEYS_EDIT)
        if not file_path:
            return ToolResult.error_result(
                message=(
                    "No file path provided. edit_file requires `file_path` "
                    "(string) and `patches` (array of {find, replace})."
                ),
                error=ValidationError("file_path is required"),
            )

        # 2. Extrai e normaliza patches.
        # Aceita também o formato single-edit "old_string"/"new_string" para
        # ergonomia: o LLM pode mandar uma edição simples sem montar array.
        patches_raw = context.parsed_args.get("patches")
        if patches_raw is None:
            old = context.parsed_args.get("old_string")
            new = context.parsed_args.get("new_string")
            if old is not None and new is not None:
                patches_raw = [{
                    "find": old,
                    "replace": new,
                    "replace_all": bool(context.parsed_args.get("replace_all", False)),
                }]

        if not isinstance(patches_raw, list) or not patches_raw:
            return ToolResult.error_result(
                message=(
                    "`patches` must be a non-empty array of objects with "
                    "fields {find, replace, replace_all?}. Got: "
                    f"{type(patches_raw).__name__}"
                ),
                error=ValidationError("patches must be a non-empty list"),
            )

        # 3. Valida estrutura de cada patch UPFRONT (antes de tocar disco).
        normalized: List[Dict[str, Any]] = []
        for idx, raw in enumerate(patches_raw, start=1):
            if not isinstance(raw, dict):
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} is not an object: got {type(raw).__name__}. "
                        f"Each patch must be {{find: str, replace: str, replace_all?: bool}}."
                    ),
                    error=ValidationError(f"patch #{idx} must be an object"),
                )
            find = raw.get("find")
            replace = raw.get("replace")
            replace_all = bool(raw.get("replace_all", False))
            if not isinstance(find, str) or not isinstance(replace, str):
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} must have `find` (str) and `replace` (str). "
                        f"Got find={type(find).__name__}, replace={type(replace).__name__}."
                    ),
                    error=ValidationError(f"patch #{idx} requires string find/replace"),
                )
            if find == "":
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} has empty `find`. Empty strings match "
                        "everywhere — refusing. Use write_file to create or "
                        "fully rewrite a file."
                    ),
                    error=ValidationError(f"patch #{idx}: empty find"),
                )
            normalized.append({
                "find": find,
                "replace": replace,
                "replace_all": replace_all,
            })

        # 4. Resolve path e lê conteúdo atual.
        try:
            resolved = _resolve_project_path(file_path, context.working_directory)
        except LocalFileAccessViolation as exc:
            return ToolResult.error_result(message=str(exc), error=exc)

        full_path = Path(resolved.absolute)
        if not full_path.exists():
            return ToolResult.error_result(
                message=_not_found_message(
                    resolved,
                    file_path,
                    detail="edit_file only modifies existing files — use "
                           "write_file to create new ones.",
                ),
                error=FileNotFoundError(
                    f"File '{resolved.relative_to_cwd}' not found"
                ),
            )
        if not full_path.is_file():
            return ToolResult.error_result(
                message=f"Path is not a file: {resolved.relative_to_cwd}",
                error=ValueError(f"'{resolved.relative_to_cwd}' is not a file"),
            )

        try:
            original_bytes = full_path.read_bytes()
        except OSError as exc:
            return ToolResult.error_result(
                message=f"Could not read file {resolved.relative_to_cwd}: {exc}",
                error=exc,
            )

        # UTF-8 só é exigido se o arquivo for textual válido. Decodificamos com
        # 'strict' para que arquivos binários falhem cedo e sejam roteados
        # para write_file/bash_execute.
        try:
            buffer = original_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return ToolResult.error_result(
                message=(
                    f"File {resolved.relative_to_cwd} is not valid UTF-8 "
                    f"(byte {exc.start}: {exc.reason}). edit_file works only "
                    f"on UTF-8 text files. For binary or other encodings, "
                    f"use write_file."
                ),
                error=exc,
            )

        # 5. Aplica patches em memória. Cada patch vê o resultado dos anteriores.
        applied_log: List[Dict[str, Any]] = []
        for idx, patch in enumerate(normalized, start=1):
            find = patch["find"]
            replace = patch["replace"]
            replace_all = patch["replace_all"]
            occurrences = buffer.count(find)

            if occurrences == 0:
                # Diagnóstico rico: mostra trecho próximo de uma substring
                # parecida quando for plausível (linha que contém o início do find).
                hint = self._suggest_near_match(buffer, find)
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} failed: `find` not present in current "
                        f"buffer (0 matches). Hint: read the file again and "
                        f"copy the exact substring (including whitespace and "
                        f"newlines).{hint} No change was written; original "
                        f"file is intact."
                    ),
                    error=ValidationError(
                        f"patch #{idx}: 'find' not found in buffer"
                    ),
                    metadata={
                        "function_name": "edit_file",
                        "file_path": resolved.absolute,
                        "failed_patch_index": idx,
                        "occurrences": 0,
                    },
                )
            if occurrences > 1 and not replace_all:
                preview = find[:80] + ("…" if len(find) > 80 else "")
                return ToolResult.error_result(
                    message=(
                        f"patch #{idx} failed: `find` is ambiguous — "
                        f"{occurrences} occurrences in the current buffer "
                        f"(found {preview!r}). Add more surrounding context "
                        f"to make `find` unique, or set `replace_all: true` "
                        f"to replace all occurrences. No change was written; "
                        f"original file is intact."
                    ),
                    error=ValidationError(
                        f"patch #{idx}: 'find' is ambiguous ({occurrences} matches)"
                    ),
                    metadata={
                        "function_name": "edit_file",
                        "file_path": resolved.absolute,
                        "failed_patch_index": idx,
                        "occurrences": occurrences,
                    },
                )

            if replace_all:
                replaced_count = occurrences
                buffer = buffer.replace(find, replace)
            else:
                # Exatamente 1 ocorrência confirmada acima — single replace.
                replaced_count = 1
                buffer = buffer.replace(find, replace, 1)

            applied_log.append({
                "index": idx,
                "occurrences": occurrences,
                "replaced": replaced_count,
                "find_preview": find[:60] + ("…" if len(find) > 60 else ""),
            })

        # 6. No-op? Se nada mudou (ex.: find == replace), não reescreve disco.
        new_bytes = buffer.encode("utf-8")
        if new_bytes == original_bytes:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=resolved.absolute,
                message=(
                    f"No bytes changed after applying {len(normalized)} patch(es) "
                    f"to {resolved.relative_to_cwd}. File left untouched. "
                    f"(This usually means find == replace; check the patches.)"
                ),
                metadata={
                    "function_name": "edit_file",
                    "file_path": resolved.absolute,
                    "project_relative_path": resolved.relative_to_cwd,
                    "patches_applied": applied_log,
                    "no_op": True,
                },
            )

        # 7. Escreve atomicamente. Reutiliza o helper já testado em isolação.
        try:
            WriteFileTool._atomic_write_text(full_path, buffer)
        except Exception as exc:
            return ToolResult.error_result(
                message=(
                    f"Atomic write failed for {resolved.relative_to_cwd}: {exc}. "
                    f"Original file is intact (atomic write guarantees no "
                    f"partial publish)."
                ),
                error=exc,
            )

        # 8. Display rico + validation hint pós-write quando aplicável.
        line_count = buffer.count("\n") + (0 if buffer.endswith("\n") else 1 if buffer else 0)
        rich_lines = [f"● edit_file({resolved.relative_to_cwd})"]
        rich_lines.append(
            f"  ⎿ Applied {len(applied_log)} patch(es), now {len(new_bytes)} bytes / {line_count} lines"
        )
        for entry in applied_log:
            rich_lines.append(
                f"     • patch #{entry['index']}: replaced {entry['replaced']}× → "
                f"{entry['find_preview']!r}"
            )
        rich_display = "\n".join(rich_lines)

        message_parts = [
            f"Applied {len(applied_log)} patch(es) to:",
            f"  file_path: {resolved.absolute}",
            f"  project_relative: {resolved.relative_to_cwd}",
            f"  bytes_before: {len(original_bytes)} → bytes_after: {len(new_bytes)}",
        ]
        if resolved.note:
            message_parts.append(f"  ⚠️  PATH_NORMALIZED: {resolved.note}")

        metadata: Dict[str, Any] = {
            "function_name": "edit_file",
            "file_path": resolved.absolute,
            "project_relative_path": resolved.relative_to_cwd,
            "input_path": resolved.input,
            "path_normalization_note": resolved.note,
            "patches_applied": applied_log,
            "bytes_before": len(original_bytes),
            "bytes_after": len(new_bytes),
            "encoding": "utf-8",
            "rich_display": rich_display,
        }

        _apply_post_write_hint(resolved.relative_to_cwd, metadata, message_parts)

        return ToolResult(
            status=ToolStatus.SUCCESS,
            data=resolved.absolute,
            message="\n".join(message_parts),
            metadata=metadata,
        )

    @staticmethod
    def _suggest_near_match(buffer: str, find: str) -> str:
        """Sugestão de diagnóstico quando ``find`` não bate.

        Heurística barata: pega a primeira linha não-vazia de ``find`` e
        verifica se aparece (após strip) no buffer. Se aparecer, sugere que
        o LLM provavelmente errou whitespace/newline. Retorna string vazia
        quando nada útil pode ser dito — não fabrica diagnóstico.
        """
        if not find:
            return ""
        first_line = next((ln for ln in find.splitlines() if ln.strip()), "")
        if not first_line:
            return ""
        stripped = first_line.strip()
        if len(stripped) < 6:
            return ""
        if stripped in buffer:
            return (
                f" The first line of `find` ({stripped!r}) DOES appear in the "
                f"buffer — the mismatch is likely whitespace, indentation, "
                f"or trailing newlines. Re-read the file and copy exactly."
            )
        return ""
