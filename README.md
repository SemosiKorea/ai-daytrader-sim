# AI Day Trader Simulator

한국어 문서: [`README.ko.md`](README.ko.md)

Safety enhancement matrix (Korean):
[`docs/SAFETY_ENHANCEMENTS.ko.md`](docs/SAFETY_ENHANCEMENTS.ko.md)

State-based pullback strategy (Korean):
[`docs/PULLBACK_STRATEGY.ko.md`](docs/PULLBACK_STRATEGY.ko.md)

KIS record-only order gateway (Korean):
[`docs/KIS_ORDER_GATEWAY.ko.md`](docs/KIS_ORDER_GATEWAY.ko.md)

An independent, paper-only program for this workflow:

1. The feed subscribes to the active market's AI/semiconductor universe before approval.
2. A Custom GPT reads a premarket shortlist through a read-only Action and proposes a plan.
3. KR is confirmed after 09:10 KST and US after 09:40 ET using regular volume, spread, and VWAP.
4. The user reviews it and explicitly approves it with a one-time Telegram code.
5. The GPT Action sends the unchanged structured plan to this service.
6. Deterministic validation arms the plan for that market date only.
7. Read-only KIS quotes or an authenticated enriched feed drive simulated fills.
8. Stops, targets, risk limits, and forced close are executed by code, not by an LLM.
9. Order decisions are converted to KIS request contracts and stored locally only.

No OpenAI API key is used by this project. The Custom GPT runs in ChatGPT and calls
the HTTPS Action after approval. ChatGPT/GPT availability is governed by the user's
ChatGPT plan. This software does not provide investment advice or guarantee results.

## Safety boundary

- `PaperBroker` is the only active execution adapter.
- `KISReadOnlyClient` allowlists quotation GET paths and OAuth token creation.
- `place_order()` always raises `LiveOrderCapabilityDisabled`.
- KIS KR/US cash-equity limit/cancel request builders and OAuth/hash/HTTP transport
  contracts exist, but the service never instantiates the transport.
- Runtime configuration accepts only `KIS_ORDER_MODE=record_only`; production dispatch
  is also source-code blocked, so changing `.env` cannot enable live orders.
- Account reconciliation and execution notices are not implemented, so this is not
  ready for live trading.
- KR and US require distinct, single-use, 45-minute approval codes.
- One plan per market/date, maximum three candidates, and one reserved market slot
  shared by pending entry orders and open positions.
- Position size is capped by per-trade risk, total daily risk, cash, and a fixed
  maximum quantity. Daily risk includes realized loss, open stop risk, and pending
  order risk.
- Forced close follows the official exchange calendar: KR close minus 15 minutes
  and US close minus 10 minutes, including holidays, DST, and early closes.
- Portfolio state, plans, hashes, and an audit log persist in SQLite WAL mode.

## Install

Requirements: macOS, `uv`, Python 3.11+, KIS Open API quotation credentials, and
optionally a Telegram bot and ngrok.

```bash
git clone https://github.com/SemosiKorea/ai-daytrader-sim.git
cd ai-daytrader-sim
uv sync --extra dev
cp .env.example .env
```

Generate three different long random bearer secrets and put them in `.env`.
Restrict `.env` to the local user. Never paste KIS or Telegram secrets into a GPT.
The service refuses to start with placeholder, duplicate, or shorter-than-24-character
bearer secrets.

Before the service accepts a plan, set `sell_tax_bps` for both markets in
`config/costs.yaml` from the current official KIS/exchange fee schedule. The values
are intentionally `null` by default because these charges change; guessing them
would invalidate the paper results.

Run locally:

```bash
uv run daytrader-sim
curl http://127.0.0.1:8787/healthz
```

The event dashboard is protected with `ADMIN_BEARER`:

```bash
curl -H "Authorization: Bearer $ADMIN_BEARER" http://127.0.0.1:8787/
```

Inspect locally recorded KIS order intents through the admin-only endpoint:

```bash
curl -H "Authorization: Bearer $ADMIN_BEARER" \
  http://127.0.0.1:8787/v1/admin/kis-order-intents
```

## Market data modes

The normative feed and indicator definitions are documented in
[`docs/INDICATOR_CONTRACT.ko.md`](docs/INDICATOR_CONTRACT.ko.md).
Setup and operational limitations of the included KIS WebSocket bridge are in
[`docs/ENRICHED_FEED.ko.md`](docs/ENRICHED_FEED.ko.md).

The included `daytrader-feed` process discovers symbols in armed plans, subscribes
to read-only KIS WebSocket trade/quote feeds, calculates the indicator contract,
and posts authenticated ticks to the simulator:

```bash
# Run the API first, then the feed in a second terminal.
uv run daytrader-sim
uv run daytrader-feed
```

The target endpoint also accepts another verified enriched feed:

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

Completed one-minute bars persist in `data/feed_history.db`. The exact 20-session
same-time relative-volume indicator remains unready until 20 prior sessions have
been collected or imported with `daytrader-feed --import-history bars.csv`.

KIS overseas WebSocket provides a real-time top quote but the official sample does
not establish that it is NBBO. `FEED_US_QUOTE_SCOPE` therefore defaults to `venue`,
which intentionally blocks US entry. Set it to `consolidated` only after confirming
the entitlement and scope of the supplied quote with the data provider. KIS does
not provide the full LULD/corporate-action status contract in these quote records;
those safeguards require an additional verified status overlay before production-like
evaluation.

Set `KIS_POLL_ENABLED=true` to use the quotation-only KIS REST fallback instead.
It polls only symbols in an armed plan. It supplies price/spread data, not VWAP,
RSI, opening range, or relative volume; plans that reference missing indicators do
not enter. US entry also requires the feed to identify a consolidated quote;
the REST fallback reports `quote_scope=unknown` and therefore cannot arm a US fill
by itself.

## Daily approval workflow

The scheduler sends KR OTP at 08:15 KST and US OTP at 08:45 ET on exchange sessions.
The user asks the Custom GPT for the daily plan, reviews exact JSON-equivalent terms,
then says they approve and types the relevant OTP. The GPT Action registers the plan.
Discussion or a generic “looks good” must not trigger the Action.

The Custom GPT first calls `GET /v1/gpt-actions/candidates`. The KR premarket window
starts at 08:40 KST and the US window starts 45 minutes before the regular open;
regular confirmation begins at 09:10 KST and 09:40 ET respectively. A regular open
outside the approved guard permanently marks that candidate RISK_BLOCKED for the
day. No entry order is created before regular relative-volume, spread, and VWAP
confirmation or while ask exceeds the maximum entry limit.

For manual testing, issue an OTP with the admin endpoint:

```bash
curl -X POST http://127.0.0.1:8787/v1/admin/nonces \
  -H "Authorization: Bearer $ADMIN_BEARER" \
  -H "Content-Type: application/json" \
  -d '{"market":"KR","trade_date":"2026-07-15"}'
```

Then replace the sample's date, expiry, and OTP and post it using
`GPT_ACTION_BEARER`. `samples/kr_plan.json` is schema-only and is not a recommendation.

## Custom GPT and ngrok

1. Create a Custom GPT (a GPT in ChatGPT) and paste
   `docs/CUSTOM_GPT_INSTRUCTIONS.md` into its instructions.
2. Install ngrok, register its account authtoken locally, and start it with
   `deploy/ngrok-traffic-policy.yml`. The policy exposes only `/v1/gpt-actions/*`;
   admin, dashboard, portfolio, and market-data paths remain local.
3. Replace the ngrok placeholder in `gpt_action_openapi.yaml`, import it as an
   Action, and configure bearer/API-key authentication with `GPT_ACTION_BEARER`.
4. Test with a new OTP and confirm a `201` receipt and matching content hash.

The launchd templates in `deploy/` keep the API, feed, and ngrok running. Replace all
absolute path placeholders before installing manually, or run
`scripts/install_runtime_launch_agents.sh` to populate and install the API and feed
agents automatically. After the ngrok authtoken has been registered,
`scripts/install_ngrok_launch_agent.sh` installs its populated launch agent. See
[`docs/NGROK_SETUP.ko.md`](docs/NGROK_SETUP.ko.md) for the complete setup and checks.

## Rules and simulated fills

Candidate `strategy_type` supports ordinary `rules` and the persistent,
sequence-aware `pullback_rebreak` state machine. Boolean predicates support `eq`
and `ne`; pullback state and derived metrics are persisted in SQLite.

- A valid signal creates `ENTRY_PENDING`; it does not create an immediate fill.
  Pending orders reserve the market slot, wait the configured latency, fill only
  against visible liquidity, allow conservative partial fills, and expire after the
  timeout. The first partial fill consumes one daily entry.
- Quantity is `min(risk-budget quantity, cash quantity, maximum quantity)`. Order
  submissions and filled entries have separate daily limits.
- Stop triggers use executable `bid`, not `last`. A gap below the marketable-limit
  boundary remains `EXIT_PENDING` until the emergency timeout, then uses the next
  valid bid. Targets also require `bid >= target`.
- Reward/risk is validated from the maximum entry limit using expected fees,
  slippage, taxes, and the conservative stop execution price.
- Source/receive timestamps, sequence order, session, quote scope, crossed/locked
  markets, halt/LULD state, and per-indicator readiness/timestamps are validated.
- Cross predicates support confirmation ticks, hold duration, minimum margin, and
  cooldown. State resets on session/connection changes and halt recovery.
- Maximum holding time and optional no-progress/VWAP-failure exits are supported.
- Every state transition, fill, cancellation, data rejection, and entry rejection
  is written to the audit log with reason codes.

The allowlist is in `config/universe.yaml`. Validation also rejects empty ordinary
rule groups unless explicitly price-only, contradictory conditions, duplicate targets,
ambiguous indicator names, incomplete
opening ranges, non-session dates, invalid plan versions/timestamps, and corporate
action invalidations.

## Evaluation

Use `GET /v1/performance/KR` and `/US` with `ADMIN_BEARER`. Metrics are computed from
closed paper positions persisted in the audit log: trade count, net P&L, profit
factor, and maximum drawdown. The intended gate is at least 30 market sessions and
50 closed trades combined, positive net P&L, profit factor >=1.2, and MDD <=5%.
Passing a paper test is not evidence that live execution will perform similarly.

## Test

```bash
uv run ruff check .
uv run pytest
```

The tests cover schema/risk rejection, one-time approval, rule crossing, simulated
fills, restart persistence, stale ticks, official KIS order request construction,
and pre-network production-order blocking.
