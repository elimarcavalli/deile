"""Smoke tests do shell Textual (issue #317 Fase 0+1).

Estes testes:
  1. Sao skipped quando ``textual`` nao esta instalado (extra ``[ui]``
     opcional, evita quebrar a suite default).
  2. Cobrem o caminho de erro sem dependencia externa: stubs/helpers do
     proprio modulo (``TEXTUAL_INSTALL_HINT``, ``_format_header_subtitle``).
  3. Quando ``textual`` esta presente, sobem a app via ``App.run_test()`` e
     simulam 3 mensagens via ``Input``, verificando que o ``RichLog``
     contem cada uma.
"""

from __future__ import annotations

import pytest

from deile.ui import textual_app

# Marker reutilizavel para os smoke tests reais (que precisam do extra ``[ui]``).
requires_textual = pytest.mark.skipif(
    not textual_app.TEXTUAL_AVAILABLE,
    reason="textual nao instalado — pulando smoke real do App",
)


@pytest.fixture
def app_with_cli_snapshot():
    """DEILEApp instanciada com snapshot minimo de role=cli/turns=0.

    Usada pelos smoke tests que so precisam de um app rodando — evita
    repetir o dict literal em cada teste.
    """
    return textual_app.DEILEApp(
        instance_state_snapshot={"role": "cli", "stats": {"turns": 0}}
    )


@pytest.mark.ui
def test_install_hint_mentions_extra():
    """A mensagem de install precisa mencionar a extra ``[ui]`` exatamente
    como aparece em ``pyproject.toml``, para que o usuario consiga copiar e
    colar sem traducao."""
    assert "[ui]" in textual_app.TEXTUAL_INSTALL_HINT
    assert "pip install" in textual_app.TEXTUAL_INSTALL_HINT


@pytest.mark.ui
def test_ensure_textual_available_raises_clearly_when_absent():
    """Quando textual nao esta instalado, ``ensure_textual_available`` levanta
    um ``ImportError`` com mensagem util — nao um ``ModuleNotFoundError`` cru
    de uma linha de import interna."""
    if textual_app.TEXTUAL_AVAILABLE:
        pytest.skip("textual instalado — caso de erro nao reproduzivel sem mock")
    with pytest.raises(ImportError) as excinfo:
        textual_app.ensure_textual_available()
    assert "[ui]" in str(excinfo.value)


@pytest.mark.ui
@pytest.mark.parametrize(
    "snap,expected_substrings",
    [
        ({}, ["role=?", "action=idle", "turns=0"]),
        (
            {
                "role": "cli",
                "current_action": {"kind": "tool_execution", "detail": "execute_bash"},
                "stats": {"turns": 7},
            },
            ["role=cli", "action=tool_execution:execute_bash", "turns=7"],
        ),
        (
            {
                "role": "worker",
                "current_action": None,
                "stats": {"turns": 0, "cost_usd": 0.42},
            },
            ["role=worker", "action=idle", "turns=0"],
        ),
    ],
)
def test_format_header_subtitle_handles_partial_snapshots(snap, expected_substrings):
    """O Header deve degradar gracioso quando o snapshot do InstanceState
    nao tem todas as chaves. Cobre os tres cenarios principais: snapshot
    vazio (sem singleton), snapshot completo com acao em curso, snapshot
    com ``current_action=None`` (idle)."""
    out = textual_app._format_header_subtitle(snap)
    for expected in expected_substrings:
        assert expected in out, f"esperava '{expected}' em '{out}'"


@pytest.mark.ui
@requires_textual
async def test_app_run_test_renders_input_messages(app_with_cli_snapshot):
    """App.run_test smoke: instancia a DEILEApp, simula tres submits no Input
    e verifica que o ``RichLog`` contem cada mensagem (echo)."""
    # Importacao tardia: so quando textual existe.
    from textual.widgets import Input, RichLog

    app = app_with_cli_snapshot
    messages = ["primeira", "segunda", "terceira"]

    async with app.run_test() as pilot:
        # Espera o ChatScreen ser empurrado e widgets virem mounted.
        await pilot.pause()
        # ``query_one`` em escopo de app desce ate a tela ativa via ``screen``.
        prompt = app.screen.query_one("#prompt", Input)
        log = app.screen.query_one("#chat_history", RichLog)
        assert prompt is not None
        assert log is not None

        for msg in messages:
            prompt.value = msg
            await pilot.press("enter")
            await pilot.pause()

        # O ``RichLog.lines`` expoe cada linha renderizada. Procuramos cada
        # mensagem em qualquer linha (o handler prepende ``> `` + tag bold).
        rendered = "\n".join(str(line) for line in log.lines)
        for msg in messages:
            assert msg in rendered, f"mensagem '{msg}' nao apareceu no log"

        # E o input deve estar limpo apos cada submit.
        assert prompt.value == ""


@pytest.mark.ui
@requires_textual
async def test_app_subtitle_reflects_injected_snapshot():
    """A subtitle do App deve refletir o snapshot injetado (testabilidade
    sem precisar de InstanceState singleton)."""
    app = textual_app.DEILEApp(
        instance_state_snapshot={
            "role": "cli",
            "current_action": {"kind": "llm_call", "detail": "anthropic"},
            "stats": {"turns": 3},
        }
    )
    async with app.run_test():
        # on_mount roda sincrono antes do primeiro frame; sub_title ja foi
        # setado quando run_test entrega o controle.
        assert app.sub_title == "role=cli | action=llm_call:anthropic | turns=3"


@pytest.mark.ui
def test_cli_flag_routes_to_textual_runner(monkeypatch):
    """``deile --ui textual`` (sem mensagem) deve invocar ``_run_textual_ui``
    e devolver o exit code dele, sem cair no caminho legacy."""
    import sys as _sys

    from deile import cli as cli_module

    # Limpa qualquer env de chave que possa fazer a CLI tentar bootstrap.
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    # ``main`` so cai no ``sys.stdin.read()`` quando o stdin nao e TTY (o pytest
    # captura stdin → ``isatty()`` retorna False). Forcamos True para evitar
    # OSError "reading from stdin while output is captured".
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)

    calls = {"count": 0}

    def _fake_runner() -> int:
        calls["count"] += 1
        return 0

    monkeypatch.setattr(cli_module, "_run_textual_ui", _fake_runner)
    exit_code = cli_module.main(["--ui", "textual"])
    assert exit_code == 0
    assert calls["count"] == 1


@pytest.mark.ui
def test_cli_flag_default_legacy_does_not_call_textual(monkeypatch):
    """Sem ``--ui textual`` (default legacy + uma mensagem one-shot),
    ``_run_textual_ui`` NUNCA pode ser chamado — invariante anti-regressao
    pra evitar o Textual ser bootado por engano e quebrar o caminho default."""
    import sys as _sys

    from deile import cli as cli_module

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)

    called = {"textual": 0, "oneshot": 0}

    def _fake_textual() -> int:
        called["textual"] += 1
        return 0

    async def _fake_oneshot(*args, **kwargs) -> int:
        called["oneshot"] += 1
        return 0

    monkeypatch.setattr(cli_module, "_run_textual_ui", _fake_textual)
    monkeypatch.setattr(cli_module, "_run_oneshot", _fake_oneshot)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-ignored-by-fake-oneshot")
    cli_module.main(["mensagem qualquer"])
    assert called["textual"] == 0


@pytest.mark.ui
def test_run_textual_ui_emits_install_hint_when_extra_missing(monkeypatch, capsys):
    """Quando ``ensure_textual_available`` levanta ``ImportError``, o helper
    da CLI deve imprimir o ``TEXTUAL_INSTALL_HINT`` no stderr e devolver
    exit code 2 — usuario sai sem stacktrace e com instrucao acionavel."""
    from deile import cli as cli_module
    from deile.ui import textual_app as ta

    def _raise() -> None:
        raise ImportError(ta.TEXTUAL_INSTALL_HINT)

    monkeypatch.setattr(ta, "ensure_textual_available", _raise)
    code = cli_module._run_textual_ui()
    captured = capsys.readouterr()
    assert code == 2
    assert "[ui]" in captured.err


@pytest.mark.ui
@requires_textual
async def test_app_clear_log_binding_empties_history(app_with_cli_snapshot):
    """Ctrl+L deve limpar o RichLog sem encerrar a app."""
    from textual.widgets import Input, RichLog

    app = app_with_cli_snapshot
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.screen.query_one("#prompt", Input)
        log = app.screen.query_one("#chat_history", RichLog)

        prompt.value = "mensagem antes do clear"
        await pilot.press("enter")
        await pilot.pause()
        assert len(log.lines) > 0

        await pilot.press("ctrl+l")
        await pilot.pause()
        assert len(log.lines) == 0


def _rendered_text(rich_log) -> str:
    """Helper: extrai o texto cru de todos os segments do RichLog.

    ``RichLog.lines`` devolve ``rich.segment.Strip`` (lista de ``Segment``);
    ``str(line)`` e o repr (com truncamento), nao o texto renderizado.
    Concatenamos ``segment.text`` em ordem para obter a string visivel.
    """
    parts = []
    for strip in rich_log.lines:
        for segment in strip:
            parts.append(segment.text)
        parts.append("\n")
    return "".join(parts)


@pytest.mark.ui
@requires_textual
async def test_app_escapes_rich_markup_in_user_input(app_with_cli_snapshot):
    """Regressao do achado de seguranca do Revisor 2: usuario digita
    ``[red]inject[/red]`` no Input — o RichLog tem ``markup=True`` e ecoaria
    como Rich markup (spoofing visual). A sanitizacao via
    ``rich.markup.escape`` deve renderizar as tags como texto literal."""
    from textual.widgets import Input, RichLog

    app = app_with_cli_snapshot
    payloads = [
        "[red]inject[/red]",
        "ola unicode áéíóú \U0001f680",
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.screen.query_one("#prompt", Input)
        log = app.screen.query_one("#chat_history", RichLog)
        for payload in payloads:
            prompt.value = payload
            await pilot.press("enter")
            await pilot.pause()
        rendered = _rendered_text(log)
        # As tags brutas (com colchetes) precisam estar literais no buffer:
        # sem o escape, Rich consumiria ``[red]...[/red]`` como style e
        # nenhum texto literal apareceria.
        assert "[red]inject[/red]" in rendered, rendered
        # Acentos/emoji devem aparecer intactos (Unicode-clean path).
        assert "áéíóú" in rendered, rendered
        assert "\U0001f680" in rendered, rendered


@pytest.mark.ui
def test_snapshot_instance_state_does_not_create_singleton(monkeypatch):
    """Regressao do achado major do Revisor 2: ``_snapshot_instance_state``
    NAO pode criar o singleton InstanceState (que dispara side-effects:
    state file, atexit, StatusServer, Registry). Quando o singleton e None,
    deve retornar ``{}`` sem importar/criar nada novo. Usa a API publica
    ``peek_instance_state`` (issue #317 — sub-readers consomem, nao produzem)."""
    from deile.runtime import instance_state as _is_mod

    # Garante que comecamos sem singleton.
    monkeypatch.setattr(_is_mod, "_instance_singleton", None, raising=False)
    snap = textual_app._snapshot_instance_state()
    assert snap == {}
    # E o singleton continua None — nada foi instanciado.
    assert _is_mod.peek_instance_state() is None


@pytest.mark.ui
def test_peek_instance_state_returns_singleton_or_none(monkeypatch):
    """API publica ``peek_instance_state``: contrato e devolver o singleton
    se existe, ou ``None`` se nao. Sem criar. Sub-readers (Header da app
    Textual, painel) consomem via essa porta."""
    from deile.runtime import instance_state as _is_mod
    from deile.runtime.instance_state import peek_instance_state

    monkeypatch.setattr(_is_mod, "_instance_singleton", None, raising=False)
    assert peek_instance_state() is None

    sentinel = object()
    monkeypatch.setattr(_is_mod, "_instance_singleton", sentinel, raising=False)
    assert peek_instance_state() is sentinel


@pytest.mark.ui
def test_cli_warns_when_ui_textual_combined_with_message(monkeypatch, capsys):
    """Regressao do achado minor (R1+R2): combinar ``--ui textual`` com
    mensagem one-shot deve emitir warning em stderr explicando o conflito,
    em vez de ignorar silenciosamente."""
    import sys as _sys

    from deile import cli as cli_module

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)

    async def _fake_oneshot(*args, **kwargs) -> int:
        return 0

    monkeypatch.setattr(cli_module, "_run_oneshot", _fake_oneshot)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-ignored-by-fake-oneshot")
    cli_module.main(["--ui", "textual", "olha so essa mensagem"])
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "--ui textual" in captured.err
    assert "one-shot" in captured.err
