"""WhatsApp Cloud API HTTP client."""

from __future__ import annotations

from typing import Any, Mapping

from deile_bot.foundation.exceptions import ProviderError


class WhatsAppApiClient:
    """Minimal HTTP wrapper around graph.facebook.com /messages endpoint."""

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str = "v22.0",
    ):
        self._token = access_token
        self._phone_id = phone_number_id
        self._version = api_version
        self._client: Any = None

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self._version}/{self._phone_id}/messages"

    async def _get_client(self):
        if self._client is None:
            try:
                import httpx
            except ImportError as e:
                raise ProviderError("httpx not installed (extras=whatsapp)", context={}) from e
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def send_text(self, to: str, text: str) -> str:
        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        })

    async def send_template(
        self,
        to: str,
        name: str,
        language: str,
        components: list = None,
    ) -> str:
        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": components or [],
            },
        })

    async def _post(self, payload: Mapping[str, Any]) -> str:
        client = await self._get_client()
        try:
            r = await client.post(
                self.base_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            r.raise_for_status()
            data = r.json()
            return str((data.get("messages") or [{}])[0].get("id", ""))
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"whatsapp send failed: {e}", context={}) from e

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
