# Redis Data Contract

Everyone connects to the **same Redis instance** (one laptop hosts it; others point
`REDIS_HOST` at that machine's LAN IP). These are the only Redis keys anyone
should read or write — stick to this so all pieces integrate cleanly. Agent-to-
orchestrator signals do not go through Redis at all; see the HTTP section below.

## Keys (state)

| Key | Type | Shape | Written by | Read by |
|---|---|---|---|---|
| `price:<TICKER>` | string (JSON) | `{"price": 184.2, "ts": 1732000000}` | price_feed.py (backend) | dashboard, quant agent |
| `price_history:<TICKER>` | list (JSON strings) | same shape as above, capped at last 400 | price_feed.py (backend) | dashboard, quant agent |
| `news:<TICKER>` | list (JSON strings) | `{"headline": "...", "ts": 1732000000, "source": "finnhub" or "manual"}`, capped at last 20 | news_feed.py (backend) | sentiment agent |
| `wallet:user` | string (JSON) | `{"cash": 100000.0, "holdings": {"AAPL": {"qty": 5, "avg_cost": 180.1}}}` | backend (via /trade) | dashboard |
| `wallet:agent` | string (JSON) | same shape as `wallet:user` | backend (via /trade), orchestrator | dashboard |
| `trade_log` | list (JSON strings) | `{"wallet": "user"/"agent", "ticker": "AAPL", "side": "buy"/"sell", "qty": 5, "price": 180.1, "ts": ..., "reason": "manual" or agent's reasoning text}` | backend (via /trade) | dashboard |
| `wallet_transfer_log` | list (JSON strings), capped at last 200 | `{"from": "user"/"agent", "to": "user"/"agent", "amount": 20000.0, "ts": ...}` | backend (via /transfer) | dashboard |
| `settings:risk_tolerance` | string | `"conservative"` or `"aggressive"` | dashboard (user toggle) | orchestrator (decides where sell proceeds go) |

## Agent signal endpoints (HTTP, not Redis Pub/Sub)

The quant and sentiment agents do **not** publish to Redis. Each runs its own
tiny read-only FastAPI server and the orchestrator polls them directly over
HTTP. This lets each agent sit on a different laptop with only a URL to
configure, and keeps the Redis contract limited to the state table above.

| Service | Default URL | Endpoint | Consumed by | Message shape |
|---|---|---|---|---|
| Quant agent | `http://<quant-laptop>:8101` | `GET /signals` → `{"agent": "quant", "ts": ..., "signals": {"<TICKER>": {...}, ...}}` | Orchestrator | Per-ticker: `{"ticker": "AAPL", "signal": "BUY"/"SELL"/"HOLD", "anomaly": bool, "confidence": 0.0-1.0, "metrics": {...}, "reason": "...", "ts": ...}` |
| | | `GET /signals/{ticker}` → single ticker's signal object | Orchestrator | same shape as one entry above |
| | | `GET /health` → `{"agent": "quant", "ok": true, "tickers": <n>, "ts": ...}` | ops/debugging | — |
| Sentiment agent | `http://<sentiment-laptop>:8102` | `GET /signals` → `{"agent": "sentiment", "ts": ..., "signals": {"<TICKER>": {...}, ...}}` | Orchestrator | Per-ticker: `{"ticker": "AAPL", "sentiment": "POSITIVE"/"NEGATIVE"/"NEUTRAL", "severity": 0-100, "headline_count": <n>, "headlines": [...], "reason": "...", "ts": ...}` |
| | | `GET /signals/{ticker}` → single ticker's signal object | Orchestrator | same shape as one entry above |
| | | `GET /health` → `{"agent": "sentiment", "ok": true, "tickers": <n>, "llm": "...", "llm_available": bool, "ts": ...}` | ops/debugging | — |

The orchestrator combines these two per ticker into a verdict and, if it
decides to act, executes the trade against the backend's `/trade` endpoint
(below) with `wallet: "agent"`. It does not publish its decisions to Redis or
anywhere else — the dashboard learns about executed trades the same way it
learns about any trade, by polling `GET /trades`.

> Earlier drafts of this contract described these three flows as Redis
> Pub/Sub channels (`quant_signals`, `semantic_signals`,
> `orchestrator_decisions`). That was the original plan; the shipped
> implementation uses the HTTP endpoints above instead. Nothing in
> `quant_agent.py`, `sentiment_agent.py`, or `orchestrator.py` publishes or
> subscribes to Redis — this table is what's actually running.

## HTTP API (FastAPI backend, default `http://localhost:8000`)

Both the dashboard and the agents should use this API rather than touching Redis
directly wherever possible — it keeps validation (e.g. "can't sell what you don't
own") in one place.

- `GET /tickers` → `{"real": [...], "fake": [...]}`
- `GET /prices` → `{"AAPL": {"price": 184.2, "ts": ...}, ...}` (all current prices)
- `GET /prices/{ticker}/history` → `[{"price": ..., "ts": ...}, ...]`
- `GET /news/{ticker}` → `[{"headline": ..., "ts": ..., "source": ...}, ...]`
- `POST /news/{ticker}/inject` body `{"headline": "..."}` → manually push a headline (used live for fake tickers during the demo, but works for any ticker)
- `GET /wallet/{wallet_id}` where wallet_id is `user` or `agent` → `{"cash": ..., "holdings": {...}}`
- `POST /trade` body `{"wallet": "user"/"agent", "ticker": "AAPL", "side": "buy"/"sell", "qty": 3, "reason": "optional string"}` → executes trade, returns updated wallet or an error like `{"error": "insufficient cash"}`
- `GET /trades?limit=50` → recent trade log entries
- `POST /transfer` body `{"from_wallet": "user"/"agent", "to_wallet": "user"/"agent", "amount": 20000.0}` → moves **cash only** between `wallet:user` and `wallet:agent` (holdings untouched, not a trade, does not touch `trade_log`). Rejects zero/negative amounts, insufficient funds, identical source/destination, or any wallet id other than `user`/`agent`. Wallet debit/credit is applied atomically (Redis `WATCH`/`MULTI`/`EXEC`) so a failure can't leave one wallet updated without the other. Returns `{"ok": true, "from_wallet": {...}, "to_wallet": {...}}` or an error like `{"error": "insufficient cash"}`.
- `GET /transfers?limit=50` → recent `wallet_transfer_log` entries
- `GET /settings/risk_tolerance` / `POST /settings/risk_tolerance` body `{"value": "conservative"/"aggressive"}`

**Sell-proceeds rule (already implemented in backend `wallet.py`):** when a sell
happens on the **agent wallet**, the backend checks `settings:risk_tolerance` —
`conservative` sweeps the proceeds to `wallet:user`, `aggressive` keeps them in
`wallet:agent`. Sells on the **user wallet** always just return cash to the user
wallet (the user can't accidentally fund the agent this way).