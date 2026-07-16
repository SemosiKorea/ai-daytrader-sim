# AI 단타 가상매매 시뮬레이터

보완사항 구현 대조표: [`docs/SAFETY_ENHANCEMENTS.ko.md`](docs/SAFETY_ENHANCEMENTS.ko.md)

눌림목 전략: [`docs/PULLBACK_STRATEGY.ko.md`](docs/PULLBACK_STRATEGY.ko.md)

포트폴리오 비교실험: [`docs/PORTFOLIO_EXPERIMENT.ko.md`](docs/PORTFOLIO_EXPERIMENT.ko.md)

KIS 주문 기록 게이트웨이: [`docs/KIS_ORDER_GATEWAY.ko.md`](docs/KIS_ORDER_GATEWAY.ko.md)

다음 흐름을 위한 독립형 가상매매 전용 프로그램입니다.

1. 시세 피드가 승인 전에도 현재 거래시간대의 다중 테마 유니버스를 구독합니다.
2. Custom GPT가 읽기 전용 Action으로 장전 후보를 조회하고 당일 계획을 제안합니다.
3. 한국은 09:10 KST, 미국은 09:40 ET 이후 정규장 거래량·스프레드·VWAP을 재확인합니다.
4. 사용자가 계획을 검토한 후 텔레그램 일회용 코드로 명시적으로 승인합니다.
5. GPT Action이 승인된 구조화 계획을 이 서비스로 전송합니다.
6. 프로그램이 결정론적 검증을 거쳐 해당 시장의 당일 계획만 활성화합니다.
7. 읽기 전용 KIS 시세 또는 인증된 확장 시세 피드로 가상 체결을 실행합니다.
8. 손절, 익절, 위험 제한, 장 마감 청산은 LLM이 아니라 프로그램 코드가 수행합니다.
9. 주문 결정은 KIS 요청 형식으로 변환해 로컬 원장에만 기록합니다.

이 프로젝트는 OpenAI API 키를 사용하지 않습니다. Custom GPT는 ChatGPT에서
동작하며, 사용자의 승인 이후 HTTPS Action을 호출합니다. ChatGPT와 GPT 사용
가능 여부는 사용자의 ChatGPT 요금제 및 정책을 따릅니다. 이 프로그램은 투자
자문을 제공하지 않으며 수익을 보장하지 않습니다.

## 안전 경계

- 유일하게 활성화된 체결 어댑터는 `PaperBroker`입니다.
- `KISReadOnlyClient`는 시세 조회 GET 경로와 OAuth 토큰 발급만 허용합니다.
- `place_order()`는 항상 `LiveOrderCapabilityDisabled` 예외를 발생시킵니다.
- 국내·미국 현금주식 지정가 주문과 취소 요청 빌더, OAuth·Hashkey·HTTP 전송
  계약은 구현되어 있지만 서비스 실행 경로에는 연결되어 있지 않습니다.
- `KIS_ORDER_MODE=record_only`만 허용되고 프로덕션 전송은 소스코드 단계에서
  차단됩니다. `.env` 변경만으로 실거래를 활성화할 수 없습니다.
- 계좌·잔고 대사와 주문 체결통보는 아직 구현하지 않았으므로 실거래 준비
  상태가 아닙니다.
- 한국과 미국 시장은 각각 별도의 일회용 45분 승인코드가 필요합니다.
- 시장별 하루 한 계획과 후보 최대 3개를 허용합니다. 미체결 진입 주문과
  보유 포지션이 시장별 슬롯 하나를 함께 사용합니다.
- 수량은 거래당 허용위험, 당일 총위험, 현금 및 고정 최대수량 중 가장 작은
  값으로 계산합니다. 당일 총위험에는 실현손실, 보유 포지션 손절위험 및
  미체결 주문 예정위험이 포함됩니다.
- 강제청산은 공식 거래소 달력을 사용합니다. 한국은 공식 종료 15분 전,
  미국은 조기폐장과 서머타임을 포함해 공식 종료 10분 전입니다.
- 가상 포트폴리오, 계획, 해시 및 감사 로그는 SQLite WAL 모드로 저장됩니다.

## 설치

필요 환경은 macOS, `uv`, Python 3.11 이상, KIS Open API 시세 조회 인증정보이며,
텔레그램 봇과 ngrok은 선택 사항입니다.

```bash
git clone https://github.com/SemosiKorea/ai-daytrader-sim.git
cd ai-daytrader-sim
uv sync --extra dev
cp .env.example .env
```

서로 다른 충분히 긴 무작위 Bearer Secret 세 개를 생성하여 `.env`에 입력하십시오.
`.env`는 현재 사용자만 읽을 수 있도록 제한하고, KIS 및 텔레그램 인증정보를
GPT 대화에 붙여 넣지 마십시오. 프로그램은 기본값, 중복값 또는 24자 미만의
Bearer Secret이 설정되면 시작을 거부합니다.

서비스가 거래계획을 승인하려면 `config/costs.yaml`의 두 시장
`sell_tax_bps`에 현재 공식 KIS·거래소 비용 기준을 입력해야 합니다. 비용은
변경될 수 있으므로 기본값을 의도적으로 `null`로 두었습니다. 임의의 값을
입력하면 가상매매 결과가 왜곡될 수 있습니다.

로컬 실행:

```bash
uv run daytrader-sim
curl http://127.0.0.1:8787/healthz
```

이벤트 대시보드는 `ADMIN_BEARER`로 보호됩니다.

```bash
curl -H "Authorization: Bearer $ADMIN_BEARER" http://127.0.0.1:8787/
```

로컬에 기록된 KIS 주문 의도는 다음 관리자 API에서 확인합니다.

```bash
curl -H "Authorization: Bearer $ADMIN_BEARER" \
  http://127.0.0.1:8787/v1/admin/kis-order-intents
```

## 시세 데이터 방식

확장 피드의 정확한 계산 규약은
[`docs/INDICATOR_CONTRACT.ko.md`](docs/INDICATOR_CONTRACT.ko.md)를 따릅니다.
내장 KIS WebSocket 피드의 설정과 운용 제한은
[`docs/ENRICHED_FEED.ko.md`](docs/ENRICHED_FEED.ko.md)에 정리되어 있습니다.

내장 `daytrader-feed` 프로세스는 승인된 계획의 종목을 자동 발견하고 KIS의
읽기 전용 WebSocket 체결·호가를 구독합니다. 기술지표를 계산한 뒤 인증된
Tick을 시뮬레이터로 전송합니다.

```bash
# API를 먼저 실행하고 두 번째 터미널에서 피드를 실행합니다.
uv run daytrader-sim
uv run daytrader-feed
```

다른 검증된 확장 피드도 동일한 입력 API를 사용할 수 있습니다.

```bash
curl -X POST http://127.0.0.1:8787/v1/market-data/ticks \
  -H "Authorization: Bearer $MARKET_DATA_BEARER" \
  -H "Content-Type: application/json" \
  -d '{"market":"US","symbol":"NVDA","timestamp":"2026-07-15T14:00:00Z",\
       "source_timestamp":"2026-07-15T14:00:00Z",\
       "received_timestamp":"2026-07-15T14:00:00.2Z","sequence_id":101,\
       "connection_id":"feed-20260715-1","data_source":"verified-nbbo-feed",\
       "session":"regular","market_status":"open","symbol_status":"trading",\
       "luld_status":"normal",\
       "quote_scope":"consolidated","last":180.0,"bid":179.99,"ask":180.01,\
       "ask_size":25,"trade_size":100,"indicators":{"vwap_regular":179.4},\
       "indicator_ready":{"vwap_regular":true},\
       "indicator_timestamps":{"vwap_regular":"2026-07-15T14:00:00Z"}}'
```

완성된 1분봉은 `data/feed_history.db`에 보존됩니다. 정확한 20거래일 동시간
상대 거래량은 과거 20세션이 누적되거나 아래 명령으로 KIS 미국 정규장 분봉을
가져오기 전까지 `ready=false`입니다.

```bash
uv run daytrader-feed --sync-kis-us-history --history-sessions 20
```

별도 공급자 CSV는 `daytrader-feed --import-history verified-bars.csv`로 가져올 수
있습니다. 동기화 결과의 모든 종목이 `ready: true`인지 확인해야 합니다.

KIS 해외 WebSocket은 미국 실시간 1호가를 제공하지만 공식 샘플만으로 NBBO임을
확정할 수 없습니다. 따라서 `FEED_US_QUOTE_SCOPE` 기본값은 `venue`이고, 이 상태에서는
기존 안전 규칙에 따라 미국 신규 진입이 차단됩니다. 데이터 공급자에게 호가 범위를
확인한 경우에만 `consolidated`로 변경해야 합니다. 해당 실시간 레코드만으로는 전체
LULD·기업행동 상태도 제공되지 않으므로 실제 운용 수준의 평가에는 별도의 검증된
상태 피드가 필요합니다.

내장된 KIS REST 시세 조회 보조 기능을 대신 사용하려면 `KIS_POLL_ENABLED=true`로
설정합니다. 이 기능은 활성화된 계획에 포함된 종목만 조회합니다. 가격과
스프레드 정보는 제공하지만 VWAP, RSI, 시가 범위, 상대 거래량은 계산하지
않습니다. 계획에서 참조한 지표가 Tick에 없으면 해당 조건은 거짓으로 처리되어
진입하지 않습니다. 미국 시장 진입에는 통합호가임이 확인된
`quote_scope=consolidated`가 필요합니다. REST 보조 기능의 호가 범위는 확인되지
않은 것으로 처리되므로 이것만으로는 미국 종목을 가상 체결하지 않습니다.

## 매일 승인 절차

스케줄러는 거래소 영업일에 한국 시장 승인코드를 08:15 KST, 미국 시장
승인코드를 08:45 ET에 전송합니다. 사용자는 Custom GPT에 당일 계획을 요청한
후 모든 가격과 규칙을 검토하고, 승인 의사와 해당 시장의 OTP를 입력합니다.
Custom GPT는 그때 GPT Action으로 계획을 등록합니다. 단순한 계획 논의나
“괜찮아 보인다”라는 표현만으로는 Action을 호출하면 안 됩니다.

Custom GPT는 계획 전에 `GET /v1/gpt-actions/candidates`를 호출합니다. 한국 장전
후보 창은 08:40부터, 미국 장전 후보 창은 정규장 45분 전부터 열립니다. 정규장
확인은 각각 09:10 KST와 09:40 ET부터 가능합니다. 장전 계획의 기준가격 대비
정규장 시가가 허용 범위를 벗어나면 종목은 당일 `RISK_BLOCKED`로 영구 차단되며,
정규장 상대 거래량·스프레드·VWAP 확인 전이나 ask가 최대 지정가보다 높을 때는
진입 주문 자체를 만들지 않습니다.

관리자 API로 승인코드를 수동 발급하여 테스트할 수도 있습니다.

```bash
curl -X POST http://127.0.0.1:8787/v1/admin/nonces \
  -H "Authorization: Bearer $ADMIN_BEARER" \
  -H "Content-Type: application/json" \
  -d '{"market":"KR","trade_date":"2026-07-15"}'
```

샘플 파일의 날짜, 만료 시각, OTP를 바꾼 후 `GPT_ACTION_BEARER`를 사용해
전송할 수 있습니다. `samples/kr_plan.json`은 스키마 설명용이며 현재 종목
추천이 아닙니다.

## Custom GPT 및 ngrok 설정

1. Custom GPT를 만들고 `docs/CUSTOM_GPT_INSTRUCTIONS.ko.md` 내용을 GPT의
   Instructions 항목에 붙여 넣습니다.
2. ngrok을 설치하고 계정 Authtoken을 맥에 등록한 다음
   `deploy/ngrok-traffic-policy.yml` 정책과 함께 실행합니다. 이 정책은
   `/v1/gpt-actions/*`만 외부에 공개하며 관리자, 대시보드, 포트폴리오 및
   시세 입력 경로는 로컬에 남깁니다.
3. `gpt_action_openapi.yaml`의 ngrok 주소 자리표시자를 실제 주소로
   변경한 후 Action으로 가져옵니다. 인증에는 `.env`의
   `GPT_ACTION_BEARER`와 동일한 Bearer 또는 API 키를 설정합니다.
4. 새로운 OTP로 시험하고 응답 코드 `201`, 계획 상태 및 Content Hash가
   올바른지 확인합니다.

`deploy/`의 launchd 템플릿으로 API, 피드 및 ngrok을 계속 실행할 수 있습니다.
API와 피드 템플릿은 `~/Library/LaunchAgents`에 설치하기 전에 모든 절대경로
자리표시자를 실제 경로로 바꾸거나 `scripts/install_runtime_launch_agents.sh`로
두 LaunchAgent를 자동 설치하십시오. ngrok 토큰 등록 후에는
`scripts/install_ngrok_launch_agent.sh`가 ngrok LaunchAgent를 자동 설치합니다.
전체 설정과 검증 절차는 [`docs/NGROK_SETUP.ko.md`](docs/NGROK_SETUP.ko.md)를
참고하십시오.

## 매매 규칙과 가상 체결

후보의 `strategy_type`은 일반 DSL·가격 규칙을 사용하는 `rules`와 시간 순서를
추적하는 `pullback_rebreak`를 지원합니다. 불리언 조건에는 `eq`, `ne`를 사용할
수 있습니다. 눌림목 전략 상태와 계산값은 SQLite에 저장됩니다.

- 유효한 진입 신호는 즉시 체결이 아니라 `ENTRY_PENDING` 주문을 만듭니다.
  주문은 시장 슬롯을 예약하고 가상 지연 후 호가 잔량 범위에서만 부분체결되며,
  제한시간이 지나면 잔여 수량이 취소됩니다. 최초 부분체결 때 진입 횟수를
  차감합니다.
- 수량은 위험예산 수량, 현금 수량, 최대 허용수량 중 최솟값입니다. 주문 제출
  횟수와 실제 진입 횟수는 별도로 제한합니다.
- 손절은 `last`가 아니라 실제 매도 가능한 `bid`로 트리거합니다. 급락으로
  시장성 지정가 아래로 건너뛰면 `EXIT_PENDING` 상태에서 비상 제한시간을 기다린
  후 다음 유효 bid로 청산합니다. 손절·익절·시간청산도 가상 지연 후 `bid_size`
  또는 거래량 참여율 범위에서 부분체결됩니다. 익절은 `bid >= target`일 때만
  주문을 시작합니다.
- 최대 지정가와 예상 비용·슬리피지·세금·보수적인 손절 체결가를 기준으로
  손익비를 검증합니다.
- 원천·수신 시각, 시퀀스, 세션, 호가 범위, crossed/locked 상태, 거래중단,
  LULD 및 지표별 준비 상태와 생성 시각을 검사합니다.
- 교차 조건에는 확인 Tick 수, 유지시간, 최소 돌파폭 및 재발동 대기시간을
  설정할 수 있습니다. 연결·세션 변경이나 거래중단 복구 시 상태를 초기화합니다.
- 최대 보유시간, 진행 부진 및 VWAP 이탈 청산을 지원합니다.
- 모든 주문 상태, 체결, 취소, 데이터 거절 및 미진입 사유를 감사 로그에 남깁니다.

허용 종목은 `config/universe.yaml`에 정의되어 있습니다. 한국과 미국은 각각
18개 종목이며, 기준 종목까지 포함하면 시장별 19종목·38개 WebSocket 구독을
사용합니다. 기존 AI·반도체 외에 한국은 2차전지·바이오·방산·전력 인프라,
미국은 전기차·로보틱스·AI 데이터 분석·사이버보안·전력 테마를 포함합니다.
통과한 후보 응답에는 `theme`이 함께 반환됩니다. 명시적 가격 전용이 아닌
빈 일반 규칙, 모순된 조건, 중복 익절가격, 모호한 지표명, 완성 전 시가 범위,
휴장일, 잘못된 계획
버전·시각도 거절합니다. 기업행동이 감지되면 해당 계획을 자동 무효화합니다.

## 성과 평가

GPT 전체 후보와 사용자 선택 효과를 분리하려면 승인코드로
`POST /v1/gpt-actions/experiments`를 한 번 호출합니다. 프로그램은 기존 수동
포트폴리오와 분리된 `GPT_ALL_EQUAL`, `USER_FIXED_SLEEVE`,
`USER_REALLOCATED` 가상계좌를 생성하고 동일 Tick·체결 모델을 적용합니다.
실험 결과는 `GET /v1/gpt-actions/experiments/{experiment_id}`에서 조회합니다.
실험 계좌는 KIS 주문 의도 기록기를 사용하지 않습니다.

`ADMIN_BEARER` 인증으로 `GET /v1/performance/KR` 또는
`GET /v1/performance/US`를 호출합니다. 감사 로그에 저장된 청산 포지션을
기준으로 거래 횟수와 Profit Factor를 계산하고, Tick별 평가금액 시계열로
미실현손익을 포함한 순손익과 최대 낙폭을 계산합니다.

목표 검증 기준은 두 시장 합산 최소 30거래 세션 및 50회 청산 거래, 순손익
양수, Profit Factor 1.2 이상, MDD 5% 이하입니다. 가상매매 기준을 통과해도
실제 매매에서 같은 성과가 나온다는 의미는 아닙니다.

## 테스트

```bash
uv run ruff check .
uv run pytest
```

테스트에는 스키마 및 위험 조건 거절, 일회용 승인, 교차 조건, 가상 체결,
재시작 후 상태 복원, 오래된 Tick 거절, 공식 KIS 주문 요청 생성 및 프로덕션
전송 사전 차단이 포함됩니다.
