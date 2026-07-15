from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from .engine import MARKET_TZ, TradingEngine
from .models import Market, MarketTick
from .repository import Repository

logger = logging.getLogger(__name__)


class LiveOrderCapabilityDisabled(RuntimeError):
    pass


class KISReadOnlyClient:
    """Strictly read-only KIS client. Only token and GET market-data calls are allowed."""

    PROD_BASE_URL = "https://openapi.koreainvestment.com:9443"
    PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
    ALLOWED_GET_PREFIXES = (
        "/uapi/domestic-stock/v1/quotations/",
        "/uapi/overseas-price/v1/quotations/",
    )

    def __init__(self, app_key: str, app_secret: str, environment: str = "paper"):
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = self.PROD_BASE_URL if environment == "prod" else self.PAPER_BASE_URL
        self.access_token: str | None = None

    async def authenticate(self) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
            response = await client.post(
                "/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                },
            )
            response.raise_for_status()
            self.access_token = response.json()["access_token"]

    async def get_market_data(
        self, path: str, *, tr_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if not path.startswith(self.ALLOWED_GET_PREFIXES):
            raise LiveOrderCapabilityDisabled(f"path is not an allowed market-data path: {path}")
        if not self.access_token:
            await self.authenticate()
        headers = {
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
            response = await client.get(path, headers=headers, params=params)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _number(output: dict[str, Any], *keys: str, default: float = 0.0) -> float:
        for key in keys:
            value = output.get(key)
            if value not in (None, ""):
                try:
                    return float(str(value).replace(",", ""))
                except ValueError:
                    continue
        return default

    async def quote(self, market: Market, symbol: str, exchange: str) -> MarketTick:
        """Fetch one quote using only KIS quotation endpoints.

        This REST fallback is intentionally small. For indicator-driven rules or lower
        latency, POST enriched ticks to the secured market-data endpoint instead.
        """
        if market == Market.KR:
            payload = await self.get_market_data(
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                tr_id="FHKST01010100",
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
            )
            output = payload.get("output") or {}
            last = self._number(output, "stck_prpr")
            bid = self._number(output, "bidp", "bidp1", default=last)
            ask = self._number(output, "askp", "askp1", default=last)
            volume = int(self._number(output, "acml_vol"))
        else:
            exchange_code = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}.get(
                exchange.upper()
            )
            if not exchange_code:
                raise ValueError(f"unsupported KIS overseas exchange: {exchange}")
            payload = await self.get_market_data(
                "/uapi/overseas-price/v1/quotations/price",
                tr_id="HHDFS00000300",
                params={"AUTH": "", "EXCD": exchange_code, "SYMB": symbol},
            )
            output = payload.get("output") or {}
            last = self._number(output, "last")
            bid = self._number(output, "bidp", "pbid", default=last)
            ask = self._number(output, "askp", "pask", default=last)
            volume = int(self._number(output, "tvol", "evol"))
        if last <= 0:
            raise ValueError(f"KIS returned no usable quote for {market.value}:{symbol}")
        if bid <= 0:
            bid = last
        if ask < bid:
            ask = max(last, bid)
        timestamp = datetime.now(UTC)
        return MarketTick(
            market=market,
            symbol=symbol,
            timestamp=timestamp,
            source_timestamp=timestamp,
            received_timestamp=timestamp,
            sequence_id=int(timestamp.timestamp() * 1000),
            connection_id="kis-rest-poller",
            data_source="KIS_REST",
            quote_scope="unknown",
            session="regular",
            market_status="open",
            symbol_status="trading",
            luld_status="normal",
            last=last,
            bid=bid,
            ask=ask,
            trade_size=max(volume, 0),
        )

    async def place_order(self, *_: Any, **__: Any) -> None:
        raise LiveOrderCapabilityDisabled(
            "KISReadOnlyClient cannot place orders; service mode is record_only"
        )


class KISQuotePoller:
    """Polls only symbols in today's armed plans; it never touches an order endpoint."""

    def __init__(
        self,
        client: KISReadOnlyClient,
        repository: Repository,
        engine: TradingEngine,
        universe: dict[str, dict[str, dict[str, str]]],
        interval_seconds: float = 1.0,
    ):
        self.client = client
        self.repository = repository
        self.engine = engine
        self.universe = universe
        self.interval_seconds = max(interval_seconds, 0.5)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="kis-readonly-quote-poller")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def run(self) -> None:
        while True:
            for market in Market:
                local_date = datetime.now(MARKET_TZ[market]).date()
                for plan in self.repository.active_plans(market, local_date):
                    for candidate in plan.approved_symbols:
                        try:
                            tick = await self.client.quote(
                                market, candidate.symbol, candidate.exchange
                            )
                            self.engine.process_tick(tick)
                        except Exception as exc:  # keep other approved symbols running
                            logger.warning(
                                "KIS quote failed for %s:%s: %s",
                                market.value,
                                candidate.symbol,
                                exc,
                            )
            await asyncio.sleep(self.interval_seconds)
