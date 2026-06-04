"""Testes do Nível 3 — pipeline tenta auto-renew do OAuth antes de bloquear.

Cobre o método novo ``WorkerImplementer._try_auto_renew_oauth_and_retry``
introduzido neste PR e o módulo helper ``_claude_creds_refresh``:

  1. Refresh sucesso + retry sucesso → outcome do retry assumido (transparente).
  2. Refresh sucesso + retry retorna auth-expired de novo → propaga bloqueio.
  3. Refresh falha (refresh_token expirado) → propaga outcome original.
  4. Outcome sem ``[WORKER_AUTH_EXPIRED]`` → método é no-op (não chama refresh).
  5. ``try_refresh_claude_credentials`` rejeita lock concorrente (idempotência).

Testes do refresh helper isolados também ficam aqui (lock-file, kubectl mocks).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.orchestration.pipeline import _claude_creds_refresh as crf
from deile.orchestration.pipeline.implementer import (WorkerImplementer,
                                                       WorkOutcome,
                                                       _outcome_from_worker_response)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_expired_outcome() -> WorkOutcome:
    """Outcome como o worker retornaria com WORKER_AUTH_EXPIRED."""
    return _outcome_from_worker_response({
        "ok": False,
        "error_code": "WORKER_AUTH_EXPIRED",
        "error": "claude reportou Not logged in",
    })


def _success_outcome() -> WorkOutcome:
    return _outcome_from_worker_response({
        "ok": True,
        "summary": "implementação concluída",
        "task_id": "deadbeefcafe1234",
        "session_id": "abc-123",
    })


def _make_implementer() -> tuple[WorkerImplementer, MagicMock]:
    """Implementer com client mockado e ledger isolado."""
    client = MagicMock()
    client.dispatch = AsyncMock()
    client.get_resume_info = AsyncMock(return_value=None)
    implementer = WorkerImplementer(
        client=client,
        endpoint_override="http://test/v1/dispatch",
        ledger=MagicMock(get=MagicMock(return_value=None),
                          record=MagicMock(),
                          clear=MagicMock()),
    )
    return implementer, client


# ---------------------------------------------------------------------------
# N3.1 — método _try_auto_renew_oauth_and_retry
# ---------------------------------------------------------------------------


class TestTryAutoRenewOauthAndRetry:
    @pytest.mark.unit
    async def test_noop_when_outcome_ok(self):
        """Outcome ok=True → método retorna sem chamar refresh."""
        impl, _client = _make_implementer()
        outcome = _success_outcome()
        with patch.object(
            crf, "try_refresh_claude_credentials", new=AsyncMock(),
        ) as refresh:
            result = await impl._try_auto_renew_oauth_and_retry(
                outcome=outcome, url="x", payload={}, wait=True, stage=None,
            )
            refresh.assert_not_called()
            assert result is outcome

    @pytest.mark.unit
    async def test_noop_when_error_is_not_auth_expired(self):
        """Outcome com erro genérico não-auth → método retorna sem refresh."""
        impl, _client = _make_implementer()
        outcome = WorkOutcome(ok=False, text="", error="TIMEOUT after 30s")
        with patch.object(
            crf, "try_refresh_claude_credentials", new=AsyncMock(),
        ) as refresh:
            result = await impl._try_auto_renew_oauth_and_retry(
                outcome=outcome, url="x", payload={}, wait=True, stage=None,
            )
            refresh.assert_not_called()
            assert result is outcome

    @pytest.mark.unit
    async def test_refresh_success_then_retry_success(self):
        """Auth expirado → refresh ok → retry → retorna outcome do retry."""
        impl, client = _make_implementer()
        outcome_in = _auth_expired_outcome()
        # 2º dispatch (o retry) responde sucesso.
        client.dispatch.return_value = {
            "ok": True, "summary": "implementação concluída no retry",
            "task_id": "feedcafedead1234", "session_id": "sid-2",
        }
        with patch.object(
            crf, "try_refresh_claude_credentials",
            new=AsyncMock(return_value=crf.RefreshResult(
                ok=True, strategy="exec_inplace",
                message="Secret atualizado",
            )),
        ):
            result = await impl._try_auto_renew_oauth_and_retry(
                outcome=outcome_in,
                url="http://test/v1/dispatch",
                payload={"brief": "x"}, wait=True, stage="implement",
            )
        assert result.ok is True
        assert "concluída no retry" in result.text
        assert result.task_id == "feedcafedead1234"
        client.dispatch.assert_called_once()

    @pytest.mark.unit
    async def test_refresh_success_but_retry_still_auth_expired(self):
        """Refresh ok mas 2º dispatch volta auth expired → propaga bloqueio.

        Caso degenerado: estado inconsistente (replicas distintas, Secret
        propagação lag). Não loop — devolve o outcome do retry com o code.
        """
        impl, client = _make_implementer()
        outcome_in = _auth_expired_outcome()
        client.dispatch.return_value = {
            "ok": False, "error_code": "WORKER_AUTH_EXPIRED",
            "error": "Not logged in (ainda)",
        }
        with patch.object(
            crf, "try_refresh_claude_credentials",
            new=AsyncMock(return_value=crf.RefreshResult(ok=True)),
        ):
            result = await impl._try_auto_renew_oauth_and_retry(
                outcome=outcome_in,
                url="x", payload={"brief": "x"}, wait=True, stage="implement",
            )
        assert result.ok is False
        assert "[WORKER_AUTH_EXPIRED]" in result.error
        client.dispatch.assert_called_once()

    @pytest.mark.unit
    async def test_refresh_fails_propagates_original_block(self):
        """Refresh falha → método anota a tentativa e propaga outcome."""
        impl, client = _make_implementer()
        outcome_in = _auth_expired_outcome()
        with patch.object(
            crf, "try_refresh_claude_credentials",
            new=AsyncMock(return_value=crf.RefreshResult(
                ok=False, error="refresh_token também expirado",
            )),
        ):
            result = await impl._try_auto_renew_oauth_and_retry(
                outcome=outcome_in,
                url="x", payload={}, wait=True, stage="implement",
            )
        assert result.ok is False
        assert "[WORKER_AUTH_EXPIRED]" in result.error
        assert "auto-renew falhou" in result.error
        client.dispatch.assert_not_called()

    @pytest.mark.unit
    async def test_retry_transport_exception_returns_original(self):
        """Exceção no transporte do retry → método retorna outcome original."""
        impl, client = _make_implementer()
        outcome_in = _auth_expired_outcome()
        client.dispatch.side_effect = RuntimeError("connect timeout")
        with patch.object(
            crf, "try_refresh_claude_credentials",
            new=AsyncMock(return_value=crf.RefreshResult(ok=True)),
        ):
            result = await impl._try_auto_renew_oauth_and_retry(
                outcome=outcome_in,
                url="x", payload={}, wait=True, stage="implement",
            )
        # Recebe o outcome ORIGINAL (não cria um novo) — operador é alertado
        # pelo bloqueio normal do stage handler.
        assert result is outcome_in


# ---------------------------------------------------------------------------
# N3.2 — try_refresh_claude_credentials (helper)
# ---------------------------------------------------------------------------


class TestTryRefreshClaudeCredentials:
    @pytest.mark.unit
    async def test_lock_already_held_short_circuits(self, tmp_path, monkeypatch):
        """Lock-file fresco → retorna sem tentar nada (anti-concorrência)."""
        lock = tmp_path / "lock"
        lock.write_text(str(int(__import__("time").time())))
        monkeypatch.setenv("DEILE_CLAUDE_REFRESH_LOCK", str(lock))
        # O módulo lê o env var na constante; precisamos recarregar.
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(lock))
        result = await crf.try_refresh_claude_credentials()
        assert result.ok is False
        assert "lock" in (result.message or "").lower()

    @pytest.mark.unit
    async def test_pod_read_failure_returns_error(
        self, tmp_path, monkeypatch,
    ):
        """``kubectl exec`` retornando rc!=0 vira erro estruturado, não exception."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            return (1, "", "Error: pod not found")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)
        result = await crf.try_refresh_claude_credentials(skip_lock=True)
        assert result.ok is False
        assert "pod" in (result.error or "").lower() or "kubectl" in (result.error or "").lower()

    @pytest.mark.unit
    async def test_reactive_mode_token_in_pod_still_valid_patches_secret(
        self, tmp_path, monkeypatch,
    ):
        """Modo reativo (min_window=0): token in-pod >0s, ainda válido →
        propaga pro Secret e retorna ok."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))
        import time as _time
        fresh_exp_ms = int((_time.time() + 3600) * 1000)
        good_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "fresh-token",
                "expiresAt": fresh_exp_ms,
            }
        })

        calls: list[tuple[str, ...]] = []

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            calls.append(tuple(args))
            if "exec" in args:
                return (0, good_creds, "")
            if "create" in args:
                # dry-run produz YAML.
                return (0, "apiVersion: v1\nkind: Secret\n", "")
            if "apply" in args:
                return (0, "secret/claude-credentials configured", "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)
        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=0.0, skip_lock=True,
        )
        assert result.ok is True, result.error or result.message
        # Houve exec + create + apply
        assert any("exec" in c for c in calls)
        assert any("apply" in c for c in calls)

    @pytest.mark.unit
    async def test_reactive_mode_token_in_pod_expired_returns_error(
        self, tmp_path, monkeypatch,
    ):
        """Pod retornou token TAMBÉM expirado → refresh_token provavelmente
        expirou; propaga erro acionável."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))
        import time as _time
        # Token expirou 1h atrás.
        expired_ms = int((_time.time() - 3600) * 1000)
        expired_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "stale-token",
                "expiresAt": expired_ms,
            }
        })

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            return (0, expired_creds, "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)
        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=0.0, skip_lock=True,
        )
        assert result.ok is False
        assert "expirad" in (result.error or "").lower()

    @pytest.mark.unit
    async def test_proactive_mode_skips_when_token_well_within_window(
        self, tmp_path, monkeypatch,
    ):
        """Modo proativo (min_window=2h): token expira em 5h → skip."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))
        import time as _time
        future_ms = int((_time.time() + 5 * 3600) * 1000)
        creds_json = json.dumps({
            "claudeAiOauth": {
                "accessToken": "fresh",
                "expiresAt": future_ms,
            }
        })

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            return (0, creds_json, "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)
        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=7200, skip_lock=True,
        )
        assert result.ok is True
        assert "skip" in " ".join(result.steps).lower()

    @pytest.mark.unit
    def test_extract_expires_at_parses_keychain_format(self):
        assert crf._extract_expires_at(
            {"claudeAiOauth": {"expiresAt": 12345}}
        ) == 12345

    @pytest.mark.unit
    def test_extract_expires_at_parses_root_format(self):
        assert crf._extract_expires_at({"expiresAt": 99}) == 99

    @pytest.mark.unit
    def test_extract_expires_at_returns_none_for_missing(self):
        assert crf._extract_expires_at({}) is None
        assert crf._extract_expires_at({"claudeAiOauth": {}}) is None


# ---------------------------------------------------------------------------
# N3.3 — Integração com _dispatch (refresh chamado no path certo)
# ---------------------------------------------------------------------------


class TestDispatchIntegration:
    @pytest.mark.unit
    async def test_auth_expired_response_triggers_refresh_in_dispatch(
        self, tmp_path, monkeypatch,
    ):
        """O ``_dispatch`` chama ``_try_auto_renew_oauth_and_retry`` na cadeia
        após receber a primeira resposta. Quando o refresh + retry tem sucesso,
        o outcome final propagado pelo dispatch já é o do retry."""
        impl, client = _make_implementer()

        # 1ª resposta: auth expired; 2ª resposta (retry): sucesso.
        responses = [
            {"ok": False, "error_code": "WORKER_AUTH_EXPIRED",
             "error": "Not logged in"},
            {"ok": True, "summary": "OK no retry",
             "task_id": "babababababababa", "session_id": "sid"},
        ]
        client.dispatch.side_effect = responses
        # Bypass do cost guard / ledger irrelevantes.
        with patch.object(
            crf, "try_refresh_claude_credentials",
            new=AsyncMock(return_value=crf.RefreshResult(ok=True)),
        ):
            outcome = await impl._dispatch(
                brief="implement this",
                channel_id="pipeline-issue-99",
                persona="developer",
                stage="implement",
                branch="auto/issue-99",
                ledger_key=None,
            )
        # Resultado final é o do retry.
        assert outcome.ok is True
        assert "OK no retry" in outcome.text
        assert client.dispatch.call_count == 2
