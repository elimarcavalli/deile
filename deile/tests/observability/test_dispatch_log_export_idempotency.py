"""Testes de idempotência do singleton — issue #454 D7.

Verifica que get_log_provider() é idempotente e _init_count == 1 após init.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit


class TestIdempotency:
    def test_same_instance_returned(self, in_memory_log_exporter):
        """get_log_provider() retorna o mesmo objeto em múltiplas chamadas."""
        from deile.observability.dispatch_log_export import get_log_provider

        p1 = get_log_provider()
        p2 = get_log_provider()
        p3 = get_log_provider()

        assert p1 is not None
        assert p1 is p2
        assert p2 is p3

    def test_init_count_exactly_one(self, in_memory_log_exporter):
        """_init_count deve ser exatamente 1 após init bem-sucedida."""
        from deile.observability.dispatch_log_export import _init_count, get_log_provider

        get_log_provider()
        get_log_provider()
        get_log_provider()

        assert _init_count == 1

    def test_get_dispatch_log_export_same_instance(self):
        """get_dispatch_log_export() retorna o mesmo objeto."""
        from deile.observability.dispatch_log_export import get_dispatch_log_export

        e1 = get_dispatch_log_export()
        e2 = get_dispatch_log_export()
        e3 = get_dispatch_log_export()

        assert e1 is e2
        assert e2 is e3

    def test_concurrent_get_log_provider(self, in_memory_log_exporter):
        """Múltiplas threads chamando get_log_provider() → mesmo objeto."""
        from deile.observability.dispatch_log_export import get_log_provider

        results = []
        errors = []

        def call_get():
            try:
                results.append(get_log_provider())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=call_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"errors in threads: {errors}"
        assert all(r is results[0] for r in results), "all threads should get same provider"

    def test_reset_clears_init_count(self, in_memory_log_exporter):
        """reset_dispatch_log_export() reseta _init_count para 0."""
        from deile.observability.dispatch_log_export import _init_count, get_log_provider
        from deile.observability import reset_dispatch_log_export

        get_log_provider()
        assert _init_count == 1

        reset_dispatch_log_export()

        from deile.observability.dispatch_log_export import _init_count as count_after
        assert count_after == 0
