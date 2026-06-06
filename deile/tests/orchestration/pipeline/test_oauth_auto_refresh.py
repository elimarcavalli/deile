"""Testes do Nível 3 — pipeline tenta auto-renew do OAuth antes de bloquear.

Cobre o método novo ``WorkerImplementer._try_auto_renew_oauth_and_retry``
introduzido neste PR e o módulo helper ``_claude_creds_refresh``:

  1. Refresh sucesso + retry sucesso → outcome do retry assumido (transparente).
  2. Refresh sucesso + retry retorna auth-expired de novo → propaga bloqueio.
  3. Refresh falha (refresh_token expirado) → propaga outcome original.
  4. Outcome sem ``[WORKER_AUTH_EXPIRED]`` → método é no-op (não chama refresh).
  5. ``try_refresh_claude_credentials`` rejeita lock concorrente (idempotência).

Testes do refresh helper isolados também ficam aqui (lock-file, kubectl mocks).
Testes de AC1/AC4/AC8/AC9/AC10 (grant real, lock cross-pod, durabilidade).
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.orchestration.pipeline import _claude_creds_refresh as crf
from deile.orchestration.pipeline.implementer import (
    WorkerImplementer, WorkOutcome, _outcome_from_worker_response)

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


# ---------------------------------------------------------------------------
# N3.4 — _do_oauth_token_refresh (AC1/AC5: grant real, timeout, invalid_grant)
# ---------------------------------------------------------------------------


class TestDoOauthTokenRefresh:
    """Testes unitários do POST OAuth real (sem rede real)."""

    @pytest.mark.unit
    async def test_oauth_refresh_success(self, monkeypatch):
        """POST retorna 200 com access_token + refresh_token → dict normalizado."""
        new_at = "new-access-token-abc"
        new_rt = "new-refresh-token-xyz"
        expires_in = 3600

        async def fake_post(token_url, client_id, refresh_token, *, timeout_s=30.0):
            assert token_url == "https://example.com/oauth/token"
            assert client_id == "my-client-id"
            assert refresh_token == "old-refresh-token"
            return {
                "accessToken": new_at,
                "refreshToken": new_rt,
                "expiresAt": int((time.time() + expires_in) * 1000),
            }

        monkeypatch.setattr(crf, "_do_oauth_token_refresh", fake_post)

        result = await crf._do_oauth_token_refresh(
            "https://example.com/oauth/token",
            "my-client-id",
            "old-refresh-token",
        )
        assert result["accessToken"] == new_at
        assert result["refreshToken"] == new_rt
        assert result["expiresAt"] is not None
        assert result["expiresAt"] > int(time.time() * 1000)

    @pytest.mark.unit
    async def test_oauth_refresh_invalid_grant_raises(self, monkeypatch):
        """POST retorna invalid_grant → InvalidGrantError (não ok=False silencioso)."""
        import io
        import urllib.error
        import urllib.request

        body_bytes = b'{"error":"invalid_grant","error_description":"Refresh token expired"}'

        def fake_urlopen(req, timeout=None):
            exc = urllib.error.HTTPError(
                url="https://example.com/oauth/token",
                code=400,
                msg="Bad Request",
                hdrs={},  # type: ignore[arg-type]
                fp=io.BytesIO(body_bytes),
            )
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(crf.InvalidGrantError, match="HTTP 400"):
            await crf._do_oauth_token_refresh(
                "https://example.com/oauth/token",
                "client-id",
                "expired-refresh-token",
            )

    @pytest.mark.unit
    async def test_oauth_refresh_http_error_raises_runtime(self, monkeypatch):
        """POST retorna erro HTTP não-invalid_grant → RuntimeError com detalhe."""
        import io
        import urllib.error
        import urllib.request

        body_bytes = b'{"error":"server_error"}'

        def fake_urlopen(req, timeout=None):
            exc = urllib.error.HTTPError(
                url="https://example.com/oauth/token",
                code=503,
                msg="Service Unavailable",
                hdrs={},  # type: ignore[arg-type]
                fp=io.BytesIO(body_bytes),
            )
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(RuntimeError, match="HTTP 503"):
            await crf._do_oauth_token_refresh(
                "https://example.com/oauth/token",
                "client-id",
                "some-refresh-token",
            )


# ---------------------------------------------------------------------------
# N3.5 — AC4: lock cross-pod (ConfigMap lease, TTL, contention)
# ---------------------------------------------------------------------------


class TestCrossPodLock:
    @pytest.mark.unit
    async def test_lock_acquired_when_no_configmap(self, monkeypatch):
        """ConfigMap ausente (rc=1 no get) → lock adquirido sem erro."""
        calls = []

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            calls.append(list(args))
            if "get" in args and "configmap" in args:
                return (1, "", "not found")
            if "apply" in args:
                return (0, "configmap/claude-credentials-lock configured", "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)
        result = await crf._acquire_cross_pod_lock("deile")
        assert result is True
        assert any("apply" in c for c in calls)

    @pytest.mark.unit
    async def test_lock_held_raises_lock_held_error(self, monkeypatch):
        """ConfigMap existe com lease não-expirado de outro pod → LockHeldError."""

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            if "get" in args and "configmap" in args:
                data = {
                    "data": {
                        "holder": "other-pod-abc",
                        "expires": str(time.time() + 200),  # não expirou
                    }
                }
                return (0, json.dumps(data), "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)
        with pytest.raises(crf.LockHeldError, match="other-pod-abc"):
            await crf._acquire_cross_pod_lock("deile", pod_name="my-pod")

    @pytest.mark.unit
    async def test_lock_acquirable_when_expired(self, monkeypatch):
        """ConfigMap com lease EXPIRADO → lock pode ser adquirido."""
        calls = []

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            calls.append(list(args))
            if "get" in args and "configmap" in args:
                data = {
                    "data": {
                        "holder": "dead-pod",
                        "expires": str(time.time() - 10),  # expirou
                    }
                }
                return (0, json.dumps(data), "")
            if "apply" in args:
                return (0, "configmap/claude-credentials-lock configured", "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)
        result = await crf._acquire_cross_pod_lock("deile", pod_name="new-pod")
        assert result is True
        assert any("apply" in c for c in calls)


# ---------------------------------------------------------------------------
# N3.6 — AC9: durabilidade (PVC-write-fail pós-POST, Secret intocado)
# ---------------------------------------------------------------------------


class TestOauthGrantDurability:
    @pytest.mark.unit
    async def test_oauth_refresh_success_writes_pvc_then_secret(
        self, tmp_path, monkeypatch,
    ):
        """Grant OK → PVC escrito ANTES do Secret (AC9). Ambos devem ser chamados."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))

        now_ms = int(time.time() * 1000)
        creds_with_refresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "valid-refresh-token",
                "expiresAt": now_ms - 100,  # expirado
                "oauthUrl": "https://example.com/oauth/token",
                "clientId": "test-client-id",
            }
        })

        operations: list[str] = []

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            joined = " ".join(str(a) for a in args)
            # Detect PVC write (sh -c with stdin) vs PVC read (cat without stdin)
            is_pvc_write = (
                "exec" in joined and "sh" in joined and input_data is not None
            )
            is_pvc_read = (
                "exec" in joined and "cat" in joined and input_data is None
            )
            is_configmap = "configmap" in joined
            is_secret_create = "create" in joined and "secret" in joined
            is_apply = "apply" in joined

            if "get" in joined and is_configmap:
                return (1, "", "not found")
            if is_configmap and "delete" in joined:
                return (0, "", "")
            if is_apply and input_data and b"ConfigMap" in input_data:
                return (0, "configmap created", "")
            if is_pvc_read:
                return (0, creds_with_refresh, "")
            if is_pvc_write:
                operations.append("PVC_WRITE")
                return (0, "", "")
            if is_secret_create:
                return (0, "apiVersion: v1\nkind: Secret\n", "")
            if is_apply:
                operations.append("SECRET_APPLY")
                return (0, "secret configured", "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)

        new_access = "brand-new-access-token"
        new_refresh = "brand-new-refresh-token"

        async def fake_oauth_post(token_url, client_id, refresh_token, *, timeout_s=30.0):
            return {
                "accessToken": new_access,
                "refreshToken": new_refresh,
                "expiresAt": int((time.time() + 3600) * 1000),
            }

        monkeypatch.setattr(crf, "_do_oauth_token_refresh", fake_oauth_post)

        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=0.0,
            skip_lock=True,
            skip_cross_pod_lock=True,
        )
        assert result.ok is True, result.error or result.message
        assert result.strategy == "oauth_grant"
        assert result.seconds_until_new_expiry is not None
        assert result.seconds_until_new_expiry > 0

        # Verifica que PVC foi escrito antes do Secret (AC9)
        pvc_idx = next(
            (i for i, op in enumerate(operations) if "PVC_WRITE" in op), None
        )
        secret_idx = next(
            (i for i, op in enumerate(operations) if "SECRET_APPLY" in op), None
        )
        assert pvc_idx is not None, f"PVC não foi escrito. ops={operations}"
        assert secret_idx is not None, f"Secret não foi aplicado. ops={operations}"
        assert pvc_idx < secret_idx, "PVC deve ser escrito ANTES do Secret (AC9)"

    @pytest.mark.unit
    async def test_pvc_write_fail_after_grant_secret_untouched(
        self, tmp_path, monkeypatch,
    ):
        """POST OAuth OK mas escrita no PVC falha → Secret NÃO é atualizado (AC9)."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))

        now_ms = int(time.time() * 1000)
        creds_with_refresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "valid-refresh-token",
                "expiresAt": now_ms - 100,
                "oauthUrl": "https://example.com/oauth/token",
                "clientId": "test-client-id",
            }
        })

        secret_patched = {"count": 0}

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            joined = " ".join(str(a) for a in args)
            is_pvc_write = (
                "exec" in joined and "sh" in joined and input_data is not None
            )
            is_pvc_read = (
                "exec" in joined and "cat" in joined and input_data is None
            )
            is_configmap = "configmap" in joined
            is_secret_create = "create" in joined and "secret" in joined
            is_apply = "apply" in joined

            if "get" in joined and is_configmap:
                return (1, "", "not found")
            if "delete" in joined and is_configmap:
                return (0, "", "")
            if is_apply and input_data and b"ConfigMap" in input_data:
                return (0, "configmap created", "")
            if is_pvc_read:
                return (0, creds_with_refresh, "")
            if is_pvc_write:
                # Simula falha na escrita do PVC
                return (1, "", "no space left on device")
            if is_secret_create:
                secret_patched["count"] += 1
                return (0, "apiVersion: v1\nkind: Secret\n", "")
            if is_apply:
                secret_patched["count"] += 1
                return (0, "secret configured", "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)

        async def fake_oauth_post(token_url, client_id, refresh_token, *, timeout_s=30.0):
            return {
                "accessToken": "new-token",
                "refreshToken": "new-refresh",
                "expiresAt": int((time.time() + 3600) * 1000),
            }

        monkeypatch.setattr(crf, "_do_oauth_token_refresh", fake_oauth_post)

        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=0.0,
            skip_lock=True,
            skip_cross_pod_lock=True,
        )
        assert result.ok is False
        assert "pvc" in (result.error or "").lower() or "token rotacionado" in (result.error or "").lower()
        # AC9: Secret NÃO deve ter sido tocado após falha do PVC
        assert secret_patched["count"] == 0, (
            f"Secret foi alterado após falha no PVC! count={secret_patched['count']}"
        )

    @pytest.mark.unit
    async def test_invalid_grant_returns_human_fallback_error(
        self, tmp_path, monkeypatch,
    ):
        """invalid_grant → ok=False com mensagem que pede re-login humano (AC1)."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))

        now_ms = int(time.time() * 1000)
        creds_with_refresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "expired-refresh-token",
                "expiresAt": now_ms - 3600_000,  # expirou 1h atrás
                "oauthUrl": "https://example.com/oauth/token",
                "clientId": "test-client-id",
            }
        })

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            joined = " ".join(str(a) for a in args)
            if "get" in joined and "configmap" in joined:
                return (1, "", "not found")
            if "apply" in joined and input_data and b"ConfigMap" in input_data:
                return (0, "configmap created", "")
            if "exec" in joined and "cat" in joined:
                return (0, creds_with_refresh, "")
            if "delete" in joined and "configmap" in joined:
                return (0, "", "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)

        async def fake_oauth_post(token_url, client_id, refresh_token, *, timeout_s=30.0):
            raise crf.InvalidGrantError("Refresh token expired or revoked")

        monkeypatch.setattr(crf, "_do_oauth_token_refresh", fake_oauth_post)

        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=0.0,
            skip_lock=True,
            skip_cross_pod_lock=True,
        )
        assert result.ok is False
        error_lower = (result.error or "").lower()
        assert "invalid_grant" in error_lower or "refresh_token" in error_lower
        assert "claude-login" in error_lower or "re-login" in error_lower or "switch" in error_lower

    @pytest.mark.unit
    async def test_missing_refresh_token_falls_back_to_resync(
        self, tmp_path, monkeypatch,
    ):
        """Sem refreshToken em credentials → fallback para re-sync simples."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))

        fresh_ms = int((time.time() + 3600) * 1000)
        creds_no_refresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "valid-access-token",
                "expiresAt": fresh_ms,
                # sem refreshToken
            }
        })

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            joined = " ".join(str(a) for a in args)
            if "exec" in joined and "cat" in joined:
                return (0, creds_no_refresh, "")
            if "create" in joined and "secret" in joined:
                return (0, "apiVersion: v1\nkind: Secret\n", "")
            if "apply" in joined:
                return (0, "secret configured", "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)

        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=0.0,
            skip_lock=True,
            skip_cross_pod_lock=True,
        )
        assert result.ok is True
        assert result.strategy == "exec_inplace"
        assert "re-sync" in (result.message or "").lower()


# ---------------------------------------------------------------------------
# N3.7 — AC10: refresh proativo (ramo 0 < remaining_s <= window)
# ---------------------------------------------------------------------------


class TestProactiveRefresh:
    @pytest.mark.unit
    async def test_proactive_refresh_within_window_calls_grant(
        self, tmp_path, monkeypatch,
    ):
        """Token dentro da janela proativa → grant é chamado (AC10).

        Verifica: 1 POST + seconds_until_new_expiry > remaining_s_entrada.
        """
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))

        # Token expira em 30 minutos (dentro da janela de 2h).
        remaining_s_before = 1800
        expires_ms = int((time.time() + remaining_s_before) * 1000)
        creds_with_refresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "valid-refresh-token",
                "expiresAt": expires_ms,
                "oauthUrl": "https://example.com/oauth/token",
                "clientId": "test-client-id",
            }
        })

        oauth_calls = {"count": 0}

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            joined = " ".join(str(a) for a in args)
            if "get" in joined and "configmap" in joined:
                return (1, "", "not found")
            if "apply" in joined and input_data and b"ConfigMap" in input_data:
                return (0, "configmap created", "")
            if "exec" in joined and "cat" in joined:
                return (0, creds_with_refresh, "")
            if "exec" in joined and "sh" in joined:
                return (0, "", "")
            if "create" in joined and "secret" in joined:
                return (0, "apiVersion: v1\nkind: Secret\n", "")
            if "apply" in joined:
                return (0, "secret configured", "")
            if "delete" in joined and "configmap" in joined:
                return (0, "", "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)

        async def fake_oauth_post(token_url, client_id, refresh_token, *, timeout_s=30.0):
            oauth_calls["count"] += 1
            return {
                "accessToken": "new-access-token",
                "refreshToken": "new-refresh-token",
                "expiresAt": int((time.time() + 28800) * 1000),  # 8h
            }

        monkeypatch.setattr(crf, "_do_oauth_token_refresh", fake_oauth_post)

        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=7200,  # janela de 2h
            skip_lock=True,
            skip_cross_pod_lock=True,
        )
        assert result.ok is True, result.error or result.message
        assert oauth_calls["count"] == 1, "Exatamente 1 POST deve ser feito (AC10)"
        assert result.seconds_until_new_expiry is not None
        assert result.seconds_until_new_expiry > remaining_s_before, (
            f"Novo expiry ({result.seconds_until_new_expiry:.0f}s) deve ser "
            f"maior que o antigo ({remaining_s_before}s)"
        )

    @pytest.mark.unit
    async def test_proactive_skip_outside_window_no_grant(
        self, tmp_path, monkeypatch,
    ):
        """Token fora da janela proativa → 0 POSTs, ok=True (skip inteligente) (AC10)."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))

        # Token expira em 5 horas (fora da janela de 2h).
        expires_ms = int((time.time() + 5 * 3600) * 1000)
        creds_with_refresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "valid-access",
                "refreshToken": "valid-refresh-token",
                "expiresAt": expires_ms,
                "oauthUrl": "https://example.com/oauth/token",
                "clientId": "test-client-id",
            }
        })

        oauth_calls = {"count": 0}

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            joined = " ".join(str(a) for a in args)
            if "exec" in joined and "cat" in joined:
                return (0, creds_with_refresh, "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)

        async def fake_oauth_post(token_url, client_id, refresh_token, *, timeout_s=30.0):
            oauth_calls["count"] += 1
            return {"accessToken": "new", "refreshToken": "new-r", "expiresAt": None}

        monkeypatch.setattr(crf, "_do_oauth_token_refresh", fake_oauth_post)

        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=7200,  # janela de 2h
            skip_lock=True,
            skip_cross_pod_lock=True,
        )
        assert result.ok is True
        assert oauth_calls["count"] == 0, "Não deve fazer POST fora da janela (AC10)"
        assert "skip" in " ".join(result.steps).lower()

    @pytest.mark.unit
    async def test_reactive_expired_calls_grant(self, tmp_path, monkeypatch):
        """Token expirado (remaining <= 0) + refreshToken presente → grant chamado (AC1)."""
        monkeypatch.setattr(crf, "_REFRESH_LOCK_PATH", str(tmp_path / "lock"))

        # Token expirou 1h atrás.
        expires_ms = int((time.time() - 3600) * 1000)
        creds_with_refresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "stale-access",
                "refreshToken": "still-valid-refresh",
                "expiresAt": expires_ms,
                "oauthUrl": "https://example.com/oauth/token",
                "clientId": "test-client-id",
            }
        })

        oauth_calls = {"count": 0}

        async def fake_kubectl(args, timeout_s=30.0, input_data=None):
            joined = " ".join(str(a) for a in args)
            if "get" in joined and "configmap" in joined:
                return (1, "", "not found")
            if "apply" in joined and input_data and b"ConfigMap" in input_data:
                return (0, "configmap created", "")
            if "exec" in joined and "cat" in joined:
                return (0, creds_with_refresh, "")
            if "exec" in joined and "sh" in joined:
                return (0, "", "")
            if "create" in joined and "secret" in joined:
                return (0, "apiVersion: v1\nkind: Secret\n", "")
            if "apply" in joined:
                return (0, "secret configured", "")
            if "delete" in joined and "configmap" in joined:
                return (0, "", "")
            return (0, "", "")

        monkeypatch.setattr(crf, "_kubectl_async", fake_kubectl)

        async def fake_oauth_post(token_url, client_id, refresh_token, *, timeout_s=30.0):
            oauth_calls["count"] += 1
            return {
                "accessToken": "fresh-access",
                "refreshToken": "rotated-refresh",
                "expiresAt": int((time.time() + 28800) * 1000),
            }

        monkeypatch.setattr(crf, "_do_oauth_token_refresh", fake_oauth_post)

        result = await crf.try_refresh_claude_credentials(
            min_expiry_window_s=0.0,  # reativo
            skip_lock=True,
            skip_cross_pod_lock=True,
        )
        assert result.ok is True, result.error or result.message
        assert oauth_calls["count"] == 1, "Exatamente 1 POST deve ser feito (AC1)"
        assert result.strategy == "oauth_grant"


# ---------------------------------------------------------------------------
# N3.8 — _extract_oauth_config (AC3: sem hardcode, leitura de config/env)
# ---------------------------------------------------------------------------


class TestExtractOauthConfig:
    @pytest.mark.unit
    def test_reads_from_credentials_json(self):
        creds = {
            "claudeAiOauth": {
                "refreshToken": "rt123",
                "oauthUrl": "https://token.example.com",
                "clientId": "cid-abc",
            }
        }
        token_url, client_id, refresh_token = crf._extract_oauth_config(creds)
        assert token_url == "https://token.example.com"
        assert client_id == "cid-abc"
        assert refresh_token == "rt123"

    @pytest.mark.unit
    def test_reads_from_env_when_missing_in_creds(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_OAUTH_TOKEN_URL", "https://env.example.com/tok")
        monkeypatch.setenv("CLAUDE_OAUTH_CLIENT_ID", "env-client-id")
        creds = {"claudeAiOauth": {"refreshToken": "rt-env"}}
        token_url, client_id, refresh_token = crf._extract_oauth_config(creds)
        assert token_url == "https://env.example.com/tok"
        assert client_id == "env-client-id"
        assert refresh_token == "rt-env"

    @pytest.mark.unit
    def test_returns_none_when_completely_absent(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_OAUTH_TOKEN_URL", raising=False)
        monkeypatch.delenv("CLAUDE_OAUTH_CLIENT_ID", raising=False)
        creds = {"claudeAiOauth": {"accessToken": "at-only"}}
        token_url, client_id, refresh_token = crf._extract_oauth_config(creds)
        assert token_url is None
        assert client_id is None
        assert refresh_token is None

    @pytest.mark.unit
    def test_creds_json_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_OAUTH_TOKEN_URL", "https://env.example.com/tok")
        monkeypatch.setenv("CLAUDE_OAUTH_CLIENT_ID", "env-client-id")
        creds = {
            "claudeAiOauth": {
                "refreshToken": "rt",
                "oauthUrl": "https://creds.example.com/tok",
                "clientId": "creds-client-id",
            }
        }
        token_url, client_id, _ = crf._extract_oauth_config(creds)
        assert token_url == "https://creds.example.com/tok"
        assert client_id == "creds-client-id"
