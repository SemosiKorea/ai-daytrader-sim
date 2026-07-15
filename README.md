# AI Day Trader Simulator

한국어 문서: [`README.ko.md`](README.ko.md)

Safety enhancement matrix (Korean):
[`docs/SAFETY_ENHANCEMENTS.ko.md`](docs/SAFETY_ENHANCEMENTS.ko.md)

State-based pullback strategy (Korean):
[`docs/PULLBACK_STRATEGY.ko.md`](docs/PULLBACK_STRATEGY.ko.md)

An independent, paper-only program for this workflow:

1. A Custom GPT proposes one KR or US AI/semiconductor intraday plan.
2. The user reviews it and explicitly approves it with a one-time Telegram code.
3. The GPT Action sends the unchanged structured plan to this service.
4. Deterministic validation arms the plan for that market date only.
5. Read-only KIS quotes or an authenticated enriched feed drive simulated fills.
6. Stops, targets, risk limits, and forced close are executed by code, not by an LLM.

No OpenAI API key is used by this project. The Custom GPT runs in ChatGPT and calls
the HTTPS Action after approval. ChatGPT/GPT availability is governed by the user's
ChatGPT plan. This software does not provide investment advice or guarantee results.

## Safety boundary

- `PaperBroker` is the only execution adapter.
- `KISReadOnlyClient` allowlists quotation GET paths and OAuth token creation.
- `place_order()` always raises `LiveOrderCapabilityDisabled`.
- There are no KIS order, account, balance, hash-key, or websocket order-notice calls.
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
optionally a Telegram bot and Cloudflare Tunnel.

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

## Market data modes

The normative feed and indicator definitions are documented in
[`docs/INDICATOR_CONTRACT.ko.md`](docs/INDICATOR_CONTRACT.ko.md).

The default is authenticated push mode. A separate local feed calculator posts
fresh ticks (maximum age three seconds), including any indicators referenced by the
plan:

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

Set `KIS_POLL_ENABLED=true` to use the included quotation-only KIS REST fallback.
It polls only symbols in an armed plan. It supplies price/spread data, not VWAP,
RSI, opening range, or relative volume; plans that reference missing indicators do
not enter. US entry also requires the feed to identify a consolidated quote;
the REST fallback reports `quote_scope=unknown` and therefore cannot arm a US fill
by itself. KIS's official documentation recommends WebSocket for real-time quotes,
so use a tested enriched WebSocket feed bridge before latency-sensitive simulation.

## Daily approval workflow

The scheduler sends KR OTP at 08:15 KST and US OTP at 08:45 ET on exchange sessions.
The user asks the Custom GPT for the daily plan, reviews exact JSON-equivalent terms,
then says they approve and types the relevant OTP. The GPT Action registers the plan.
Discussion or a generic “looks good” must not trigger the Action.

For manual testing, issue an OTP with the admin endpoint:

```bash
curl -X POST http://127.0.0.1:8787/v1/admin/nonces \
  -H "Authorization: Bearer $ADMIN_BEARER" \
  -H "Content-Type: application/json" \
  -d '{"market":"KR","trade_date":"2026-07-15"}'
```

Then replace the sample's date, expiry, and OTP and post it using
`GPT_ACTION_BEARER`. `samples/kr_plan.json` is schema-only and is not a recommendation.

## Custom GPT and Cloudflare

1. Create a Custom GPT (a GPT in ChatGPT) and paste
   `docs/CUSTOM_GPT_INSTRUCTIONS.md` into its instructions.
2. Create a Cloudflare Tunnel to localhost. The example ingress exposes only
   `/v1/gpt-actions/*`; admin, dashboard, portfolio, and market-data paths remain local.
3. Replace `https://trade.example.com` in `gpt_action_openapi.yaml`, import it as an
   Action, and configure bearer/API-key authentication with `GPT_ACTION_BEARER`.
4. Test with a new OTP and confirm a `201` receipt and matching content hash.

The launchd template in `deploy/` keeps the service running. Replace all absolute
path placeholders before installing it in `~/Library/LaunchAgents`. Run cloudflared
as a separate launch agent using the restricted ingress configuration.

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
fills, restart persistence, stale ticks, and the hard KIS order prohibition.
