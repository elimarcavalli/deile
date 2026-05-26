"""Regressão: ESC durante streaming MARCA o turno como cancelado, não apaga.

Antes, ``_rollback_history`` apagava a entrada ``user`` do histórico — o
LLM perdia a memória do que o usuário tinha pedido. Mas o texto ficava no
scrollback do terminal, então o usuário olhava pra tela achando que o LLM
"viu" e mandava follow-ups que dependiam daquilo. Resultado: amnésia
silenciosa.

Agora a entrada ``user`` fica preservada, eventuais entradas ``assistant``
parciais (escritas antes do cancel) são removidas, e um placeholder
``assistant`` com ``"(cancelado pelo usuário)"`` é inserido. Isso:

* mantém o mental model do usuário (o LLM "lembra" da request cancelada);
* explicita o cancelamento pro LLM via marker textual;
* evita duas entradas ``user`` consecutivas (que DeepSeek/OpenAI colapsavam
  com ``/`` como separador — bug histórico que motivou o rollback original).
"""

from __future__ import annotations

from types import SimpleNamespace

from deile.cli import _DeileCLI


def _make_cli(history: list) -> _DeileCLI:
    cli = _DeileCLI()
    cli.default_session = SimpleNamespace(conversation_history=history)
    return cli


def test_cancel_preserves_user_entry_and_adds_placeholder() -> None:
    """ESC após o user enviar a mensagem: entrada user fica + placeholder."""
    history = [
        {"role": "user", "content": "earlier turn", "timestamp": 0.0, "metadata": {}},
        {"role": "assistant", "content": "earlier reply", "timestamp": 0.1, "metadata": {}},
        {"role": "user", "content": "cancelled message", "timestamp": 0.2, "metadata": {}},
    ]
    cli = _make_cli(history)
    cli._rollback_history(baseline_len=2)

    assert len(history) == 4
    assert history[-2]["role"] == "user"
    assert history[-2]["content"] == "cancelled message"
    assert history[-1]["role"] == "assistant"
    assert "cancelado" in history[-1]["content"].lower()
    assert history[-1]["metadata"].get("cancelled") is True


def test_cancel_during_partial_response_removes_partial_keeps_user() -> None:
    """ESC com partial assistant: apaga só o partial, mantém user + placeholder."""
    history = [
        {"role": "user", "content": "earlier turn", "timestamp": 0.0, "metadata": {}},
        {"role": "assistant", "content": "earlier reply", "timestamp": 0.1, "metadata": {}},
        {"role": "user", "content": "cancelled message", "timestamp": 0.2, "metadata": {}},
        {"role": "assistant", "content": "half of a reply", "timestamp": 0.3, "metadata": {}},
    ]
    cli = _make_cli(history)
    cli._rollback_history(baseline_len=2)

    # 'half of a reply' partial foi removido; user permanece; placeholder adicionado
    assert len(history) == 4
    contents = [e["content"] for e in history]
    assert "half of a reply" not in contents
    assert "cancelled message" in contents
    assert history[-1]["role"] == "assistant"
    assert history[-1]["metadata"].get("cancelled") is True


def test_no_cancel_no_change() -> None:
    """Se a baseline já cobre todo o histórico, nada muda."""
    history = [
        {"role": "user", "content": "u1", "timestamp": 0.0, "metadata": {}},
        {"role": "assistant", "content": "a1", "timestamp": 0.1, "metadata": {}},
    ]
    cli = _make_cli(history)
    cli._rollback_history(baseline_len=2)
    assert len(history) == 2
    # Nenhum placeholder porque não há entrada user em baseline_len=2.


def test_handles_session_without_history_attribute() -> None:
    """Não crasha se a sessão não tem ``conversation_history``."""
    cli = _DeileCLI()
    cli.default_session = SimpleNamespace()
    cli._rollback_history(baseline_len=0)  # silent no-op


def test_first_turn_cancel_keeps_user_and_adds_placeholder() -> None:
    """Primeira mensagem cancelada: user fica + placeholder, total 2 entradas."""
    history = [
        {"role": "user", "content": "first ever", "timestamp": 0.0, "metadata": {}},
    ]
    cli = _make_cli(history)
    cli._rollback_history(baseline_len=0)

    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "first ever"
    assert history[1]["role"] == "assistant"
    assert history[1]["metadata"].get("cancelled") is True


def test_cancel_then_new_turn_preserves_alternation() -> None:
    """Próxima mensagem após cancel: alternância user→assistant→user mantida.

    Esse é o invariante que protege contra o bug histórico onde providers
    (DeepSeek/OpenAI) colapsavam 2 entradas user consecutivas usando ``/``
    como separador. Com o placeholder, NUNCA há duas user consecutivas.
    """
    history = [
        {"role": "user", "content": "cancelled", "timestamp": 0.0, "metadata": {}},
    ]
    cli = _make_cli(history)
    cli._rollback_history(baseline_len=0)
    # Simula a próxima mensagem user que o CLI adicionaria
    history.append({"role": "user", "content": "next turn", "timestamp": 1.0, "metadata": {}})

    # Sequência final: user → assistant(cancelado) → user
    roles = [e["role"] for e in history]
    assert roles == ["user", "assistant", "user"]
    # E nenhum par consecutivo é user-user
    for i in range(len(roles) - 1):
        assert not (roles[i] == "user" and roles[i + 1] == "user")
