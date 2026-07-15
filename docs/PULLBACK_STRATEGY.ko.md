# 상태 기반 눌림목 재돌파 전략

`strategy_type="pullback_rebreak"`를 사용하면 범용 조건식 대신 다음 상태 머신이
동작합니다.

```text
WAIT_BREAKOUT
  → WAIT_PULLBACK
  → WAIT_REBREAK
  → SIGNAL_TRIGGERED
  → ENTRY_PENDING
```

전략 상태는 SQLite `strategy_states` 테이블에 저장됩니다. 프로그램이 재시작돼도
최초 돌파, 상승 고점, 눌림 저점, ATR 깊이, 거래량 표본을 복원합니다. 거래일이나
연결 ID가 변경되거나 거래가 중단되면 `WAIT_BREAKOUT`으로 초기화합니다.

상태 관찰은 5분 시가 범위가 준비된 뒤 시작할 수 있으며 승인된 진입 시작시각보다
이를 수 있습니다. 다만 실제 `SIGNAL_TRIGGERED`와 주문 제출은 승인된 진입 시간
안에서만 허용됩니다.

## 최초 돌파

다음 조건을 만족하고 설정된 Tick 수와 유지시간을 통과해야 합니다.

```text
last가 opening_range_5_high를 상향 돌파
AND last > vwap_regular
AND ema_9_1m_regular > ema_20_1m_regular > ema_50_1m_regular
AND relative_volume_cumulative_20d_same_time_regular >= 1.5
```

확정 시 `prior_breakout_confirmed`, `breakout_price`, `breakout_at`,
`impulse_high`를 저장하고 `WAIT_PULLBACK`으로 이동합니다.

## 눌림 확인

새로운 상승 고점이 만들어질 때마다 아직 눌림이 시작되지 않은 것으로 보고
기준 고점과 거래량 구간을 다시 설정합니다. 고점 이후 하락만 눌림 깊이로
계산합니다.

```text
pullback_depth_atr = (impulse_high - pullback_low) / atr_14_1m_regular
```

기본 정상 범위는 0.2~0.8 ATR이며, 눌림 지속시간은 2~10분입니다. 가격이 VWAP
또는 EMA20을 허용 범위보다 크게 이탈하거나 0.8 ATR을 넘으면 패턴을 폐기합니다.

눌림 확정에는 다음 조건도 필요합니다.

- RSI 45~60
- 눌림 구간 평균 1분 거래량 ÷ 상승 구간 평균 1분 거래량 ≤ 0.7
- `recent_high_5_1m_regular` 준비 완료

확정 시 `pullback_low`, `pullback_high`, `pullback_depth_atr`,
`pullback_volume_ratio`를 저장하고 `WAIT_REBREAK`으로 이동합니다.

## 재돌파

직전 Tick이 저장된 `pullback_high` 이하이고 현재 Tick이 이를 초과해야 합니다.
동시에 다음 기본 조건을 검사합니다.

- 현재가 > EMA9 및 VWAP
- RSI 50~68
- 상대 거래량 ≥ 1.3
- 스프레드 ≤ 0.15%
- 시장 기준 종목이 VWAP 위
- 현재가 ≥ 승인된 진입 트리거

모든 조건이 충족돼야 기존 보수적 주문 상태 머신에 `ENTRY_PENDING` 주문을
제출합니다. 즉, 전략 신호가 발생해도 즉시 전량체결로 처리되지 않습니다.

## 필수 확장 지표

- `opening_range_5_high`
- `vwap_regular`
- `atr_14_1m_regular`
- `ema_9_1m_regular`, `ema_20_1m_regular`, `ema_50_1m_regular`
- `rsi_14_1m_regular`
- `relative_volume_cumulative_20d_same_time_regular`
- `market_above_vwap_regular`
- `recent_high_5_1m_regular`, `recent_low_5_1m_regular`
- `bar_volume_1m_regular`

모든 지표에는 `indicator_ready=true`와 생성 시각이 필요합니다. KIS REST 시세만
사용할 때는 이 전략이 진입하지 않습니다.

전체 계획 예시는
[`samples/us_pullback_plan.json`](../samples/us_pullback_plan.json)에 있습니다.
수치는 일반화된 초기값이며 종목·시장별 재생 테스트를 거쳐야 합니다.
