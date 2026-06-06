"""
SPIKE — DESCARTAVEL — Issue #529: Compacao in-place no claude-worker.

Harness SDK in-process para validar a API Compaction (beta compact-2026-01-12)
com autenticacao OAuth do claude-worker. NAO e codigo de producao.

Estrutura:
- make_oauth_client()             — cria Anthropic client com auth_token OAuth
- run_session_with_compaction()   — loop SDK 40+ rounds; usa context_management
                                    server-side para compaction automatica em 80%
- refresh_oauth_token()           — simula renovacao de token mid-session (AC0b)
- make_one_compaction_request()   — uma request simples para validar Gate 0 (AC0)

COMO USAR:
  # Requer ANTHROPIC_AUTH_TOKEN na env (ou credentials.json no path padrao).
  export ANTHROPIC_AUTH_TOKEN="sk-ant-oau01-..."
  python3 infra/k8s/spikes/compaction_oauth_spike.py

Lido pelos testes em test_compaction_oauth.py, replay_pr527_session.py e
test_checkpoint_continuity.py.

Referencia de API:
  - compaction server-side: betas=["compact-2026-01-12"] +
    context_management={"edits": [{"type": "compact_20260112", ...}]}
  - ver anthropic.types.beta.BetaCompact20260112EditParam
  - ver anthropic.types.beta.BetaContextManagementConfigParam
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import anthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes de configuracao do spike
# ---------------------------------------------------------------------------

# Modelo padrao para o spike (Opus para fidelidade ao cenario real de #527).
# Trocar por claude-sonnet-4-6 para testes de menor custo.
DEFAULT_MODEL = os.environ.get("SPIKE_MODEL", "claude-opus-4-8")

# Beta que habilita a API de compaction. Verificar se este valor ainda e aceito;
# se a API retornar 400 com "invalid beta", registrar o valor aceito no relatorio.
COMPACTION_BETA = "compact-2026-01-12"

# Fracao do contexto que dispara compaction (espelha DEILE_CLAUDE_RESUME_CONTEXT_FRACTION).
COMPACTION_THRESHOLD = float(os.environ.get("DEILE_CLAUDE_RESUME_CONTEXT_FRACTION", "0.80"))

# Numero de tokens que dispara compaction no server-side (80% de 200k = 160k).
# Configura o campo trigger.value em BetaCompact20260112EditParam.
COMPACTION_TOKEN_TRIGGER = int(os.environ.get("SPIKE_COMPACTION_TOKEN_TRIGGER", str(int(200_000 * 0.80))))

# ---------------------------------------------------------------------------
# Pricing de referencia (verificar pagina de pricing da Anthropic antes de usar)
# ---------------------------------------------------------------------------

# Opus 4: input $15/MTok, output $75/MTok, cache_write $18.75/MTok, cache_read $1.50/MTok
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {
        "input": 15.0 / 1_000_000,
        "output": 75.0 / 1_000_000,
        "cache_write": 18.75 / 1_000_000,
        "cache_read": 1.50 / 1_000_000,
    },
    "claude-sonnet-4-6": {
        "input": 3.0 / 1_000_000,
        "output": 15.0 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
        "cache_read": 0.30 / 1_000_000,
    },
}

# Janela de contexto por modelo.
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
}


# ---------------------------------------------------------------------------
# Helpers de credencial OAuth (espelha logica de _refresh_oauth_with_lock)
# ---------------------------------------------------------------------------

def _creds_path() -> Path:
    home = Path(os.environ.get("HOME", "/home/claude"))
    return home / ".claude" / "credentials.json"


def _load_token_from_credentials(creds_path: Optional[Path] = None) -> Optional[str]:
    """Le o accessToken do credentials.json (mesma logica de _refresh_oauth_with_lock)."""
    path = creds_path or _creds_path()
    if not path.exists():
        return None
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    token = (oauth or {}).get("accessToken") if isinstance(oauth, dict) else None
    if not token:
        token = creds.get("accessToken") if isinstance(creds, dict) else None
    return token or None


def _get_expires_at_ms(creds_path: Optional[Path] = None) -> Optional[float]:
    """Retorna expiresAt em ms do credentials.json, ou None se ausente/invalido."""
    path = creds_path or _creds_path()
    if not path.exists():
        return None
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    val = (oauth or {}).get("expiresAt") if isinstance(oauth, dict) else None
    if val is None:
        val = creds.get("expiresAt") if isinstance(creds, dict) else None
    return float(val) if isinstance(val, (int, float)) else None


def is_token_expiring_soon(creds_path: Optional[Path] = None, window_s: int = 300) -> bool:
    """True se o accessToken expira nos proximos `window_s` segundos."""
    expires_at_ms = _get_expires_at_ms(creds_path)
    if expires_at_ms is None:
        return False
    return (expires_at_ms / 1000.0 - time.time()) < window_s


# ---------------------------------------------------------------------------
# Factory de client OAuth
# ---------------------------------------------------------------------------

def make_oauth_client(token: Optional[str] = None) -> anthropic.Anthropic:
    """Cria um Anthropic client com auth_token OAuth (nao api_key).

    Prioridade de token:
      1. `token` passado diretamente
      2. ANTHROPIC_AUTH_TOKEN da env
      3. accessToken lido de credentials.json

    Raises RuntimeError se nenhum token disponivel.

    Note: auth_token no SDK mapeia para o header Authorization: Bearer <token>,
    que e o formato OAuth — distinto do x-api-key usado por api_key.
    """
    resolved = token or os.environ.get("ANTHROPIC_AUTH_TOKEN") or _load_token_from_credentials()
    if not resolved:
        raise RuntimeError(
            "Nenhum token OAuth disponivel. "
            "Defina ANTHROPIC_AUTH_TOKEN ou garanta que credentials.json existe."
        )
    return anthropic.Anthropic(auth_token=resolved)


def refresh_oauth_token(
    client_ref: list[anthropic.Anthropic],
    creds_path: Optional[Path] = None,
) -> bool:
    """Simula renovacao de token OAuth mid-session (AC0b).

    Substitui o client em `client_ref[0]` por um novo com o token mais recente
    lido de credentials.json (ou de ANTHROPIC_AUTH_TOKEN se o arquivo nao existe).

    Retorna True se o token foi renovado com sucesso.

    Nota: no caminho SDK in-process, o subprocess `claude -p` NAO esta presente
    para fazer o refresh automatico. Esta funcao e o ponto de extensao para
    implementar o refresh in-process em producao (roadmap pos-spike).

    Em producao seria necessario chamar o endpoint de renovacao OAuth antes de
    tentar substituir o client — este spike apenas simula a substituicao
    com o token mais recente disponivel em disco/env.
    """
    fresh_token = (
        _load_token_from_credentials(creds_path)
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    if not fresh_token:
        logger.warning("refresh_oauth_token: nenhum token encontrado — session pode falhar")
        return False
    client_ref[0] = anthropic.Anthropic(auth_token=fresh_token)
    logger.info("Token OAuth renovado mid-session (novo client criado)")
    return True


# ---------------------------------------------------------------------------
# Calculo de custo
# ---------------------------------------------------------------------------

def _round_cost_usd(usage: dict[str, Any], model: str) -> float:
    """Calcula custo de um round em USD baseado em usage dict."""
    pricing = _PRICING.get(model, _PRICING["claude-opus-4-8"])
    return (
        usage.get("input_tokens", 0) * pricing["input"]
        + usage.get("output_tokens", 0) * pricing["output"]
        + usage.get("cache_creation_input_tokens", 0) * pricing["cache_write"]
        + usage.get("cache_read_input_tokens", 0) * pricing["cache_read"]
    )


def _usage_from_response(response: anthropic.types.beta.BetaMessage) -> dict[str, Any]:
    """Extrai campos de usage de uma BetaMessage."""
    return {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
    }


def _context_fraction(usage: dict[str, Any], context_window: int) -> float:
    """Calcula a fracao do contexto usada baseada no usage dict.

    Implementa a mesma logica de medicao do bug fix `065e3ea` (peak-of-1-round):
    usa o maximo entre input_tokens bruto e a soma de cache_{creation,read}.
    Evita o bug de somar cache_read acumulado que causava false positives.
    """
    input_tokens = usage.get("input_tokens", 0)
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    peak = max(input_tokens, cache_creation + cache_read)
    return peak / context_window if context_window > 0 else 0.0


# ---------------------------------------------------------------------------
# Validacao Gate 0 (AC0) — uma request simples com o beta de compaction
# ---------------------------------------------------------------------------

def make_one_compaction_request(
    token: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Faz UMA request real com o beta de compaction para validar Gate 0 (AC0).

    Usa context_management com edits server-side (approach nao-deprecated).
    O beta header "compact-2026-01-12" e passado via betas=[COMPACTION_BETA].

    Retorna dict com:
        status_code: 200 se OK (inferido da ausencia de excecao)
        response_type: tipo do primeiro bloco retornado
        has_content: bool — True se response.content nao e vazio
        has_compaction_block: bool — True se algum bloco e do tipo 'compaction'
        usage: dict com tokens input/output
        stop_reason: string do stop_reason
        error: str ou None
    """
    client = make_oauth_client(token)
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=256,
            messages=[
                {"role": "user", "content": "Diga 'compaction ok' em portugues."}
            ],
            # Habilita o beta de compaction
            betas=[COMPACTION_BETA],
            # Configura compaction server-side com pause_after_compaction=True para
            # forcar retorno do bloco de compaction mesmo num historico curto.
            # Em sessoes reais, o trigger seria 80% do contexto (160k tokens).
            context_management={
                "edits": [
                    {
                        "type": "compact_20260112",
                        "pause_after_compaction": True,
                        "trigger": {
                            "type": "input_tokens",
                            "value": COMPACTION_TOKEN_TRIGGER,
                        },
                    }
                ]
            },
        )
        # Verifica presenca de bloco de compaction (AC0 — a API retornou um turn compactado)
        has_compaction_block = any(
            getattr(block, "type", "") == "compaction"
            for block in response.content
        )
        return {
            "status_code": 200,
            "response_type": type(response.content[0]).__name__ if response.content else "empty",
            "has_content": bool(response.content),
            "has_compaction_block": has_compaction_block,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "stop_reason": response.stop_reason,
            "error": None,
        }
    except anthropic.AuthenticationError as exc:
        return {"status_code": 401, "error": f"AuthenticationError: {exc}", "has_content": False, "has_compaction_block": False}
    except anthropic.PermissionDeniedError as exc:
        return {"status_code": 403, "error": f"PermissionDenied: {exc}", "has_content": False, "has_compaction_block": False}
    except anthropic.BadRequestError as exc:
        # Beta header invalido ou malformado retorna 400.
        return {"status_code": 400, "error": f"BadRequest (beta invalido?): {exc}", "has_content": False, "has_compaction_block": False}
    except anthropic.APIStatusError as exc:
        return {"status_code": exc.status_code, "error": str(exc), "has_content": False, "has_compaction_block": False}


# ---------------------------------------------------------------------------
# Sessao de multiplos rounds com compaction server-side (AC0b + AC1-4)
# ---------------------------------------------------------------------------

def run_session_with_compaction(
    prompt_rounds: list[str],
    *,
    model: str = DEFAULT_MODEL,
    token: Optional[str] = None,
    compaction_threshold: float = COMPACTION_THRESHOLD,
    simulate_expiry_after_round: Optional[int] = None,
    creds_path: Optional[Path] = None,
    max_tokens_per_round: int = 1024,
) -> dict[str, Any]:
    """Roda uma sessao de multiplos rounds com compaction server-side mid-session.

    Usa `context_management` com `compact_20260112` configurado para disparar
    automaticamente quando o contexto cruza `compaction_threshold` do context window.
    O beta "compact-2026-01-12" e passado em betas= em cada request.

    A compaction e server-side: o proprio model decide quando compactar baseado
    no trigger configurado. O cliente so precisa propagar o bloco de compaction
    de volta no historico de mensagens para continuar a sessao.

    Args:
        prompt_rounds: lista de prompts para a sessao (um por round).
        model: modelo a usar (default claude-opus-4-8).
        token: token OAuth; se None, lido de env/credentials.json.
        compaction_threshold: fracao de contexto que dispara compaction (default 0.80).
        simulate_expiry_after_round: se definido, simula expiracao do token OAuth apos
            esse round para testar AC0b (refresh mid-session).
        creds_path: caminho alternativo para credentials.json (para testes).
        max_tokens_per_round: max_tokens por round normal (default 1024).

    Returns:
        dict com:
            rounds_completed: int
            compaction_count: int  — numero de rounds em que o server retornou compaction
            usage_per_round: list[dict]  — tokens por round
            total_cost_usd: float — custo estimado
            compaction_events: list[dict]  — {round, tokens_before, tokens_after, ...}
            errors: list[str]
            final_messages: list[dict]  — historico final
            token_refresh_occurred: bool  — True se refresh foi executado (AC0b)
    """
    client_ref: list[anthropic.Anthropic] = [make_oauth_client(token)]
    context_window = _CONTEXT_WINDOWS.get(model, 200_000)
    token_trigger = int(context_window * compaction_threshold)

    messages: list[dict] = []
    usage_per_round: list[dict] = []
    compaction_events: list[dict] = []
    errors: list[str] = []
    compaction_count = 0
    token_refresh_occurred = False
    total_cost_usd = 0.0

    # Configuracao de compaction server-side
    context_management_config = {
        "edits": [
            {
                "type": "compact_20260112",
                "pause_after_compaction": True,  # retorna o bloco ao cliente
                "trigger": {
                    "type": "input_tokens",
                    "value": token_trigger,
                },
                "instructions": (
                    "Preserve: (1) proximo passo do trabalho, "
                    "(2) arquivos modificados, (3) decisoes tomadas, "
                    "(4) checkpoints de .deile-progress.md"
                ),
            }
        ]
    }

    for i, prompt in enumerate(prompt_rounds):
        round_start = time.monotonic()

        # AC0b: simula expiracao de token OAuth mid-session
        if simulate_expiry_after_round is not None and i == simulate_expiry_after_round:
            logger.info("Simulando expiracao do token OAuth no round %d (AC0b)", i)
            ok = refresh_oauth_token(client_ref, creds_path=creds_path)
            if ok:
                token_refresh_occurred = True
            else:
                errors.append(f"round {i}: falha no refresh OAuth mid-session")

        messages.append({"role": "user", "content": prompt})

        try:
            response = client_ref[0].beta.messages.create(
                model=model,
                max_tokens=max_tokens_per_round,
                messages=messages,
                betas=[COMPACTION_BETA],
                context_management=context_management_config,
            )
        except anthropic.AuthenticationError as exc:
            errors.append(f"round {i}: auth error — {exc}")
            # Tenta refresh automatico antes de abortar (comportamento de producao)
            if refresh_oauth_token(client_ref, creds_path=creds_path):
                token_refresh_occurred = True
                logger.info("Auth error no round %d — token renovado, tentando novamente", i)
                try:
                    response = client_ref[0].beta.messages.create(
                        model=model,
                        max_tokens=max_tokens_per_round,
                        messages=messages,
                        betas=[COMPACTION_BETA],
                        context_management=context_management_config,
                    )
                    errors.pop()  # remove o erro se a tentativa funcionou
                except Exception as retry_exc:
                    errors.append(f"round {i}: retry apos refresh falhou — {retry_exc}")
                    break
            else:
                break
        except anthropic.APIStatusError as exc:
            errors.append(f"round {i}: API error {exc.status_code} — {exc.message}")
            break

        usage = _usage_from_response(response)
        usage_per_round.append(usage)
        total_cost_usd += _round_cost_usd(usage, model)

        # Verifica se o server retornou um bloco de compaction neste round (AC4)
        compaction_block = None
        assistant_content = []
        for block in response.content:
            if getattr(block, "type", "") == "compaction":
                compaction_block = block
                compaction_count += 1
            else:
                assistant_content.append(block)

        # Extrai texto de resposta para o historico
        assistant_text = ""
        for block in assistant_content:
            if hasattr(block, "text"):
                assistant_text += block.text

        fraction = _context_fraction(usage, context_window)
        round_elapsed_ms = (time.monotonic() - round_start) * 1000

        logger.info(
            "Round %d: input=%d output=%d fraction=%.1f%% compaction=%s elapsed=%.0fms",
            i,
            usage["input_tokens"],
            usage["output_tokens"],
            fraction * 100,
            "SIM" if compaction_block else "nao",
            round_elapsed_ms,
        )

        if compaction_block is not None:
            # Registra evento de compaction (AC4 observability)
            compaction_events.append({
                "round": i,
                "tokens_before": usage["input_tokens"],
                "tokens_after": usage["output_tokens"],
                "compaction_cost_tokens": usage["input_tokens"] + usage["output_tokens"],
                "compaction_cost_usd": _round_cost_usd(usage, model),
                "fraction_triggered": fraction,
                "latency_ms": round_elapsed_ms,
                "outcome": "success",
                "stop_reason": response.stop_reason,
            })
            # Propaga o bloco de compaction no historico (necessario para continuidade)
            # O bloco compactado substitui o historico anterior no contexto do servidor.
            messages.append({
                "role": "assistant",
                "content": response.content,  # inclui o bloco compactado
            })
        else:
            messages.append({"role": "assistant", "content": assistant_text or "[sem texto]"})

    return {
        "rounds_completed": len(usage_per_round),
        "compaction_count": compaction_count,
        "usage_per_round": usage_per_round,
        "total_cost_usd": total_cost_usd,
        "compaction_events": compaction_events,
        "errors": errors,
        "final_messages": messages,
        "token_refresh_occurred": token_refresh_occurred,
    }


# ---------------------------------------------------------------------------
# Entry point para execucao manual do spike
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    print("=" * 60)
    print("SPIKE #529 — Gate 0 (AC0): testando compaction com OAuth")
    print("=" * 60)

    result = make_one_compaction_request()
    print(f"status_code   : {result['status_code']}")
    print(f"has_content   : {result['has_content']}")
    print(f"has_compaction: {result['has_compaction_block']}")
    print(f"stop_reason   : {result.get('stop_reason')}")
    print(f"usage         : {result.get('usage')}")
    print(f"error         : {result.get('error')}")

    if result["status_code"] == 200:
        print("\nGate 0 PASSOU — OAuth + compaction beta funcionam juntos")
    else:
        print(f"\nGate 0 FALHOU — ver erro acima e registrar no SPIKE_529_REPORT.md")
