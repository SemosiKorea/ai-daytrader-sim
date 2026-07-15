# KIS 주문 게이트웨이: 현재는 로컬 기록 전용

이 모듈은 가상매매 엔진이 내린 진입·취소·청산 결정을 KIS 주문 형식으로
변환할 수 있도록 준비합니다. 현재 애플리케이션은 주문 의도를 SQLite에만
기록하며 KIS 모의투자나 실전투자 주문 API를 호출하지 않습니다.

## 구현된 주문 계약

공식 KIS 샘플을 기준으로 다음 현금주식 지정가 주문을 지원합니다.

- 국내 현금 매수·매도: `/uapi/domestic-stock/v1/trading/order-cash`
- 국내 주문 취소: `/uapi/domestic-stock/v1/trading/order-rvsecncl`
- 미국 주식 매수·매도: `/uapi/overseas-stock/v1/trading/order`
- 미국 주문 취소: `/uapi/overseas-stock/v1/trading/order-rvsecncl`
- OAuth 접근토큰: `/oauth2/tokenP`
- 요청 본문 Hashkey: `/uapi/hashkey`

국내는 KRX·NXT·SOR, 미국은 NASDAQ·NYSE·AMEX 코드 변환을 구현했습니다.
시장가·신용·예약·주간거래 주문은 현재 범위에 포함하지 않습니다.

공식 참고 자료:

- [KIS 공식 Open API 저장소](https://github.com/koreainvestment/open-trading-api)
- [국내 현금주문 공식 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_cash/order_cash.py)
- [국내 정정취소 공식 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_rvsecncl/order_rvsecncl.py)
- [해외주식 주문 공식 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/order/order.py)
- [해외주식 정정취소 공식 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/order_rvsecncl/order_rvsecncl.py)

## 현재 실행 흐름

```text
매매 신호
  -> PaperBroker 주문/취소/청산 결정
  -> KIS 요청 필드와 TR ID 미리보기 생성
  -> kis_order_intents SQLite 테이블에 RECORDED_ONLY로 저장
  -> 종료
```

네트워크 주문 전송 단계는 애플리케이션 실행 흐름에 연결되어 있지 않습니다.
`KIS_ENV=prod`는 시세 클라이언트 설정일 뿐 주문 모드를 바꾸지 않습니다.

기록은 관리자 전용 API에서 확인할 수 있습니다.

```bash
curl -H "Authorization: Bearer $ADMIN_BEARER" \
  'http://127.0.0.1:8787/v1/admin/kis-order-intents?limit=100'
```

저장 항목에는 주문 방향·수량·지정가·사유·공식 API 경로·TR ID·요청 필드
미리보기가 포함됩니다. 계좌번호는 `<redacted>`로 저장하며 앱키, 앱시크릿,
접근토큰 및 Hashkey는 저장하지 않습니다.

## 실거래 방지 장치

현재 버전은 다음 장치를 동시에 적용합니다.

1. `KIS_ORDER_MODE`는 스키마상 `record_only`만 허용합니다.
2. API 서버는 `KISOrderTransport`를 생성하거나 실행하지 않습니다.
3. 주문 전송용 외부 API 엔드포인트가 없습니다.
4. 소스코드 전송 상수 `LIVE_ORDER_TRANSPORT_ENABLED`는 `False`입니다.
5. 프로덕션 요청은 OAuth 호출 전에 `KISOrderSafetyError`로 거절됩니다.
6. 가상 체결의 주문 의도마다 별도 idempotency key를 저장합니다.

따라서 `.env` 값만 바꿔서는 실전 주문을 활성화할 수 없습니다. KIS 모의투자
HTTP 전송기 자체는 MockTransport 기반 테스트로 토큰·Hashkey·주문 순서를
검증하지만 현재 서비스에서는 호출하지 않습니다.

## 나중에 실거래를 검토할 때 필요한 추가 작업

현재 차단을 해제하는 것만으로 실거래 준비가 끝나는 것은 아닙니다. 최소한 다음
항목을 별도 변경과 코드리뷰로 구현해야 합니다.

- KIS 잔고·주문가능금액과 로컬 포지션의 시작 전 대사
- 주문 접수 응답의 주문번호·조직번호 영구 저장
- 실시간 체결통보 또는 주문체결조회 기반 부분체결 동기화
- 정정취소 가능수량 확인 후 취소 요청
- 재시작·응답 유실·타임아웃 후 주문번호 기준 재조정
- 별도 실거래 승인 토큰, 금액 상한, 종목 상한 및 kill switch
- 장 마감 후 미체결 주문과 실제 잔고의 강제 대사
- KIS 모의투자 환경에서 충분한 통합 테스트

실거래 활성화는 이 문서의 범위 밖이며 현재 프로그램의 안전 보장을 무효화하는
별도 배포 변경으로 취급해야 합니다.
