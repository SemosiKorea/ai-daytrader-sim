from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Iterable, Literal
from zoneinfo import ZoneInfo

import httpx
import websockets

from .models import Market

logger = logging.getLogger(__name__)

DOMESTIC_QUOTE_TR = "H0UNASP0"
DOMESTIC_TRADE_TR = "H0UNCNT0"
OVERSEAS_QUOTE_TR = "HDFSASP0"
OVERSEAS_TRADE_TR = "HDFSCNT0"

DOMESTIC_QUOTE_COLUMNS = [
    "MKSC_SHRN_ISCD",
    "BSOP_HOUR",
    "HOUR_CLS_CODE",
    *[f"ASKP{index}" for index in range(1, 11)],
    *[f"BIDP{index}" for index in range(1, 11)],
    *[f"ASKP_RSQN{index}" for index in range(1, 11)],
    *[f"BIDP_RSQN{index}" for index in range(1, 11)],
    "TOTAL_ASKP_RSQN",
    "TOTAL_BIDP_RSQN",
    "OVTM_TOTAL_ASKP_RSQN",
    "OVTM_TOTAL_BIDP_RSQN",
    "ANTC_CNPR",
    "ANTC_CNQN",
    "ANTC_VOL",
    "ANTC_CNTG_VRSS",
    "ANTC_CNTG_VRSS_SIGN",
    "ANTC_CNTG_PRDY_CTRT",
    "ACML_VOL",
    "TOTAL_ASKP_RSQN_ICDC",
    "TOTAL_BIDP_RSQN_ICDC",
    "OVTM_TOTAL_ASKP_ICDC",
    "OVTM_TOTAL_BIDP_ICDC",
    "STCK_DEAL_CLS_CODE",
    "KMID_PRC",
    "KMID_TOTAL_RSQN",
    "KMID_CLS_CODE",
    "NMID_PRC",
    "NMID_TOTAL_RSQN",
    "NMID_CLS_CODE",
]

DOMESTIC_TRADE_COLUMNS = [
    "MKSC_SHRN_ISCD",
    "STCK_CNTG_HOUR",
    "STCK_PRPR",
    "PRDY_VRSS_SIGN",
    "PRDY_VRSS",
    "PRDY_CTRT",
    "WGHN_AVRG_STCK_PRC",
    "STCK_OPRC",
    "STCK_HGPR",
    "STCK_LWPR",
    "ASKP1",
    "BIDP1",
    "CNTG_VOL",
    "ACML_VOL",
    "ACML_TR_PBMN",
    "SELN_CNTG_CSNU",
    "SHNU_CNTG_CSNU",
    "NTBY_CNTG_CSNU",
    "CTTR",
    "SELN_CNTG_SMTN",
    "SHNU_CNTG_SMTN",
    "CNTG_CLS_CODE",
    "SHNU_RATE",
    "PRDY_VOL_VRSS_ACML_VOL_RATE",
    "OPRC_HOUR",
    "OPRC_VRSS_PRPR_SIGN",
    "OPRC_VRSS_PRPR",
    "HGPR_HOUR",
    "HGPR_VRSS_PRPR_SIGN",
    "HGPR_VRSS_PRPR",
    "LWPR_HOUR",
    "LWPR_VRSS_PRPR_SIGN",
    "LWPR_VRSS_PRPR",
    "BSOP_DATE",
    "NEW_MKOP_CLS_CODE",
    "TRHT_YN",
    "ASKP_RSQN1",
    "BIDP_RSQN1",
    "TOTAL_ASKP_RSQN",
    "TOTAL_BIDP_RSQN",
    "VOL_TNRT",
    "PRDY_SMNS_HOUR_ACML_VOL",
    "PRDY_SMNS_HOUR_ACML_VOL_RATE",
    "HOUR_CLS_CODE",
    "MRKT_TRTM_CLS_CODE",
    "VI_STND_PRC",
]

OVERSEAS_QUOTE_COLUMNS = [
    "SYMB",
    "ZDIV",
    "XYMD",
    "XHMS",
    "KYMD",
    "KHMS",
    "BVOL",
    "AVOL",
    "BDVL",
    "ADVL",
    "PBID1",
    "PASK1",
    "VBID1",
    "VASK1",
    "DBID1",
    "DASK1",
]

OVERSEAS_TRADE_COLUMNS = [
    "SYMB",
    "ZDIV",
    "TYMD",
    "XYMD",
    "XHMS",
    "KYMD",
    "KHMS",
    "OPEN",
    "HIGH",
    "LOW",
    "LAST",
    "SIGN",
    "DIFF",
    "RATE",
    "PBID",
    "PASK",
    "VBID",
    "VASK",
    "EVOL",
    "TVOL",
    "TAMT",
    "BIVL",
    "ASVL",
    "STRN",
    "MTYP",
]

TR_COLUMNS = {
    DOMESTIC_QUOTE_TR: DOMESTIC_QUOTE_COLUMNS,
    DOMESTIC_TRADE_TR: DOMESTIC_TRADE_COLUMNS,
    OVERSEAS_QUOTE_TR: OVERSEAS_QUOTE_COLUMNS,
    OVERSEAS_TRADE_TR: OVERSEAS_TRADE_COLUMNS,
}


@dataclass(frozen=True, slots=True)
class FeedSymbol:
    market: Market
    symbol: str
    exchange: str
    reference: bool = False


@dataclass(frozen=True, slots=True)
class KISSubscription:
    market: Market
    symbol: str
    exchange: str
    tr_id: str
    tr_key: str
    kind: Literal["quote", "trade"]
    reference: bool = False


@dataclass(slots=True)
class RawQuote:
    market: Market
    symbol: str
    at: datetime
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    reference: bool = False


@dataclass(slots=True)
class RawTrade:
    market: Market
    symbol: str
    at: datetime
    last: float
    size: int
    cumulative_volume: int
    halted: bool = False
    reference: bool = False


def _number(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _integer(value: str, default: int = 0) -> int:
    return int(_number(value, default))


def _timestamp(day: str, clock: str, timezone: ZoneInfo, now: datetime) -> datetime:
    clean_day = "".join(character for character in day if character.isdigit())
    clean_clock = "".join(character for character in clock if character.isdigit())
    if len(clean_day) != 8:
        clean_day = now.astimezone(timezone).strftime("%Y%m%d")
    clean_clock = clean_clock.ljust(6, "0")[:6]
    parsed = datetime.strptime(clean_day + clean_clock, "%Y%m%d%H%M%S")
    return parsed.replace(tzinfo=timezone)


def subscription_message(approval_key: str, subscription: KISSubscription) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": subscription.tr_id, "tr_key": subscription.tr_key}},
        }
    )


def build_subscriptions(
    symbols: Iterable[FeedSymbol], *, overseas_prefix: str = "D"
) -> tuple[KISSubscription, ...]:
    exchange_codes = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
    subscriptions: list[KISSubscription] = []
    for item in sorted(symbols, key=lambda value: (value.market.value, value.symbol)):
        if item.market == Market.KR:
            keys = (
                (DOMESTIC_QUOTE_TR, item.symbol, "quote"),
                (DOMESTIC_TRADE_TR, item.symbol, "trade"),
            )
        else:
            exchange = exchange_codes.get(item.exchange.upper())
            if exchange is None:
                raise ValueError(f"unsupported overseas exchange: {item.exchange}")
            tr_key = f"{overseas_prefix.upper()}{exchange}{item.symbol.upper()}"
            keys = (
                (OVERSEAS_QUOTE_TR, tr_key, "quote"),
                (OVERSEAS_TRADE_TR, tr_key, "trade"),
            )
        subscriptions.extend(
            KISSubscription(
                market=item.market,
                symbol=item.symbol.upper(),
                exchange=item.exchange.upper(),
                tr_id=tr_id,
                tr_key=tr_key,
                kind=kind,
                reference=item.reference,
            )
            for tr_id, tr_key, kind in keys
        )
    if len(subscriptions) > 40:
        raise ValueError("KIS WebSocket supports at most 40 subscriptions per connection")
    return tuple(subscriptions)


class KISFrameDecoder:
    def __init__(self, subscriptions: Iterable[KISSubscription]):
        self.subscriptions = tuple(subscriptions)

    def _subscription(self, tr_id: str, raw_symbol: str) -> KISSubscription:
        candidates = [item for item in self.subscriptions if item.tr_id == tr_id]
        normalized = raw_symbol.upper()
        for item in candidates:
            if (
                normalized == item.symbol
                or normalized == item.tr_key
                or normalized.endswith(item.symbol)
            ):
                return item
        if len(candidates) == 1:
            return candidates[0]
        raise ValueError(f"cannot map KIS record to a subscription: {tr_id}:{raw_symbol}")

    @staticmethod
    def records(raw: str) -> tuple[str, list[dict[str, str]]]:
        sections = raw.split("|", 3)
        if len(sections) != 4 or sections[0] not in {"0", "1"}:
            raise ValueError("not a KIS realtime data frame")
        tr_id = sections[1]
        columns = TR_COLUMNS.get(tr_id)
        if columns is None:
            return tr_id, []
        values = sections[3].split("^")
        count = _integer(sections[2], 1)
        width = len(columns)
        available = len(values) // width
        count = min(max(count, 1), available)
        return tr_id, [
            dict(zip(columns, values[index * width : (index + 1) * width]))
            for index in range(count)
        ]

    def decode(self, raw: str, received_at: datetime) -> list[RawQuote | RawTrade]:
        tr_id, records = self.records(raw)
        decoded: list[RawQuote | RawTrade] = []
        for record in records:
            raw_symbol = record.get("MKSC_SHRN_ISCD") or record.get("SYMB") or ""
            subscription = self._subscription(tr_id, raw_symbol)
            timezone = ZoneInfo(
                "Asia/Seoul" if subscription.market == Market.KR else "America/New_York"
            )
            if tr_id == DOMESTIC_QUOTE_TR:
                at = _timestamp("", record["BSOP_HOUR"], timezone, received_at)
                decoded.append(
                    RawQuote(
                        market=subscription.market,
                        symbol=subscription.symbol,
                        at=at,
                        bid=_number(record["BIDP1"]),
                        ask=_number(record["ASKP1"]),
                        bid_size=_integer(record["BIDP_RSQN1"]),
                        ask_size=_integer(record["ASKP_RSQN1"]),
                        reference=subscription.reference,
                    )
                )
            elif tr_id == DOMESTIC_TRADE_TR:
                at = _timestamp(
                    record["BSOP_DATE"], record["STCK_CNTG_HOUR"], timezone, received_at
                )
                decoded.append(
                    RawTrade(
                        market=subscription.market,
                        symbol=subscription.symbol,
                        at=at,
                        last=_number(record["STCK_PRPR"]),
                        size=_integer(record["CNTG_VOL"]),
                        cumulative_volume=_integer(record["ACML_VOL"]),
                        halted=record.get("TRHT_YN", "N") == "Y",
                        reference=subscription.reference,
                    )
                )
            elif tr_id == OVERSEAS_QUOTE_TR:
                at = _timestamp(record["XYMD"], record["XHMS"], timezone, received_at)
                decoded.append(
                    RawQuote(
                        market=subscription.market,
                        symbol=subscription.symbol,
                        at=at,
                        bid=_number(record["PBID1"]),
                        ask=_number(record["PASK1"]),
                        bid_size=_integer(record["VBID1"]),
                        ask_size=_integer(record["VASK1"]),
                        reference=subscription.reference,
                    )
                )
            elif tr_id == OVERSEAS_TRADE_TR:
                at = _timestamp(record["XYMD"], record["XHMS"], timezone, received_at)
                decoded.append(
                    RawTrade(
                        market=subscription.market,
                        symbol=subscription.symbol,
                        at=at,
                        last=_number(record["LAST"]),
                        size=_integer(record["EVOL"]),
                        cumulative_volume=_integer(record["TVOL"]),
                        reference=subscription.reference,
                    )
                )
        return decoded


class KISApprovalClient:
    PROD_HTTP = "https://openapi.koreainvestment.com:9443"
    PAPER_HTTP = "https://openapivts.koreainvestment.com:29443"

    def __init__(self, app_key: str, app_secret: str, environment: str):
        self.app_key = app_key
        self.app_secret = app_secret
        self.environment = environment
        self._approval_key: str | None = None
        self._issued_at: datetime | None = None

    async def approval_key(self) -> str:
        now = datetime.now().astimezone()
        if (
            self._approval_key
            and self._issued_at
            and (now - self._issued_at).total_seconds() < 23 * 3600
        ):
            return self._approval_key
        base_url = self.PROD_HTTP if self.environment == "prod" else self.PAPER_HTTP
        async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
            response = await client.post(
                "/oauth2/Approval",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "secretkey": self.app_secret,
                },
            )
            response.raise_for_status()
            self._approval_key = str(response.json()["approval_key"])
            self._issued_at = now
            return self._approval_key


class KISWebSocketStream:
    PROD_WS = "ws://ops.koreainvestment.com:21000/tryitout"
    PAPER_WS = "ws://ops.koreainvestment.com:31000/tryitout"

    def __init__(
        self,
        approval: KISApprovalClient,
        environment: str,
        subscription_provider: Callable[[], tuple[KISSubscription, ...]],
        on_record: Callable[[RawQuote | RawTrade, datetime, str], Awaitable[None]],
        refresh_seconds: float = 5.0,
    ):
        self.approval = approval
        self.environment = environment
        self.subscription_provider = subscription_provider
        self.on_record = on_record
        self.refresh_seconds = max(1.0, refresh_seconds)

    async def run(self) -> None:
        backoff = 1.0
        while True:
            subscriptions = self.subscription_provider()
            if not subscriptions:
                await asyncio.sleep(self.refresh_seconds)
                continue
            decoder = KISFrameDecoder(subscriptions)
            url = self.PROD_WS if self.environment == "prod" else self.PAPER_WS
            try:
                approval_key = await self.approval.approval_key()
                connection_id = f"kis-ws-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S%f')}"
                async with websockets.connect(url, ping_interval=None, close_timeout=5) as socket:
                    for subscription in subscriptions:
                        await socket.send(subscription_message(approval_key, subscription))
                        await asyncio.sleep(0.05)
                    logger.info(
                        "KIS WebSocket connected: %s subscriptions=%d",
                        connection_id,
                        len(subscriptions),
                    )
                    backoff = 1.0
                    loop = asyncio.get_running_loop()
                    next_refresh = loop.time() + self.refresh_seconds
                    while True:
                        remaining = max(0.01, next_refresh - loop.time())
                        try:
                            raw = await asyncio.wait_for(socket.recv(), remaining)
                        except TimeoutError:
                            if self.subscription_provider() != subscriptions:
                                break
                            next_refresh = loop.time() + self.refresh_seconds
                            continue
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        received_at = datetime.now().astimezone()
                        if raw.startswith(("0|", "1|")):
                            for record in decoder.decode(raw, received_at):
                                await self.on_record(record, received_at, connection_id)
                            if loop.time() >= next_refresh:
                                if self.subscription_provider() != subscriptions:
                                    break
                                next_refresh = loop.time() + self.refresh_seconds
                            continue
                        response = json.loads(raw)
                        header = response.get("header") or {}
                        if header.get("tr_id") == "PINGPONG":
                            await socket.pong(raw.encode("utf-8"))
                            continue
                        body = response.get("body") or {}
                        if body.get("rt_cd") not in {None, "0"}:
                            raise RuntimeError(
                                f"KIS subscription rejected: {body.get('msg_cd')} {body.get('msg1')}"
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("KIS WebSocket reconnect after error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
