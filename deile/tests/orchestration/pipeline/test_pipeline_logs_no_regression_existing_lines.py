"""AC0/AC13 — Regressão: linhas de tick-log pré-existentes em monitor.py.

Verifica que as duas linhas de tick-log históricas (DEBUG "pipeline tick #%d"
e INFO "tick #%d done in %.2fs:") não foram removidas nem alteradas por
nenhuma sub-issue upstream (#555–#560).

Referência canônica: issue #438 §Baseline, revalidado contra main@96eee7e.
"""

from __future__ import annotations

from pathlib import Path

_MONITOR_PY = (
    Path(__file__).resolve().parents[4]
    / "deile"
    / "orchestration"
    / "pipeline"
    / "monitor.py"
)


def _lines_with_numbers() -> list[tuple[int, str]]:
    text = _MONITOR_PY.read_text(encoding="utf-8")
    return [(i + 1, ln) for i, ln in enumerate(text.splitlines())]


def test_pipeline_tick_debug_line_present() -> None:
    """monitor.py deve conter logger.debug("pipeline tick #%d") — não-regressão AC13."""
    lines = _lines_with_numbers()
    matches = [(lineno, ln) for lineno, ln in lines if "pipeline tick #" in ln]
    assert matches, (
        "Regressão AC13: nenhuma linha com 'pipeline tick #' em monitor.py. "
        'A linha logger.debug("pipeline tick #%d") foi removida ou alterada.'
    )
    assert any('logger.debug("pipeline tick #%d"' in ln for _, ln in matches), (
        f"Regressão AC13: formato da linha DEBUG de tick alterado. "
        f"Matches encontrados: {matches}"
    )


def test_tick_done_info_line_present() -> None:
    """monitor.py deve conter logger.info("tick #%d done in %.2fs:") — não-regressão AC13."""
    lines = _lines_with_numbers()
    matches = [(lineno, ln) for lineno, ln in lines if '"tick #%d done in %.2fs:' in ln]
    assert matches, (
        "Regressão AC13: nenhuma linha com 'tick #%d done in %.2fs:' em monitor.py. "
        'A linha logger.info("tick #%d done in ...") foi removida ou alterada.'
    )
