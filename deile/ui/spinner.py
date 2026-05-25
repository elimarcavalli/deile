"""Spinner frames shared across UI renderers.

Centralizado para evitar drift entre ``streaming_renderer._SPINNER_FRAMES``
e ``subagent_panel._SPINNER`` — ambos animam "thinking" do agente com a
mesma sequência braille e devem permanecer visualmente consistentes.
"""

from __future__ import annotations

BRAILLE_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
