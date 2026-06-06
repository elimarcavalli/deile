"""
SPIKE — DESCARTÁVEL — Issue #529: Gate 0 OAuth × Compaction API.

Testa AC0 e AC0b:
- AC0:  uma request real com anthropic-beta: compact-2026-01-12 autenticada via
        ANTHROPIC_AUTH_TOKEN retorna HTTP 200 com conteúdo válido.
- AC0b: o harness SDK renova o token OAuth mid-session sem matar a sessão.

Marcados como @pytest.mark.integration — NÃO entram na suíte completa normal.
Pulam automaticamente se ANTHROPIC_AUTH_TOKEN não estiver definido.

Executar manualmente (no pod claude-worker com token válido):
    python3 -m pytest infra/k8s/spikes/test_compaction_oauth.py -v -m integration -p no:cov
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

# Importa o harness do spike (descartável).
# Quando rodando fora do repo, ajuste o PYTHONPATH ou use sys.path.insert.
import sys
sys.path.insert(0, str(Path(__file__).parent))
from compaction_oauth_spike import (
    COMPACTION_BETA,
    make_oauth_client,
    make_one_compaction_request,
    refresh_oauth_token,
    run_session_with_compaction,
    _get_expires_at_ms,
    _creds_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def oauth_token() -> str:
    """Retorna ANTHROPIC_AUTH_TOKEN ou pula o teste."""
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        pytest.skip(
            "ANTHROPIC_AUTH_TOKEN não definido — teste requer credenciais OAuth reais. "
            "Rodar no pod claude-worker ou com token exportado manualmente."
        )
    return token


@pytest.fixture
def tmp_creds_file(tmp_path) -> Path:
    """Cria um credentials.json temporário com token simulado."""
    creds = {
        "claudeAiOauth": {
            "accessToken": "token-simulado-para-ac0b",
            # expiresAt no passado (já expirado) — milissegundos de epoch
            "expiresAt": int((time.time() - 3600) * 1000),
        }
    }
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(creds), encoding="utf-8")
    return path


@pytest.fixture
def tmp_creds_fresh(tmp_path) -> Path:
    """Cria credentials.json com token fresco (não expirado)."""
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "token-fresco-simulado")
    creds = {
        "claudeAiOauth": {
            "accessToken": token,
            "expiresAt": int((time.time() + 3600) * 1000),
        }
    }
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(creds), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC0 — OAuth × Compaction API → HTTP 200
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_ac0_compaction_beta_with_oauth_returns_200(oauth_token):
    """AC0: request real com beta compact-2026-01-12 e OAuth → HTTP 200.

    Reprova se retornar 401/403/400 (beta inválido ou falha de auth).
    Se o beta header for recusado (400), registra no output para escalação.
    """
    result = make_one_compaction_request(token=oauth_token)

    status = result["status_code"]
    error = result.get("error")

    if status == 401:
        pytest.fail(
            f"AC0 REPROVADO — 401 Unauthorized. "
            f"O token OAuth não é aceito pela API Compaction. "
            f"Detalhe: {error}. "
            f"Ação: escalar ao autor — decisão OAuth→API key (roadmap #529)."
        )
    elif status == 403:
        pytest.fail(
            f"AC0 REPROVADO — 403 Forbidden. "
            f"O token OAuth não tem permissão para o beta '{COMPACTION_BETA}'. "
            f"Detalhe: {error}."
        )
    elif status == 400:
        pytest.fail(
            f"AC0 REPROVADO — 400 Bad Request. "
            f"Beta header '{COMPACTION_BETA}' possivelmente inválido/obsoleto. "
            f"Detalhe: {error}. "
            f"Ação: verificar valor atual do beta header e atualizar COMPACTION_BETA "
            f"em compaction_oauth_spike.py antes de prosseguir."
        )
    elif status != 200:
        pytest.fail(f"AC0 REPROVADO — status inesperado {status}: {error}")

    assert result["has_content"], "AC0 REPROVADO — response.content está vazio"
    assert result["usage"]["input_tokens"] > 0, "AC0 REPROVADO — input_tokens = 0"
    assert result["usage"]["output_tokens"] > 0, "AC0 REPROVADO — output_tokens = 0"

    print(
        f"\nAC0 APROVADO:\n"
        f"  status_code: {status}\n"
        f"  response_type: {result.get('response_type')}\n"
        f"  stop_reason: {result.get('stop_reason')}\n"
        f"  input_tokens: {result['usage']['input_tokens']}\n"
        f"  output_tokens: {result['usage']['output_tokens']}\n"
        f"  beta_header: {COMPACTION_BETA}"
    )


@pytest.mark.integration
def test_ac0_client_created_with_auth_token_not_api_key(oauth_token):
    """Garante que o client usa auth_token (OAuth), não api_key."""
    client = make_oauth_client(token=oauth_token)
    # auth_token é a propriedade OAuth; api_key é None quando usamos OAuth.
    assert client.auth_token == oauth_token, "Client deve usar auth_token OAuth"
    # Não deve ter uma api_key definida (seria x-api-key no header).
    assert not client.api_key, "Client OAuth não deve ter api_key definida"


# ---------------------------------------------------------------------------
# AC0b — Refresh do token OAuth mid-session
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_ac0b_token_refresh_mid_session_with_expired_creds(tmp_creds_file):
    """AC0b: simula expiração de token mid-session, refresh reconstrói o client.

    Usa credentials.json temporário com expiresAt no passado.
    Verifica que após refresh_oauth_token() o novo client é diferente do anterior
    (token pode ser o mesmo se ANTHROPIC_AUTH_TOKEN estiver setado, mas o client
    foi recriado — o que importa é que não morreu).

    Reprova se refresh_oauth_token() retornar False (nenhum token disponível).
    """
    # Monta client inicial com token do arquivo expirado.
    initial_token = "token-simulado-para-ac0b"

    import anthropic as _anthropic
    initial_client = _anthropic.Anthropic(auth_token=initial_token)
    client_ref = [initial_client]

    # Simula expiração: refresh deve ler o arquivo ou cair em ANTHROPIC_AUTH_TOKEN.
    # Se ANTHROPIC_AUTH_TOKEN estiver definido, o refresh vai usá-lo.
    ok = refresh_oauth_token(client_ref, creds_path=tmp_creds_file)

    assert ok, (
        "AC0b REPROVADO — refresh_oauth_token() retornou False. "
        "Nenhum token disponível após expiração simulada. "
        "Isso é um terceiro modo de morte: sessão morre por auth error mid-session. "
        "Ação: implementar refresh in-process no caminho SDK (roadmap #529)."
    )

    # O client foi substituído — sessão sobreviveu ao refresh.
    new_client = client_ref[0]
    assert new_client is not initial_client, "Client deve ser recriado após refresh"
    print(
        f"\nAC0b APROVADO (parcial — sem sessão real de 40 rounds):\n"
        f"  refresh_oauth_token() retornou True\n"
        f"  client_ref[0] foi substituído por novo client\n"
        f"  Nota: refresh usou ANTHROPIC_AUTH_TOKEN ou token do arquivo simulado."
    )


@pytest.mark.integration
def test_ac0b_session_survives_token_expiry_in_short_run(oauth_token, tmp_path):
    """AC0b (ponta a ponta curto): sessão de 3 rounds com refresh simulado no round 1.

    Rodar com ANTHROPIC_AUTH_TOKEN definido. Verifica que:
    1. Os rounds 0, 1, 2 completam sem erro de auth.
    2. O refresh no round 1 não mata a sessão.
    3. rounds_completed == 3.

    Reprova se errors contiver algum 'auth error'.
    """
    prompts = [
        "Responda 'round 0 ok' em português.",
        "Responda 'round 1 ok' em português.",
        "Responda 'round 2 ok' em português.",
    ]

    # Cria credentials.json temporário com o token real para simular o refresh.
    creds = {
        "claudeAiOauth": {
            "accessToken": oauth_token,
            "expiresAt": int((time.time() - 1) * 1000),  # já expirado
        }
    }
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(json.dumps(creds), encoding="utf-8")

    result = run_session_with_compaction(
        prompts,
        token=oauth_token,
        compaction_threshold=0.99,  # threshold alto — não disparar compaction nesses 3 rounds
        simulate_expiry_after_round=1,
        creds_path=creds_file,
    )

    auth_errors = [e for e in result["errors"] if "auth" in e.lower()]
    assert not auth_errors, (
        f"AC0b REPROVADO — erros de auth mid-session:\n"
        + "\n".join(auth_errors)
        + "\nO caminho SDK in-process perde o refresh que o subprocess claude -p fazia. "
        "Ação: implementar renovação in-process (roadmap #529)."
    )

    assert result["rounds_completed"] == 3, (
        f"AC0b REPROVADO — sessão completou {result['rounds_completed']} rounds (esperado 3). "
        f"Errors: {result['errors']}"
    )

    print(
        f"\nAC0b APROVADO (sessão curta de 3 rounds):\n"
        f"  rounds_completed: {result['rounds_completed']}\n"
        f"  errors: {result['errors']}\n"
        f"  total_cost_usd: ${result['total_cost_usd']:.4f}"
    )


# ---------------------------------------------------------------------------
# Verificação de estrutura (não requer token real)
# ---------------------------------------------------------------------------

def test_make_oauth_client_raises_without_token(monkeypatch):
    """make_oauth_client deve levantar RuntimeError se nenhum token disponível."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with patch("compaction_oauth_spike._load_token_from_credentials", return_value=None):
        with pytest.raises(RuntimeError, match="Nenhum token OAuth"):
            make_oauth_client()


def test_compaction_beta_constant_format():
    """Beta header deve seguir o formato YYYY-MM-DD."""
    import re
    assert re.match(r"^compact-\d{4}-\d{2}-\d{2}$", COMPACTION_BETA), (
        f"COMPACTION_BETA='{COMPACTION_BETA}' não segue o formato compact-YYYY-MM-DD"
    )


def test_refresh_oauth_token_returns_false_without_credentials(tmp_path, monkeypatch):
    """refresh_oauth_token retorna False quando credentials.json não existe e env vazia."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    import anthropic as _anthropic
    client_ref = [_anthropic.Anthropic(auth_token="dummy")]
    empty_path = tmp_path / "no_creds.json"
    ok = refresh_oauth_token(client_ref, creds_path=empty_path)
    assert ok is False
