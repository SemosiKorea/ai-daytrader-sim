# KIS WebSocket 확장 시세 피드

`daytrader-feed`는 KIS 실시간 시세를 읽고 기존 가상매매 API에 확장 Tick을
전송하는 별도 프로세스입니다. 주문·계좌·잔고·체결통보 API를 사용하지 않습니다.

공식 KIS 샘플을 기준으로 다음 TR만 구독합니다.

- 국내 통합 실시간호가 `H0UNASP0`
- 국내 통합 실시간체결 `H0UNCNT0`
- 해외주식 실시간호가 `HDFSASP0`
- 해외주식 실시간체결 `HDFSCNT0`

참고 자료:

- [KIS 공식 Open API 저장소](https://github.com/koreainvestment/open-trading-api)
- [공식 국내 WebSocket 함수](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_functions_ws.py)
- [공식 해외 WebSocket 함수](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/overseas_stock/overseas_stock_functions_ws.py)

## 실행 전 설정

`.env`에 다음 값을 설정합니다.

```dotenv
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ENV=prod
MARKET_DATA_BEARER=서버와_동일한_24자_이상_랜덤값

FEED_HISTORY_PATH=data/feed_history.db
FEED_TARGET_URL=http://127.0.0.1:8787/v1/market-data/ticks
FEED_DISCOVERY_SECONDS=5.0
FEED_QUOTE_MAX_AGE_SECONDS=3.0
FEED_US_QUOTE_SCOPE=venue
FEED_OVERSEAS_TR_KEY_PREFIX=D
FEED_REFERENCE_KR=069500:KRX
FEED_REFERENCE_US=QQQ:NASDAQ
FEED_SCAN_PREMARKET_MINUTES_KR=30
FEED_SCAN_PREMARKET_MINUTES_US=60
FEED_SCAN_REGULAR_MINUTES=120
```

해외 실시간 시세의 모의 환경 지원 범위는 KIS 상품별로 다를 수 있습니다.
가상체결 프로그램이어도 실시간 시세만 실전 앱키로 읽으려면 `KIS_ENV=prod`를
사용할 수 있습니다. 이 피드에는 주문 함수가 없습니다.

미국 정규장 구독키 기본 형식은 `D` + 거래소코드 + 종목코드입니다. KIS의
주간거래 구독을 별도로 사용하는 경우에만 공식 문서와 서비스 상태를 확인하고
`FEED_OVERSEAS_TR_KEY_PREFIX=R` 및 해당 거래소 구분을 검토하십시오.

## 실행 순서

```bash
uv sync --extra dev
uv run daytrader-sim
```

다른 터미널에서 다음을 실행합니다.

```bash
uv run daytrader-feed
```

피드는 승인된 계획의 종목을 항상 구독합니다. 이와 별도로 한국 정규장 30분 전,
미국 정규장 60분 전부터 `config/universe.yaml`의 해당 시장 전체 종목을 승인 전에
구독하고, 개장 후 기본 120분까지 후보 확인용 데이터를 수집합니다. 두 시장을
시간대별로 나눠 구독해 KIS WebSocket의 연결당 40개 구독 한도를 넘지 않습니다.
대상 종목이 바뀌면 WebSocket을 재연결하며 시장 기준 종목도 함께 구독해
`market_above_vwap_regular`을 계산합니다.

## 계산 및 저장

체결 데이터로 정규장 1분봉과 정확한 거래대금 합계를 구성합니다. 완성된 봉은
`FEED_HISTORY_PATH`의 SQLite에 저장됩니다. 재시작하면 저장된 봉으로 EMA, RSI,
ATR, 최근 5봉, 전일 OHLC를 복구합니다.

장전 체결은 `premarket_open/high/low/last/vwap/volume/gap_pct`로 별도 저장하고
정규장 EMA·RSI·ATR·VWAP 계산에는 넣지 않습니다. API는 장전과 정규장 최신
스냅샷을 SQLite의 서로 다른 세션 행에 보존합니다.

당일 데이터로 다음을 계산합니다.

- 정규장 VWAP
- 5·10·15분 시가 범위
- 시가 갭
- 누적 거래량
- 시장 기준 종목의 VWAP 상·하단 상태

완성봉과 과거 세션으로 다음을 계산합니다.

- EMA 9·20·50
- Wilder RSI 14
- Wilder ATR 14
- 최근 5개 완성봉 고가·저가
- 최근 완성봉 거래량
- 최근 20거래일 동일 경과시각 평균 대비 상대 거래량

## 과거 분봉 가져오기

KIS 국내 당일분봉 API는 전일 분봉을 제공하지 않으므로 20거래일 상대 거래량을
즉시 준비하려면 신뢰할 수 있는 공급자에서 받은 정규장 1분봉을 가져와야 합니다.

```bash
uv run daytrader-feed --import-history verified-bars.csv
```

CSV 필수 열은 다음과 같습니다.

```csv
market,symbol,timestamp,open,high,low,close,volume,notional
US,NVDA,2026-07-14T09:30:00-04:00,170,171,169.9,170.8,120000,20450000
```

- `timestamp`는 시간대를 포함한 1분봉 시작시각입니다.
- `notional`은 선택 항목입니다. 없으면 `close × volume`을 사용하므로 과거 VWAP은
  근사치가 됩니다.
- 거래소 휴장일과 정규장 밖의 봉은 거절합니다.
- 정확한 상대 거래량에는 해당 종목별 이전 20개 공식 세션이 필요합니다.
- 당일 시가 범위를 정확히 쓰려면 피드를 정규장 개장 전에 시작해야 합니다.

## 미국 호가 안전 경계

KIS 공식 샘플은 미국 실시간 1호가를 제공한다고 설명하지만, 이 값이 여러 거래소를
종합한 NBBO라고 명시하지 않습니다. 따라서 기본 설정은 다음과 같습니다.

```dotenv
FEED_US_QUOTE_SCOPE=venue
```

가상매매 엔진은 미국 `venue` 호가의 신규 진입을 거절합니다. 계약한 데이터가
NBBO 또는 동등한 통합호가임을 공급자에게 확인한 경우에만 다음으로 변경합니다.

```dotenv
FEED_US_QUOTE_SCOPE=consolidated
```

단순히 진입 차단을 해제하려는 목적으로 이 값을 변경하면 안 됩니다.

## 알려진 제한

- KIS 해외 실시간 레코드에는 완전한 LULD·거래중단·기업행동 상태가 없습니다.
- 브리지는 국내 `TRHT_YN` 거래정지 표시는 반영하지만 별도 상태 공급자를 대체하지
  않습니다.
- 프로그램 시작 전에 발생한 당일 체결은 자동 복원하지 않습니다.
- 20거래일 분봉이 없으면 상대 거래량이 필요한 눌림목 계획은 진입하지 않습니다.
- 이 피드는 가상매매 입력 전용이며 실제 주문 기능을 추가하지 않습니다.

운영 전에 재생 데이터와 최소 30세션·50회 가상 청산 기준으로 검증하십시오.
