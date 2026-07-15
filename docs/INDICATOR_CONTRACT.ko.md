# 실시간 시세·지표 계약

확장 시세 브리지는 아래 정의와 동일한 값을 `/v1/market-data/ticks`로 보내야
합니다. 계산 정의가 다른 값에 같은 이름을 사용하면 안 됩니다.

## 공통 시세 필드

- `source_timestamp`: 거래소 또는 원천 피드가 생성한 시각
- `received_timestamp`: 브리지가 수신한 시각
- `sequence_id`: 연결 안에서 단조 증가하는 정수
- `connection_id`: 재연결할 때마다 바뀌는 연결 식별자
- `session`: `premarket`, `regular`, `afterhours`, `closed` 중 하나
- `market_status`: `open`, `closed`, `halted` 중 하나
- `symbol_status`: `trading`, `halted`, `paused` 중 하나
- `luld_status`: `normal`, `limit_up`, `limit_down`, `paused` 중 하나
- `quote_scope`: 미국 통합 최우선 호가가 확인되면 `consolidated`; 단일 거래소
  호가는 `venue`; 확인되지 않으면 `unknown`

미국 신규 진입은 정규장·거래 가능 상태·정상 LULD·통합호가일 때만 허용됩니다.
`bid > ask`인 crossed market과 `bid == ask`인 locked market에서는 진입하지
않습니다.

## 지표 정의

- `vwap_regular`: 당일 정규장 체결에 대해
  `누적(체결가격 × 체결수량) / 누적체결수량`으로 계산하며 매 거래일 초기화합니다.
- `ema_9_1m_regular`, `ema_20_1m_regular`, `ema_50_1m_regular`: 완성된 정규장
  1분봉 종가의 EMA입니다. 프리마켓 봉은 제외하고 정규장 봉은 거래일 사이에
  연속합니다. 최초 시드는 해당 기간 종가의 단순평균입니다.
- `rsi_14_1m_regular`: 완성된 정규장 1분봉 종가에 Wilder 방식 14기간을
  적용합니다. 정규장 봉만 거래일 사이에 연속합니다.
- `atr_14_1m_regular`: 완성된 정규장 1분봉에 Wilder 방식 14기간을 적용합니다.
  첫 정규장 봉의 True Range는 직전 거래일 정규장 종가를 이전 종가로 사용합니다.
- `opening_range_N_high/low`: 정규장 개장부터 정확히 N분 동안 완성된 거래의
  고가·저가입니다. N분이 끝나기 전에는 ready가 될 수 없습니다.
- `relative_volume_cumulative_20d_same_time_regular`: 현재 정규장 시각까지의
  당일 누적거래량을 최근 20거래일의 동일 경과시각 평균 누적거래량으로 나눈
  값입니다.
- `gap_pct`: 당일 첫 정규장 체결가와 기업행동이 반영된 직전 정규장 종가의
  차이를 직전 종가의 백분율로 표시합니다.
- `previous_open/high/low/close`: 기업행동이 반영된 직전 공식 정규장 가격입니다.
- `market_above_vwap_regular`: 사전에 정한 시장 기준 종목이 자신의 정규장
  VWAP 위에 있으면 `true`입니다. 기준 종목은 브리지 설정과 로그에 기록해야 합니다.
- `recent_high_5_1m_regular`, `recent_low_5_1m_regular`: 현재 진행 중인 봉을
  제외한 최근 5개 완성 정규장 1분봉의 최고가와 최저가입니다.
- `bar_volume_1m_regular`: 가장 최근에 완성된 정규장 1분봉 거래량입니다.
  동일 봉을 여러 Tick에서 전송할 때는 같은 `indicator_timestamp`를 사용해야 하며,
  상태 엔진은 같은 시각의 거래량을 한 번만 집계합니다.

## 지표 메타데이터

`indicators`에 포함한 모든 지표에는 다음 두 필드를 함께 보내야 합니다.

```json
{
  "indicators": {"vwap_regular": 179.4},
  "indicator_ready": {"vwap_regular": true},
  "indicator_timestamps": {"vwap_regular": "2026-07-15T10:31:12.123-04:00"}
}
```

완성된 1분봉 지표는 마지막 완성 봉의 종료시각을 사용합니다. 시가 범위는 범위가
확정된 시각을 사용합니다. 연결 재수립, 세션 전환, 거래중단 복구 뒤에는 필요한
히스토리를 다시 구성할 때까지 `indicator_ready=false`로 전송합니다.

기업행동이 확인되면 `corporate_action=true`로 전송합니다. 프로그램은 관련 계획을
취소하고 신규 진입을 금지하며, 거래 가능한 유효 bid가 있으면 보유 가상 포지션을
청산합니다.
