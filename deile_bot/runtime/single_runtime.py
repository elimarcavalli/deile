"""SingleProviderRuntime — runs one adapter wired to the foundation pipeline."""

from __future__ import annotations

from typing import Optional

from deile_bot.foundation.envelope import MessageEnvelope
from deile_bot.foundation.logging import get_logger
from deile_bot.foundation.pipeline import IngressPipeline


class SingleProviderRuntime:
    def __init__(self, adapter, pipeline: IngressPipeline):
        self.adapter = adapter
        self.pipeline = pipeline
        self._logger = get_logger("runtime.single")

    async def start(self) -> None:
        async def on_inbound(env: MessageEnvelope, src) -> None:
            try:
                await self.pipeline.handle(env, src)
            except Exception:
                self._logger.exception("ingress pipeline raised")

        self.adapter.on_inbound = on_inbound
        await self.adapter.start()

    async def stop(self) -> None:
        await self.adapter.stop()


class MultiProviderRuntime:
    """Runs N adapters sharing the same foundation pipeline."""

    def __init__(self, adapters: list, pipeline: IngressPipeline):
        self.adapters = adapters
        self.pipeline = pipeline
        self._logger = get_logger("runtime.multi")

    async def start(self) -> None:
        for adapter in self.adapters:
            async def make_cb(a):
                async def on_inbound(env, src):
                    try:
                        await self.pipeline.handle(env, src)
                    except Exception:
                        self._logger.exception("ingress pipeline raised")
                return on_inbound

            adapter.on_inbound = await make_cb(adapter)
            await adapter.start()

    async def stop(self) -> None:
        for adapter in self.adapters:
            try:
                await adapter.stop()
            except Exception:
                self._logger.exception("adapter.stop raised")
