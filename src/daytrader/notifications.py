from __future__ import annotations

import logging

import httpx


logger = logging.getLogger(__name__)


class TelegramDeliveryError(RuntimeError):
    """A sanitized Telegram failure that never includes the bot token URL."""


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None):
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, message: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    url, json={"chat_id": self.chat_id, "text": message}
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TelegramDeliveryError(
                f"Telegram send failed with HTTP {exc.response.status_code}"
            ) from None
        except httpx.HTTPError:
            raise TelegramDeliveryError("Telegram send request failed") from None
        return True

    async def send_best_effort(self, message: str) -> bool:
        """Deliver an operational notification without changing committed trading state."""
        try:
            return await self.send(message)
        except TelegramDeliveryError as exc:
            logger.warning("Telegram notification skipped: %s", exc)
            return False
