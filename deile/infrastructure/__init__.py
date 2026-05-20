"""Infrastructure layer do DEILE — External integrations.

Submodules expose their own surfaces; callers import directly from the
submodule (``from deile.infrastructure.deile_worker_client import ...``).
No package-level re-exports — those existed historically but had zero
production consumers and only invited shadow contracts.
"""
