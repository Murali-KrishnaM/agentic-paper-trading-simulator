# Agent pipeline (Track D)

Three independent processes. None of them implement wallets or trade logic —
all money movement goes through the teammate's backend, always with
`wallet: "agent"`.

```
quant_agent.py      -- reads /prices/{t}/history      -> serves :8101/signals
sentiment_agent.py  -- reads /news/{t} + local LLM    -> serves :8102/signals
orchestrator.py     -- reads both agents + /wallet/agent, POSTs /trade
```

The two analyst agents publish their signals over a tiny read-only HTTP
endpoint instead of writing to Redis, so nothing about the backend contract
changes and the orchestrator can sit on a different machine.

## Install & run (single laptop)

```bash
pip install -r requirements.txt
ollama pull llama3.2          # for the sentiment agent

python quant_agent.py         # terminal 1
python sentiment_agent.py     # terminal 2
python orchestrator.py        # terminal 3
```

Give the analyst agents ~20s of head start so they have signals before the
orchestrator's first cycle.

## Three laptops on the LAN

Nothing to edit in code. Copy the folder to each machine and set env vars:

```bash
# laptop A (quant)
BACKEND_URL=http://192.168.1.10:8000 python quant_agent.py

# laptop B (sentiment)
BACKEND_URL=http://192.168.1.10:8000 python sentiment_agent.py

# laptop C (orchestrator + dashboard)
BACKEND_URL=http://192.168.1.10:8000 \
QUANT_AGENT_URL=http://192.168.1.11:8101 \
SENTIMENT_AGENT_URL=http://192.168.1.12:8102 \
python orchestrator.py
```

Every URL, threshold, and poll interval lives in `config.py` and can be
overridden by an environment variable.

## Decision matrix (orchestrator)

| Quant | Sentiment | Action |
|---|---|---|
| ANOMALY | NEGATIVE | `SELL` if holding, else `BLOCK` (no entry) |
| ANOMALY | NEUTRAL / POSITIVE | `ESCALATE` — unexplained move, don't chase |
| normal | NEGATIVE, severity ≥ 60 | `SELL` if holding, else `ESCALATE` |
| normal | NEGATIVE, mild | `HOLD` |
| normal | POSITIVE, severity ≥ 65 | `BUY` (news-driven entry, unless quant says SELL) |
| normal | NEUTRAL / POSITIVE | `BUY` if quant BUY; `SELL` if quant SELL and holding; else `HOLD` |

Every executed trade carries a `reason` string containing both agents' inputs,
so the dashboard can show the full "why".

## Demo notes

- The sentiment agent has no concept of real vs invented tickers. Inject a
  headline for NMBS/ORVX/ZYLA through the backend and it gets classified on
  the next poll (default 20s) like any other news.
- The "normal + strong positive news" row is the one that makes the injected
  headline trade even when the fake ticker's price is flat. Threshold is
  `NEWS_ONLY_BUY_SEVERITY` in `config.py` — lower it if the demo needs to be
  more trigger-happy.
- To make the loop visibly faster for a live audience, drop the three
  `*_POLL_INTERVAL` values.

## Risk controls

`config.py`: max 10% of cash per buy, a $100 cash buffer, 25 shares max per
trade, a 60s per-ticker cooldown after any fill, and stale signals (>180s old)
are ignored rather than acted on.

## Things to know

- **VWAP is really TWAP.** `/prices/{t}/history` has no volume field, so the
  baseline is an unweighted rolling mean. If volume is added to the payload,
  weight the mean in `vwap_deviation()` and nothing else changes.
- **LLM fallback.** If Ollama is unreachable, the sentiment agent logs it once
  and falls back to a keyword classifier so the demo doesn't stall. Check
  `GET :8102/health` → `llm_available` to confirm which path is live.
- **Backend shape tolerance.** The wallet/holdings and news/history parsers
  accept a few plausible JSON shapes, since I was coding against the contract
  rather than the running service. Worth a 5-minute check against the real
  backend before the demo.
- **The quant agent is mean-reversion-leaning** (stretch from the mean pulls
  toward a fade, momentum partly offsets it). That's a tunable stance, not a
  law — weights are all in `build_signal()`.
