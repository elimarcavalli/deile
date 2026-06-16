"""Persistência central do custo da frota CLI no ``UsageRepository`` (issue #638).

Substitui o pull-via-``kubectl exec`` (parsear ``.progress`` + ledger por-PVC, que
some quando o pod escala a zero / é ``force-delete``) por um **push central** no
momento do dispatch: o worker reporta tokens-por-modelo no bloco ``usage`` da
resposta do ``/v1/dispatch``; o **pipeline** (componente longevo) persiste 1
registro por modelo no SQLite central (``~/.deile/db/usage.db``). A tela
``[T]okens`` passa a ler desse store — independe de pod estar de pé.

DECISÃO DE SCHEMA (sem migração, sem tabela paralela): o ``UsageRecord`` já
existente carrega worker/stage/issue em colunas que tinham slots livres,
espelhando o que o ``records_for_stage_model`` já faz (stage no ``session_id``):

* ``provider_id`` ← worker-kind (``opencode``/``codex``/… — "quem serviu");
* ``tier``        ← stage (``implement``/``pr_review``/…);
* ``session_id``  ← ``channel_id`` do dispatch (``pipeline-issue-<N>`` /
                    ``pipeline-pr-<N>``), que carrega o issue/PR;
* ``model_id``    ← model-id real do dispatch (``cli_model``, anti ``unknown``).

Um dispatch multi-modelo vira N registros (1 por modelo do ``tokens_by_model``).
Preço: fonte ÚNICA ``jsonl_cost.fleet_cost_of_model`` — sem duplicar tabela.

Best-effort cardinal: qualquer falha (parser/preço/SQLite indisponível) é logada
e engolida — NUNCA derruba o dispatch.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _load_jsonl_cost():
    """Importa ``jsonl_cost`` de ``infra/k8s/`` (fonte única de preço da frota).

    Mesmo padrão robusto do ``dispatch_resolver._load_cli_adapter_registry``: no
    cluster o módulo é COPiado plano para ``/app`` (já no path); em dev/local
    insere-se ``infra/k8s`` no path. Indisponível → ``None`` (degrada: o custo
    fica 0.0 mas os tokens ainda são persistidos).
    """
    try:
        import jsonl_cost  # noqa: PLC0415

        return jsonl_cost
    except ImportError:
        repo_root = Path(__file__).resolve().parents[3]
        infra_k8s = repo_root / "infra" / "k8s"
        if infra_k8s.is_dir() and str(infra_k8s) not in sys.path:
            sys.path.insert(0, str(infra_k8s))
        try:
            import jsonl_cost  # noqa: PLC0415

            return jsonl_cost
        except Exception as exc:  # noqa: BLE001 — degrada sem derrubar o dispatch
            logger.debug("jsonl_cost indisponível (%s) — custo da frota fica 0.0", exc)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "falha ao carregar jsonl_cost (%s) — custo da frota fica 0.0", exc
        )
        return None


def _coerce_tokens(tk: object) -> dict:
    """Normaliza um bloco de tokens do wire para ``{in,out,cache_read,cache_write}``.

    Tolera tanto o contrato novo (``cache_read``/``cache_write``) quanto o shape
    interno do parser (``cr``/``cc``) por robustez de wire.
    """
    if not isinstance(tk, dict):
        return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}

    def _i(*keys: str) -> int:
        for k in keys:
            v = tk.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return {
        "in": _i("in", "input"),
        "out": _i("out", "output"),
        "cache_read": _i("cache_read", "cr"),
        "cache_write": _i("cache_write", "cc"),
    }


def record_fleet_usage(
    response: object,
    *,
    worker_kind: str,
    stage: Optional[str],
    channel_id: str,
    cli_model: Optional[str],
    repo: Optional[object] = None,
) -> int:
    """Persiste o uso de um dispatch da frota no ``UsageRepository`` central.

    Caminho ``wait`` (critique/refine/pr_review/follow_ups/mention): lê o bloco
    ``usage`` da resposta do ``/v1/dispatch`` (``tokens_by_model`` + ``model`` +
    ``worker``) e grava 1 :class:`UsageRecord` por modelo. Retorna o número de
    registros gravados (0 quando não há uso — claude/deile contabilizam por
    outras vias, ou worker antigo sem o bloco).

    Args:
        response: dict da resposta do ``/v1/dispatch``.
        worker_kind: kind do worker (``opencode``/…); fallback do bloco ``usage``.
        stage: etapa canônica do pipeline (vai para ``tier``).
        channel_id: ``pipeline-issue-<N>`` / ``pipeline-pr-<N>`` (vai para ``session_id``).
        cli_model: model-id do payload (anti ``unknown`` no fallback do model).
        repo: ``UsageRepository`` opcional (injeção em teste); default = singleton.

    Best-effort: qualquer exceção é logada e engolida; retorna 0.
    """
    if not isinstance(response, dict):
        return 0
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return _persist_usage(
        usage,
        worker=str(usage.get("worker") or worker_kind or "").strip() or "cli",
        stage=stage,
        session_id=str(channel_id or "").strip() or "pipeline-fleet",
        cli_model=cli_model,
        success=bool(response.get("ok")),
        repo=repo,
        dedup=False,
    )


def record_fleet_usage_from_resume_info(
    info: object,
    *,
    worker_kind: str,
    stage: Optional[str],
    channel_id: str,
    task_id: str,
    repo: Optional[object] = None,
) -> int:
    """Grava o custo central de um dispatch fire-and-forget já CONCLUÍDO (issue #638).

    O ``implement`` paralelo despacha fire-and-forget (202) — a resposta do
    dispatch é descartada, então o bloco ``usage`` é lido depois via resume-info
    no reconcile. Como o reconcile roda a cada tick até o label mudar, a escrita
    é DEDUPADA pelo ``session_id`` ``<channel_id>#<task_id>`` (único por task):
    registros já presentes para esse session_id → no-op idempotente.

    Best-effort: qualquer exceção é logada e engolida; retorna registros gravados.
    """
    if not isinstance(info, dict):
        return 0
    usage = info.get("usage") if isinstance(info.get("usage"), dict) else {}
    tid = str(task_id or "").strip()
    base = str(channel_id or "").strip() or "pipeline-fleet"
    session_id = f"{base}#{tid}" if tid else base
    return _persist_usage(
        usage,
        worker=str(worker_kind or "").strip() or "cli",
        stage=stage,
        session_id=session_id,
        cli_model=info.get("cli_model"),
        success=not bool(info.get("last_is_error")),
        repo=repo,
        dedup=True,
    )


def _persist_usage(
    usage: dict,
    *,
    worker: str,
    stage: Optional[str],
    session_id: str,
    cli_model: Optional[str],
    success: bool,
    repo: Optional[object],
    dedup: bool,
) -> int:
    """Núcleo da persistência: 1 :class:`UsageRecord` por modelo do ``usage``.

    Compartilhado pelos caminhos ``wait`` e fire-and-forget. Best-effort: toda
    exceção é engolida (custo NUNCA derruba o dispatch/reconcile).
    """
    try:
        if not isinstance(usage, dict):
            return 0
        tokens_by_model = usage.get("tokens_by_model")
        if not isinstance(tokens_by_model, dict) or not tokens_by_model:
            return 0

        fallback_model = str(usage.get("model") or cli_model or "").strip()
        tier = str(stage or "").strip() or "implement"

        if repo is None:
            from deile.storage.usage_repository import get_usage_repository

            repo = get_usage_repository()
        from deile.storage.usage_repository import UsageRecord

        if dedup and repo.records_for_session(session_id):
            # Já contabilizado em tick anterior — idempotente.
            return 0

        jsonl_cost = _load_jsonl_cost()
        ts = time.time()
        written = 0
        for model_id, raw_tk in tokens_by_model.items():
            tk = _coerce_tokens(raw_tk)
            prompt, completion = tk["in"], tk["out"]
            cached, cache_write = tk["cache_read"], tk["cache_write"]
            total = prompt + completion + cached + cache_write
            if total <= 0:
                continue  # sem tokens reais → não polui o store
            # Anti model=unknown: chave vazia OU literal "unknown" cai no
            # fallback (model do bloco usage → cli_model do payload).
            mid = str(model_id or "").strip()
            model = (mid if mid and mid != "unknown" else fallback_model) or "unknown"
            cost = 0.0
            if jsonl_cost is not None:
                # Fonte ÚNICA de preço; cache-write soma ao input (chave ``cc``),
                # cache-read ao preço de read (chave ``cr``) no fleet_cost_of_model.
                cost = float(
                    jsonl_cost.fleet_cost_of_model(
                        {
                            "in": prompt,
                            "out": completion,
                            "cc": cache_write,
                            "cr": cached,
                        },
                        model,
                    )
                )
            repo.record(
                UsageRecord(
                    provider_id=worker,
                    model_id=model,
                    tier=tier,
                    session_id=session_id,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    cached_tokens=cached,
                    total_tokens=total,
                    cost_usd=cost,
                    success=success,
                    timestamp=ts,
                )
            )
            written += 1

        if written:
            logger.info(
                "custo da frota persistido: worker=%s stage=%s session=%s registros=%d",
                worker,
                tier,
                session_id,
                written,
            )
        return written
    except Exception as exc:  # noqa: BLE001 — escrita de custo NUNCA derruba o tick
        logger.warning(
            "falha ao persistir custo da frota (worker=%s stage=%s session=%s): %s",
            worker,
            stage,
            session_id,
            exc,
        )
        return 0
