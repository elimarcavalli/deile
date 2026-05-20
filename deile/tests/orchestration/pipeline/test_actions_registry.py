"""Validate the canonical ACTIONS registry against runtime targets.

The :data:`ACTIONS` tuple in :mod:`deile.orchestration.pipeline.actions`
holds ``method`` and ``enable_attr`` string fields that are resolved via
``getattr`` at runtime against :class:`PipelineMonitor` and
:class:`PipelineConfig`.  A typo or rename anywhere only surfaces in
production today — this test makes the contract a compile-time fact.
"""

from __future__ import annotations

import dataclasses

from deile.orchestration.pipeline.actions import ACTIONS
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)


def test_each_action_method_exists_on_monitor():
    """Every ActionDef.method is a real attribute of PipelineMonitor."""
    for a in ACTIONS:
        assert hasattr(PipelineMonitor, a.method), (
            f"ActionDef(name={a.name!r}) references method "
            f"{a.method!r} which is missing on PipelineMonitor"
        )


def test_each_action_enable_attr_is_a_config_field():
    """Every ActionDef.enable_attr maps to a real PipelineConfig field."""
    field_names = {f.name for f in dataclasses.fields(PipelineConfig)}
    for a in ACTIONS:
        assert a.enable_attr in field_names, (
            f"ActionDef(name={a.name!r}) references enable_attr "
            f"{a.enable_attr!r} which is not a PipelineConfig field"
        )
