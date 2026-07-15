from __future__ import annotations

import httpx
import pytest

from daytrader.notifications import TelegramDeliveryError, TelegramNotifier


@pytest.mark.asyncio
async def test_telegram_http_error_never_exposes_bot_token(monkeypatch) -> None:
    token = "123456789:secret-token-that-must-not-appear"
    request = httpx.Request(
        "POST", f"https://api.telegram.org/bot{token}/sendMessage"
    )
    response = httpx.Response(404, request=request)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    notifier = TelegramNotifier(token, "123456789")

    with pytest.raises(TelegramDeliveryError) as raised:
        await notifier.send("test")

    assert str(raised.value) == "Telegram send failed with HTTP 404"
    assert token not in str(raised.value)
