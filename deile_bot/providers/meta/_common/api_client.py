"""Meta Send API HTTP client (shared by Messenger + Instagram)."""

from __future__ import annotations

from typing import Any, Mapping

from deile_bot.foundation.exceptions import ProviderError


class MetaApiClient:
    def __init__(
        self,
        page_access_token: str,
        api_version: str = "v22.0",
    ):
        self._token = page_access_token
        self._version = api_version
        self._client: Any = None

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self._version}/me/messages"

    async def _get_client(self):
        if self._client is None:
            try:
                import httpx
            except ImportError as e:
                raise ProviderError("httpx not installed (extras=meta)", context={}) from e
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def send_text(self, recipient_id: str, text: str) -> str:
        return await self._post({
            "recipient": {"id": recipient_id},
            "message": {"text": text},
        })

    async def _post(self, payload: Mapping[str, Any]) -> str:
        client = await self._get_client()
        try:
            r = await client.post(
                self.base_url,
                params={"access_token": self._token},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            return str(data.get("message_id", ""))
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"meta send failed: {e}", context={}) from e

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
