# Redis Data Contract

Everyone connects to the **same Redis instance** (one laptop hosts it; others point
`REDIS_HOST` at that machine's LAN IP). These are the only keys/channels anyone
should read or write — stick to this so all 3 pieces integrate cleanly.

## Keys (state)

| Key | Type | Shape | Written by | Read by |
|---|---|---|---|---|
| `price:<TICKER>` | string (JSON) | `{"price": 184.2, "ts": 1732000000}` | price_feed.py (backend) | dashboard, quant agent |
| `price_history:<TICKER>` | list (JSON strings) | same shape as above, capped at last 400 | price_feed.py (backend) | dashboard, quant agent |
| `news:<TICKER>` | list (JSON strings) | `{"headline": "...", "ts": 1732000000, "source": "finnhub" or "manual"}`, capped at last 20 | news_feed.py (backend) | sentiment agent |
| `wallet:user` | string (JSON) | `{"cash": 100000.0, "holdings": {"AAPL": {"qty": 5, "avg_cost": 180.1}}}` | backend (via /trade) | dashboard |
| `wallet:agent` | string (JSON) | same shape as `wallet:user` | backend (via /trade), orchestrator | dashboard |
| `trade_log` | list (JSON strings) | `{"wallet": "user"/"agent", "ticker": "AAPL", "side": "buy"/"sell", "qty": 5, "price": 180.1, "ts": ..., "reason": "manual" or agent's reasoning text}` | backend (via /trade) | dashboard |
| `settings:risk_tolerance` | string | `"conservative"` or `"aggressive"` | dashboard (user toggle) | orchestrator (decides where sell proceeds go) |

## Pub/Sub channels (signals — agents only)

| Channel | Published by | Subscribed by | Message shape |
|---|---|---|---|
| `quant_signals` | Quant agent | Orchestrator | `{"ticker": "AAPL", "signal": "BUY"/"SELL"/"HOLD", "score": 0.0-1.0, "ts": ...}` |
| `semantic_signals` | Sentiment agent | Orchestrator | `{"ticker": "AAPL", "sentiment": "POSITIVE"/"NEGATIVE"/"NEUTRAL", "severity": 0-100, "ts": ...}` |
| `orchestrator_decisions` | Orchestrator | Dashboard | `{"ticker": "AAPL", "verdict": "BUY"/"SELL"/"HOLD"/"ESCALATE", "qty": 3, "reasoning": "...", "ts": ...}` |

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
- `GET /settings/risk_tolerance` / `POST /settings/risk_tolerance` body `{"value": "conservative"/"aggressive"}`

**Sell-proceeds rule (already implemented in backend `wallet.py`):** when a sell
happens on the **agent wallet**, the backend checks `settings:risk_tolerance` —
`conservative` sweeps the proceeds to `wallet:user`, `aggressive` keeps them in
`wallet:agent`. Sells on the **user wallet** always just return cash to the user
wallet (the user can't accidentally fund the agent this way).
