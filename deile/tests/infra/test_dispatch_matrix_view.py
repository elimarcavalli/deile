"""DispatchMatrixView render + skeleton handlers (#309 fase 2 Task 18).

Cobertura mínima do skeleton da nova view unificada que (na Task 21) substitui
``DispatchModeView`` (PR #330, global flip) + ``StageModelsView`` (#305,
per-stage model). Esta task cobre apenas:

- Render: 5 stages + linha "Global default" + header com status do
  ``claude-worker`` (logado como email / NÃO INSTALADO).
- Navegação básica: ↑↓ row, ←→ col, q → back.

Pickers / actions ([enter] edit, [r] reset, [L] login switch, [I] install)
ficam como STUBS implementados nas Tasks 19-20.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# infra/k8s não é um package — adicionar a sys.path para ``_panel_data`` /
# ``_panel`` resolverem (mirror de como ``deploy.py panel`` invoca o módulo).
_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))


@pytest.fixture
def mock_data_with_claude():
    """PanelData mockada com StageDispatchProvider que reporta claude-worker
    instalado + ready + email."""
    data = MagicMock()
    from _panel_data import ClaudeWorkerStatus, StageDispatchEntry

    data.stage_dispatch.get_all_stages.return_value = [
        StageDispatchEntry("classify", "deile-worker", "anthropic:haiku", "env"),
        StageDispatchEntry("refine", "deile-worker", "anthropic:sonnet", "env"),
        StageDispatchEntry(
            "implement", "claude-worker", "anthropic:claude-opus-4-8", "env"
        ),
        StageDispatchEntry(
            "pr_review", "claude-worker", "anthropic:claude-opus-4-8", "env"
        ),
        StageDispatchEntry("follow_ups", "deile-worker", None, "default"),
    ]
    data.stage_dispatch.get_claude_worker_status.return_value = ClaudeWorkerStatus(
        deployment_applied=True,
        pod_ready=True,
        logged_in_email="user@example.com",
    )
    data.namespace = "deile"
    return data


@pytest.fixture
def mock_data_no_claude():
    """PanelData mockada com claude-worker NÃO INSTALADO — todos os stages
    caem em default deile-worker."""
    data = MagicMock()
    from _panel_data import ClaudeWorkerStatus, StageDispatchEntry

    data.stage_dispatch.get_all_stages.return_value = [
        StageDispatchEntry(s, "deile-worker", None, "default")
        for s in ("classify", "refine", "implement", "pr_review", "follow_ups")
    ]
    data.stage_dispatch.get_claude_worker_status.return_value = ClaudeWorkerStatus(
        deployment_applied=False,
        pod_ready=False,
        logged_in_email=None,
    )
    data.namespace = "deile"
    return data


def test_view_renders_5_stages(mock_data_with_claude):
    from _panel import DispatchMatrixView
    from rich.console import Console

    view = DispatchMatrixView(data=mock_data_with_claude)
    console = Console(width=140, record=True)
    rendered = view.render(None)
    console.print(rendered)
    text = console.export_text()

    # All 5 stages visible.
    for stage in ("classify", "refine", "implement", "pr_review", "follow_ups"):
        assert stage in text, f"stage {stage} missing from render"

    # Worker values visible.
    assert "claude-worker" in text
    assert "deile-worker" in text

    # Pelo menos um model slug visível.
    assert "anthropic:claude-opus-4-8" in text or "claude-opus-4-8" in text


def test_view_shows_claude_worker_ready_in_header(mock_data_with_claude):
    from _panel import DispatchMatrixView
    from rich.console import Console

    view = DispatchMatrixView(data=mock_data_with_claude)
    console = Console(width=140, record=True)
    console.print(view.render(None))
    text = console.export_text()

    # Email logado deve aparecer (ou pelo menos a palavra "user").
    assert "user@example.com" in text or "user" in text.lower()
    # Status do pod deve aparecer como ready/ok.
    assert "ready" in text.lower() or "ok" in text.lower()


def test_view_shows_install_hint_when_claude_absent(mock_data_no_claude):
    from _panel import DispatchMatrixView
    from rich.console import Console

    view = DispatchMatrixView(data=mock_data_no_claude)
    console = Console(width=140, record=True)
    console.print(view.render(None))
    text = console.export_text()

    # Deve hintar a action de install — "instalar" PT ou "install" EN ou "[I]".
    assert (
        "instalar" in text.lower() or "install" in text.lower() or "[i]" in text.lower()
    )


def test_view_navigation_keys_update_cursor(mock_data_with_claude):
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)

    # Cursor inicial em (0, 0).
    assert view.cursor_row == 0
    assert view.cursor_col == 0

    # DOWN: row+1
    view.handle_key("DOWN", MagicMock())
    assert view.cursor_row == 1

    # RIGHT: col+1
    view.handle_key("RIGHT", MagicMock())
    assert view.cursor_col == 1

    # UP + LEFT back.
    view.handle_key("UP", MagicMock())
    view.handle_key("LEFT", MagicMock())
    assert view.cursor_row == 0
    assert view.cursor_col == 0


def test_q_returns_to_dashboard(mock_data_with_claude):
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    result = view.handle_key("q", MagicMock())

    # Deve sinalizar nav back para dashboard (ou back/quit). ActionResult
    # não pode ser None — qualquer das três é aceitável para o skeleton.
    from _panel import Action

    assert result is not None
    assert result.kind in (Action.NAV, Action.BACK, Action.QUIT)


# ===========================================================================
# Task 19 — pickers contextuais Worker + Model
# ===========================================================================


def test_worker_picker_options(mock_data_with_claude):
    """Worker picker mostra deile-worker, claude-worker, (global default)."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    options = view._worker_picker_options()
    assert "deile-worker" in options
    assert "claude-worker" in options
    assert any("global" in o.lower() or "default" in o.lower() for o in options)


def test_worker_picker_offers_all_fleet_dispatchers(mock_data_with_claude):
    """Frente 1: picker de Worker oferece os 7 dispatchers válidos.

    Deriva de ``dispatch_resolver.get_valid_dispatchers`` (núcleo + frota CLI
    descoberta no registro de adapters) — não uma lista hardcoded.
    """
    from _panel import DispatchMatrixView

    from deile.orchestration.pipeline.dispatch_resolver import get_valid_dispatchers

    view = DispatchMatrixView(data=mock_data_with_claude)
    options = view._worker_picker_options()
    for dispatcher in get_valid_dispatchers():
        assert dispatcher in options, f"{dispatcher} ausente do picker de worker"
    # Os 5 workers da frota CLI + 2 núcleo = 7 (mais o sentinela global).
    assert "codex-worker" in options
    assert "opencode-worker" in options
    assert "qwen-worker" in options
    assert "aider-worker" in options
    assert "goose-worker" in options


def test_model_picker_for_cli_worker_uses_adapter_catalog(mock_data_with_claude):
    """Frente 2: worker da frota CLI → model picker mostra ids do adapter.

    Os valores das opções são os ids NATIVOS (bare) que vão pro env; os
    rótulos enriquecidos (preço/auth) ficam em ``_option_labels``.
    """
    import cli_adapters
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    options = view._model_picker_options(worker="codex-worker")
    adapter_ids = {m.id for m in cli_adapters.ADAPTERS["codex"].list_models()}
    option_ids = {o for o in options if o in adapter_ids}
    assert option_ids == adapter_ids, "picker não cobre o catálogo do codex"
    # gpt-5.3-codex deve aparecer com label enriquecido (preço + auth chatgpt).
    label = view._option_labels.get("gpt-5.3-codex", "")
    assert "$" in label and "chatgpt" in label, f"label enriquecido ausente: {label!r}"


def test_model_picker_apply_uses_bare_id_not_label(mock_data_with_claude):
    """Frente 2: o valor aplicado é o id bare, NÃO o label decorado."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    options = view._model_picker_options(worker="codex-worker")
    # Toda opção que tem label enriquecido deve, ela mesma, ser um id bare
    # (sem "$", sem "[", sem "  — ") — só o display é decorado.
    for opt in options:
        if opt in view._option_labels:
            assert (
                "$" not in opt and "[" not in opt and "— " not in opt
            ), f"opção {opt!r} carrega decoração — apply enviaria valor sujo"


def test_model_picker_restricted_when_claude_worker(mock_data_with_claude):
    """Worker=claude-worker → model picker só anthropic:* + (default)."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    # Faz `_load_all_models` usar o fallback estático com providers múltiplos.
    options = view._model_picker_options(worker="claude-worker")

    # Toda opção (exceto sentinelas de "default/clear") deve ser anthropic:*.
    for opt in options:
        is_anthropic = opt.startswith("anthropic:")
        is_sentinel = "default" in opt.lower() or "clear" in opt.lower()
        assert (
            is_anthropic or is_sentinel
        ), f"option {opt!r} não é anthropic-eligible nem sentinela"
    # Pelo menos um anthropic concreto deve existir.
    assert any(
        o.startswith("anthropic:") for o in options
    ), "picker restrito não tem nenhum anthropic:* concreto"


def test_model_picker_open_when_deile_worker(mock_data_with_claude):
    """Worker=deile-worker → model picker mostra TODOS providers."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    options = view._model_picker_options(worker="deile-worker")

    # Pelo menos 2 providers diferentes devem aparecer (ex: anthropic,
    # openai, deepseek, google) — confirma que não há restrição.
    providers = {opt.split(":", 1)[0] for opt in options if ":" in opt}
    assert (
        len(providers) >= 2
    ), f"só providers {providers} no picker — esperado múltiplos"


def test_enter_on_worker_column_routes_to_worker_picker(mock_data_with_claude):
    """[enter] na coluna 0 (Worker) deve mudar para estado de picker de worker."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view.cursor_row = 0  # primeira stage
    view.cursor_col = 0  # coluna Worker
    result = view.handle_key("\r", MagicMock())

    # Picker abriu — view tem estado modal ativo (mode != None).
    assert view.mode is not None
    assert view.mode[0] == "worker"
    # ActionResult não é NOOP (algo aconteceu).
    from _panel import Action

    assert result.kind != Action.NOOP


def test_enter_on_model_column_routes_to_model_picker(mock_data_with_claude):
    """[enter] na coluna 1 (Model) deve abrir picker de model."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view.cursor_row = 0
    view.cursor_col = 1  # coluna Model
    result = view.handle_key("\r", MagicMock())

    assert view.mode is not None
    assert view.mode[0] == "model"
    from _panel import Action

    assert result.kind != Action.NOOP


def test_enter_on_global_row_opens_global_picker(mock_data_with_claude):
    """[enter] na linha 'Global default' (cursor_row == N) abre picker global."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    # Mover para a linha "Global default" (índice == len(stages)).
    view.cursor_row = 5  # 5 stages → global row at index 5
    view.cursor_col = 0
    result = view.handle_key("\r", MagicMock())

    # Picker abriu — estado modal ativo, e a flag global indica.
    assert view.mode is not None
    assert view.mode[0] in ("global_worker", "global_model")
    from _panel import Action

    assert result.kind != Action.NOOP


def test_esc_in_picker_closes_picker_modal(mock_data_with_claude):
    """ESC dentro do picker fecha o modal sem aplicar nada."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view.cursor_row = 0
    view.cursor_col = 0
    view.handle_key("\r", MagicMock())  # abre worker picker
    assert view.mode is not None

    view.handle_key("ESC", MagicMock())  # fecha
    assert view.mode is None


# ===========================================================================
# Task 20 — [I] install-on-the-fly + [L] switch-login modals
# ===========================================================================


def test_i_key_triggers_install_modal_when_claude_absent(
    mock_data_no_claude,
    monkeypatch,
):
    """[I] quando claude-worker NÃO instalado → mostra modal de install."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_no_claude)
    result = view.handle_key("I", MagicMock())

    # State: modal de install aberto (via mode state).
    assert view.mode is not None and "install" in str(view.mode).lower(), (
        f"expected install modal triggered; view.mode={view.mode}, " f"result={result}"
    )


def test_i_key_noop_when_claude_already_installed(mock_data_with_claude):
    """[I] quando claude-worker JÁ instalado → noop ou warning."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    initial_mode = getattr(view, "mode", None)
    view.handle_key("I", MagicMock())
    # Não deve abrir install modal (claude já está rodando)
    assert view.mode == initial_mode or (
        view.mode and "install" not in str(view.mode).lower()
    )


def test_l_key_triggers_switch_login_when_claude_installed(
    mock_data_with_claude,
):
    """[L] quando claude-worker instalado → modal de switch login com email atual."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    result = view.handle_key("L", MagicMock())

    assert view.mode is not None and (
        "login" in str(view.mode).lower() or "switch" in str(view.mode).lower()
    ), (
        f"expected switch-login modal triggered; view.mode={view.mode}, "
        f"result={result}"
    )


def test_l_key_noop_when_claude_not_installed(mock_data_no_claude):
    """[L] quando claude-worker NÃO instalado → noop (não pode switch sem install)."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_no_claude)
    initial_mode = getattr(view, "mode", None)
    view.handle_key("L", MagicMock())

    # Não pode abrir switch login se não há claude
    assert (
        view.mode == initial_mode
        or "login"
        not in str(
            view.mode or "",
        ).lower()
    )


def test_selecting_claude_worker_when_absent_triggers_install_flow(
    mock_data_no_claude,
    monkeypatch,
):
    """Selecionar claude-worker no picker quando Deployment absent →
    install antes de persist."""
    import _claude_install
    from _panel import DispatchMatrixView

    # Mock bootstrap_claude_worker para não chamar kubectl real.
    bootstrap_called = {"flag": False, "kwargs": None}

    def fake_bootstrap(**kwargs):
        bootstrap_called["flag"] = True
        bootstrap_called["kwargs"] = kwargs
        return _claude_install.ClaudeLoginResult(
            ok=True,
            account_email="user@new.com",
            secret_applied=True,
            deployment_applied=True,
            rollout_ready=True,
        )

    monkeypatch.setattr(
        _claude_install,
        "bootstrap_claude_worker",
        fake_bootstrap,
    )

    view = DispatchMatrixView(data=mock_data_no_claude)

    # Simulate selecting claude-worker via apply selection.
    if hasattr(view, "_on_worker_selected"):
        view._on_worker_selected("implement", "claude-worker")

    # Após install confirm flow, bootstrap deve ter sido chamado.
    # Aceitamos: ou bootstrap foi chamado, OU modal de confirm está aberto.
    assert bootstrap_called["flag"] is True or (
        view.mode and "install" in str(view.mode).lower()
    ), (
        f"esperado bootstrap chamado ou modal aberto; "
        f"called={bootstrap_called['flag']}, mode={view.mode}"
    )


# ============================================================================
# Issue #603 — [T] login via setup-token (token ~1 ano)
# ============================================================================


def test_t_key_opens_setup_token_modal(mock_data_with_claude):
    """``[T]`` abre o modal de confirmação Y/N do setup-token (issue #603)."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    result = view.handle_key("T", MagicMock())

    assert view.mode is not None and view.mode[0] == "setup_token_confirm", (
        f"expected setup_token modal triggered; view.mode={view.mode}, "
        f"result={result}"
    )


def test_t_key_blocked_when_install_in_progress(mock_data_with_claude):
    """``[T]`` enquanto install/login em background → mensagem de bloqueio."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view._install_in_progress = True
    view.handle_key("T", MagicMock())

    assert view.mode is None  # não abriu modal
    assert "em andamento" in view.last_msg.lower()


def test_setup_token_confirm_triggers_perform_setup_token(
    mock_data_with_claude,
    monkeypatch,
):
    """Confirmar (Y) o modal de setup-token dispara ``setup_token_claude_worker``
    via thread daemon (espelho do fluxo [L]/bootstrap)."""
    import _claude_install
    from _panel import DispatchMatrixView

    captured = {"called": False, "kwargs": None}

    def fake_setup(**kwargs):
        captured["called"] = True
        captured["kwargs"] = kwargs
        return _claude_install.ClaudeLoginResult(
            ok=True,
            secret_applied=True,
            deployment_applied=True,
            rollout_ready=True,
        )

    monkeypatch.setattr(
        _claude_install,
        "setup_token_claude_worker",
        fake_setup,
    )

    view = DispatchMatrixView(data=mock_data_with_claude)
    view.handle_key("T", MagicMock())  # abre modal
    view.handle_key("Y", MagicMock())  # confirma

    if view._install_thread is not None:
        view._install_thread.join(timeout=2.0)

    assert captured["called"] is True
    assert view.last_ok is True
    assert captured["kwargs"].get("interactive") is False


def test_perform_setup_token_blocking_mode_runs_inline(
    mock_data_with_claude,
    monkeypatch,
):
    """``_perform_setup_token(_blocking=True)`` executa inline e publica
    sucesso — caminho síncrono usado pelos testes."""
    import _claude_install
    from _panel import DispatchMatrixView

    calls = {"n": 0}

    def fake_setup(**kwargs):
        calls["n"] += 1
        return _claude_install.ClaudeLoginResult(
            ok=True,
            secret_applied=True,
            deployment_applied=True,
            rollout_ready=True,
        )

    monkeypatch.setattr(
        _claude_install,
        "setup_token_claude_worker",
        fake_setup,
    )

    view = DispatchMatrixView(data=mock_data_with_claude)
    view._perform_setup_token(_blocking=True)

    assert calls["n"] == 1
    assert view._install_in_progress is False
    assert view.last_ok is True


# ============================================================================
# Bug #2 hotfix: _perform_install NÃO BLOQUEIA o painel TUI
# ============================================================================


def test_perform_install_runs_in_background_thread_by_default(
    mock_data_with_claude,
    monkeypatch,
):
    """``_perform_install(force_relogin=True)`` retorna imediatamente sem
    bloquear o caller; bootstrap roda em thread daemon — fix do freeze
    relatado no [L]."""
    import threading
    import time as _time

    import _claude_install
    from _panel import DispatchMatrixView

    # bootstrap simulado: dorme 0.5s pra emular subprocess.run blocante.
    started = threading.Event()
    finished = threading.Event()

    def slow_bootstrap(**kwargs):
        started.set()
        _time.sleep(0.5)
        finished.set()
        return _claude_install.ClaudeLoginResult(
            ok=True,
            account_email="x@y.com",
            secret_applied=True,
            deployment_applied=True,
            rollout_ready=True,
        )

    monkeypatch.setattr(
        _claude_install,
        "bootstrap_claude_worker",
        slow_bootstrap,
    )

    view = DispatchMatrixView(data=mock_data_with_claude)
    t0 = _time.monotonic()
    view._perform_install(force_relogin=True)
    elapsed = _time.monotonic() - t0

    # Caller retornou em < 100ms (não bloqueou os 500ms do bootstrap).
    assert elapsed < 0.1, (
        f"_perform_install bloqueou por {elapsed:.3f}s — esperado retorno "
        f"imediato (thread daemon)"
    )
    # Mensagem otimista visível no painel enquanto thread roda.
    assert "background" in view.last_msg.lower()
    assert view._install_in_progress is True
    # Thread foi de fato iniciada.
    assert started.wait(timeout=1.0), "bootstrap thread não iniciou em 1s"
    # E completa OK em até 2s.
    assert finished.wait(timeout=2.0), "bootstrap thread não completou em 2s"
    # Após completar, painel atualiza mensagem de sucesso.
    view._install_thread.join(timeout=2.0)
    assert view._install_in_progress is False
    assert view.last_ok is True
    assert "sucesso" in view.last_msg.lower()


def test_perform_install_blocking_mode_runs_inline(
    mock_data_with_claude,
    monkeypatch,
):
    """Modo ``_blocking=True`` (usado por ``_on_worker_selected``) executa
    inline — preserva a verificação de cw_status logo depois do install."""
    import _claude_install
    from _panel import DispatchMatrixView

    call_count = {"n": 0}

    def fake_bootstrap(**kwargs):
        call_count["n"] += 1
        return _claude_install.ClaudeLoginResult(
            ok=True,
            account_email="user@blocking.com",
            secret_applied=True,
            deployment_applied=True,
            rollout_ready=True,
        )

    monkeypatch.setattr(
        _claude_install,
        "bootstrap_claude_worker",
        fake_bootstrap,
    )

    view = DispatchMatrixView(data=mock_data_with_claude)
    view._perform_install(force_relogin=False, _blocking=True)

    # Bootstrap chamado e estado completo já visível ao retornar.
    assert call_count["n"] == 1
    assert view._install_in_progress is False
    assert view.last_ok is True


def test_concurrent_install_request_is_ignored(
    mock_data_with_claude,
    monkeypatch,
):
    """Apertar [I]/[L] enquanto bootstrap em background NÃO spawna nova
    thread — devolve mensagem informativa."""
    import threading
    import time as _time

    import _claude_install
    from _panel import DispatchMatrixView

    bootstrap_calls = {"n": 0}
    release = threading.Event()

    def bootstrap_that_waits(**kwargs):
        bootstrap_calls["n"] += 1
        release.wait(timeout=2.0)
        return _claude_install.ClaudeLoginResult(
            ok=True,
            account_email="single@call.com",
            secret_applied=True,
            deployment_applied=True,
            rollout_ready=True,
        )

    monkeypatch.setattr(
        _claude_install,
        "bootstrap_claude_worker",
        bootstrap_that_waits,
    )

    view = DispatchMatrixView(data=mock_data_with_claude)
    view._perform_install(force_relogin=True)  # 1ª chamada — spawna thread
    _time.sleep(0.05)  # dá tempo da thread iniciar
    view._perform_install(force_relogin=True)  # 2ª chamada — ignorada

    assert bootstrap_calls["n"] == 1
    assert "em andamento" in view.last_msg.lower()

    release.set()  # libera a thread pra terminar
    view._install_thread.join(timeout=2.0)


# ============================================================================
# FU #6: [U] uninstall hotkey
# ============================================================================


def test_u_key_opens_uninstall_modal(mock_data_with_claude):
    """``[U]`` abre modal de confirmação Y/N — mesmo quando install
    parcial (deployment_applied=False), pois pode haver orfãos."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view.handle_key("U", MagicMock())
    assert view.mode is not None
    assert view.mode[0] == "uninstall_confirm"


def test_u_key_blocked_when_install_in_progress(mock_data_with_claude):
    """Spawn de [U] enquanto install em background → mensagem de bloqueio."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view._install_in_progress = True
    view.handle_key("U", MagicMock())
    assert view.mode is None  # não abriu modal
    assert "em andamento" in view.last_msg.lower()


def test_perform_uninstall_runs_in_background_thread_by_default(
    mock_data_with_claude,
    monkeypatch,
):
    """``_perform_uninstall`` retorna imediatamente; uninstall roda em
    daemon thread — mesmo padrão de :meth:`_perform_install`."""
    import threading
    import time as _time

    import _claude_install
    from _panel import DispatchMatrixView

    finished = threading.Event()

    def slow_uninstall(**kwargs):
        _time.sleep(0.3)
        finished.set()
        return _claude_install.ClaudeLoginResult(ok=True)

    monkeypatch.setattr(
        _claude_install,
        "uninstall_claude_worker",
        slow_uninstall,
    )

    view = DispatchMatrixView(data=mock_data_with_claude)
    t0 = _time.monotonic()
    view._perform_uninstall()
    elapsed = _time.monotonic() - t0

    assert elapsed < 0.1, (
        f"_perform_uninstall bloqueou {elapsed:.3f}s — esperado retorno "
        f"imediato (thread daemon)"
    )
    assert "background" in view.last_msg.lower()
    assert view._install_in_progress is True
    assert finished.wait(timeout=2.0)
    view._install_thread.join(timeout=2.0)
    assert view._install_in_progress is False
    assert view.last_ok is True
    assert "desinstalado" in view.last_msg.lower()


def test_handle_uninstall_confirm_yes_triggers_perform(
    mock_data_with_claude,
    monkeypatch,
):
    """``[Y]`` no modal de uninstall chama ``_perform_uninstall``."""
    import _claude_install
    from _panel import DispatchMatrixView

    call_count = {"n": 0}

    def fake_uninstall(**kwargs):
        call_count["n"] += 1
        return _claude_install.ClaudeLoginResult(ok=True)

    monkeypatch.setattr(
        _claude_install,
        "uninstall_claude_worker",
        fake_uninstall,
    )

    view = DispatchMatrixView(data=mock_data_with_claude)
    view._open_uninstall_modal()
    view._perform_uninstall = lambda: setattr(view, "_test_called", True)
    view._handle_uninstall_confirm("Y")
    assert getattr(view, "_test_called", False) is True
    assert view.mode is None  # modal fechado


def test_handle_uninstall_confirm_no_cancels(mock_data_with_claude):
    """``[N]`` fecha o modal sem chamar uninstall."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view._open_uninstall_modal()
    view._handle_uninstall_confirm("N")
    assert view.mode is None
    assert "cancelada" in view.last_msg.lower()


# ============================================================================
# Bug #1 hotfix: deploy.py expõe repo root no sys.path
# ============================================================================


def test_deploy_py_inserts_repo_root_in_syspath():
    """``deploy.py`` insere o repo root no ``sys.path`` para que imports
    ``from deile.<x>`` resolvam quando o script é executado direto
    (``python3 infra/k8s/deploy.py``). Sem isso, ``set_pipeline_dispatch_stage``
    quebra com 'No module named deile.orchestration.pipeline.dispatch_resolver'."""
    deploy_py = Path(__file__).resolve().parents[3] / "infra" / "k8s" / "deploy.py"
    source = deploy_py.read_text()
    # O fix usa ``_REPO_ROOT = _INFRA.parent`` + ``sys.path.insert(0, str(_REPO_ROOT))``.
    assert "_REPO_ROOT" in source
    assert "sys.path.insert(0, str(_REPO_ROOT))" in source
