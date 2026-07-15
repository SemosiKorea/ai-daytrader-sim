from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from .models import Market
from .repository import Repository

# This release deliberately cannot transmit an order to the production KIS domain.
# Enabling live trading requires a reviewed code change in addition to runtime configuration.
LIVE_ORDER_TRANSPORT_ENABLED = False


class KISOrderSafetyError(RuntimeError):
    pass


class KISOrderRejected(RuntimeError):
    pass


class KISOrderAction(StrEnum):
    NEW = "NEW"
    CANCEL = "CANCEL"


class KISOrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class KISOrderStatus(StrEnum):
    RECORDED_ONLY = "RECORDED_ONLY"


class KISOrderIntent(BaseModel):
    intent_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str = Field(min_length=8, max_length=200)
    source_order_id: str = Field(min_length=1, max_length=100)
    plan_id: str = Field(min_length=1, max_length=80)
    market: Market
    symbol: str = Field(min_length=1, max_length=20)
    exchange: str = Field(min_length=2, max_length=20)
    action: KISOrderAction
    side: KISOrderSide | None = None
    quantity: int = Field(ge=0)
    limit_price: float = Field(ge=0)
    reason: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_order(self) -> "KISOrderIntent":
        self.symbol = self.symbol.upper()
        self.exchange = self.exchange.upper()
        if self.action == KISOrderAction.NEW:
            if self.side is None or self.quantity <= 0 or self.limit_price <= 0:
                raise ValueError("new KIS order intents require side, quantity, and limit price")
        return self


class KISPreparedRequest(BaseModel):
    environment: Literal["paper", "prod"]
    path: str = Field(pattern=r"^/uapi/")
    tr_id: str = Field(min_length=9, max_length=12)
    body: dict[str, str]


class KISOrderRequestBuilder:
    """Build the official KIS cash-equity limit-order request contract."""

    US_EXCHANGES = {"NASDAQ": "NASD", "NASD": "NASD", "NYSE": "NYSE", "AMEX": "AMEX"}
    KR_EXCHANGES = {"KRX", "NXT", "SOR"}

    @staticmethod
    def _account(account_number: str, product_code: str) -> None:
        if len(account_number) != 8 or not account_number.isdigit():
            raise ValueError("KIS account number must contain the first eight digits only")
        if len(product_code) != 2 or not product_code.isdigit():
            raise ValueError("KIS product code must contain two digits")

    @staticmethod
    def _price(market: Market, value: float) -> str:
        decimal = Decimal(str(value))
        if not decimal.is_finite() or decimal <= 0:
            raise ValueError("limit price must be a positive finite number")
        if market == Market.KR and decimal != decimal.to_integral_value():
            raise ValueError("Korean cash-equity order prices must be whole won")
        rendered = format(decimal.normalize(), "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered

    @classmethod
    def prepare_new(
        cls,
        intent: KISOrderIntent,
        *,
        account_number: str,
        product_code: str,
        environment: Literal["paper", "prod"],
    ) -> KISPreparedRequest:
        cls._account(account_number, product_code)
        if intent.action != KISOrderAction.NEW or intent.side is None:
            raise ValueError("prepare_new requires a new-order intent")
        price = cls._price(intent.market, intent.limit_price)
        if intent.market == Market.KR:
            if intent.exchange not in cls.KR_EXCHANGES:
                raise ValueError(f"unsupported Korean exchange: {intent.exchange}")
            tr_id = {
                ("prod", KISOrderSide.BUY): "TTTC0012U",
                ("prod", KISOrderSide.SELL): "TTTC0011U",
                ("paper", KISOrderSide.BUY): "VTTC0012U",
                ("paper", KISOrderSide.SELL): "VTTC0011U",
            }[(environment, intent.side)]
            body = {
                "CANO": account_number,
                "ACNT_PRDT_CD": product_code,
                "PDNO": intent.symbol,
                "ORD_DVSN": "00",
                "ORD_QTY": str(intent.quantity),
                "ORD_UNPR": price,
                "EXCG_ID_DVSN_CD": intent.exchange,
                "SLL_TYPE": "01" if intent.side == KISOrderSide.SELL else "",
                "CNDT_PRIC": "",
            }
            path = "/uapi/domestic-stock/v1/trading/order-cash"
        else:
            exchange = cls.US_EXCHANGES.get(intent.exchange)
            if exchange is None:
                raise ValueError(f"unsupported US exchange: {intent.exchange}")
            tr_id = {
                ("prod", KISOrderSide.BUY): "TTTT1002U",
                ("prod", KISOrderSide.SELL): "TTTT1006U",
                ("paper", KISOrderSide.BUY): "VTTT1002U",
                ("paper", KISOrderSide.SELL): "VTTT1001U",
            }[(environment, intent.side)]
            body = {
                "CANO": account_number,
                "ACNT_PRDT_CD": product_code,
                "OVRS_EXCG_CD": exchange,
                "PDNO": intent.symbol,
                "ORD_QTY": str(intent.quantity),
                "OVRS_ORD_UNPR": price,
                "CTAC_TLNO": "",
                "MGCO_APTM_ODNO": "",
                "SLL_TYPE": "00" if intent.side == KISOrderSide.SELL else "",
                "ORD_SVR_DVSN_CD": "0",
                "ORD_DVSN": "00",
            }
            path = "/uapi/overseas-stock/v1/trading/order"
        return KISPreparedRequest(
            environment=environment,
            path=path,
            tr_id=tr_id,
            body=body,
        )

    @classmethod
    def prepare_cancel(
        cls,
        intent: KISOrderIntent,
        *,
        account_number: str,
        product_code: str,
        environment: Literal["paper", "prod"],
        original_order_number: str,
        order_org_number: str = "",
    ) -> KISPreparedRequest:
        cls._account(account_number, product_code)
        if intent.action != KISOrderAction.CANCEL:
            raise ValueError("prepare_cancel requires a cancel intent")
        if not original_order_number:
            raise ValueError("original KIS order number is required for cancellation")
        if intent.market == Market.KR:
            if intent.exchange not in cls.KR_EXCHANGES:
                raise ValueError(f"unsupported Korean exchange: {intent.exchange}")
            request = KISPreparedRequest(
                environment=environment,
                path="/uapi/domestic-stock/v1/trading/order-rvsecncl",
                tr_id="TTTC0013U" if environment == "prod" else "VTTC0013U",
                body={
                    "CANO": account_number,
                    "ACNT_PRDT_CD": product_code,
                    "KRX_FWDG_ORD_ORGNO": order_org_number,
                    "ORGN_ODNO": original_order_number,
                    "ORD_DVSN": "00",
                    "RVSE_CNCL_DVSN_CD": "02",
                    "ORD_QTY": str(intent.quantity),
                    "ORD_UNPR": "0",
                    "QTY_ALL_ORD_YN": "Y",
                    "EXCG_ID_DVSN_CD": intent.exchange,
                },
            )
        else:
            exchange = cls.US_EXCHANGES.get(intent.exchange)
            if exchange is None:
                raise ValueError(f"unsupported US exchange: {intent.exchange}")
            request = KISPreparedRequest(
                environment=environment,
                path="/uapi/overseas-stock/v1/trading/order-rvsecncl",
                tr_id="TTTT1004U" if environment == "prod" else "VTTT1004U",
                body={
                    "CANO": account_number,
                    "ACNT_PRDT_CD": product_code,
                    "OVRS_EXCG_CD": exchange,
                    "PDNO": intent.symbol,
                    "ORGN_ODNO": original_order_number,
                    "RVSE_CNCL_DVSN_CD": "02",
                    "ORD_QTY": str(intent.quantity),
                    "OVRS_ORD_UNPR": "0",
                    "MGCO_APTM_ODNO": "",
                    "ORD_SVR_DVSN_CD": "0",
                },
            )
        return request


class KISOrderTransport:
    """KIS order HTTP transport with a source-code production kill switch.

    The application does not instantiate this transport in record-only mode. It exists so
    request/auth/hash/response handling can be tested against KIS paper trading before a
    separately reviewed release enables any production dispatch path.
    """

    PROD_BASE_URL = "https://openapi.koreainvestment.com:9443"
    PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.transport = transport

    async def dispatch(self, request: KISPreparedRequest) -> dict[str, Any]:
        if request.environment == "prod" and not LIVE_ORDER_TRANSPORT_ENABLED:
            raise KISOrderSafetyError(
                "production KIS order transport is source-code disabled in this release"
            )
        base_url = self.PROD_BASE_URL if request.environment == "prod" else self.PAPER_BASE_URL
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=15,
            transport=self.transport,
        ) as client:
            token_response = await client.post(
                "/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                },
            )
            token_response.raise_for_status()
            access_token = str(token_response.json()["access_token"])
            hash_response = await client.post(
                "/uapi/hashkey",
                headers={
                    "content-type": "application/json",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                },
                json=request.body,
            )
            hash_response.raise_for_status()
            hash_key = str(hash_response.json()["HASH"])
            response = await client.post(
                request.path,
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {access_token}",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                    "tr_id": request.tr_id,
                    "custtype": "P",
                    "hashkey": hash_key,
                },
                json=request.body,
            )
            response.raise_for_status()
            payload = response.json()
        if str(payload.get("rt_cd")) != "0":
            raise KISOrderRejected(
                f"KIS order rejected: {payload.get('msg_cd')} {payload.get('msg1')}"
            )
        return payload


class KISOrderIntentRecorder:
    """Mirror paper-broker decisions into a local, sanitized KIS order-intent ledger."""

    def __init__(
        self,
        repository: Repository,
        *,
        account_configured: bool,
        product_code: str,
    ):
        self.repository = repository
        self.account_configured = account_configured
        self.product_code = product_code

    @staticmethod
    def _request_preview(intent: KISOrderIntent, environment: Literal["paper", "prod"]) -> dict:
        request = KISOrderRequestBuilder.prepare_new(
            intent,
            account_number="00000000",
            product_code="01",
            environment=environment,
        )
        body = {**request.body, "CANO": "<redacted>", "ACNT_PRDT_CD": "<configured>"}
        return {"path": request.path, "tr_id": request.tr_id, "body": body}

    def _store(self, intent: KISOrderIntent, extra: dict[str, Any] | None = None) -> bool:
        payload: dict[str, Any] = {
            "source_order_id": intent.source_order_id,
            "exchange": intent.exchange,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
            "reason": intent.reason,
            "mode": "record_only",
            "live_transport_enabled": LIVE_ORDER_TRANSPORT_ENABLED,
            "account_configured": self.account_configured,
            "product_code_configured": len(self.product_code) == 2,
        }
        if intent.action == KISOrderAction.NEW:
            payload["request_preview"] = {
                "paper": self._request_preview(intent, "paper"),
                "prod": self._request_preview(intent, "prod"),
            }
        else:
            payload["request_preview"] = {
                "ready": False,
                "requires_remote_fields": ["ORGN_ODNO", "KRX_FWDG_ORD_ORGNO"],
            }
        if extra:
            payload.update(extra)
        inserted = self.repository.record_kis_order_intent(
            intent_id=intent.intent_id,
            idempotency_key=intent.idempotency_key,
            market=intent.market,
            symbol=intent.symbol,
            plan_id=intent.plan_id,
            action=intent.action.value,
            side=intent.side.value if intent.side else None,
            status=KISOrderStatus.RECORDED_ONLY.value,
            payload=payload,
        )
        if inserted:
            self.repository.add_event(
                "KIS_ORDER_INTENT_RECORDED",
                intent.market,
                intent.symbol,
                intent.plan_id,
                {
                    "intent_id": intent.intent_id,
                    "source_order_id": intent.source_order_id,
                    "action": intent.action.value,
                    "side": intent.side.value if intent.side else None,
                    "status": KISOrderStatus.RECORDED_ONLY.value,
                },
            )
        return inserted

    def record_new_order(
        self,
        *,
        order_id: str,
        idempotency_key: str,
        plan_id: str,
        market: Market,
        symbol: str,
        exchange: str,
        side: Literal["BUY", "SELL"],
        quantity: int,
        limit_price: float,
        reason: str,
    ) -> bool:
        return self._store(
            KISOrderIntent(
                idempotency_key=idempotency_key,
                source_order_id=order_id,
                plan_id=plan_id,
                market=market,
                symbol=symbol,
                exchange=exchange,
                action=KISOrderAction.NEW,
                side=KISOrderSide(side),
                quantity=quantity,
                limit_price=limit_price,
                reason=reason,
            )
        )

    def record_cancel_order(
        self,
        *,
        order_id: str,
        idempotency_key: str,
        plan_id: str,
        market: Market,
        symbol: str,
        exchange: str,
        remaining_quantity: int,
        reason: str,
    ) -> bool:
        return self._store(
            KISOrderIntent(
                idempotency_key=idempotency_key,
                source_order_id=order_id,
                plan_id=plan_id,
                market=market,
                symbol=symbol,
                exchange=exchange,
                action=KISOrderAction.CANCEL,
                quantity=max(remaining_quantity, 0),
                limit_price=0,
                reason=reason,
            )
        )
