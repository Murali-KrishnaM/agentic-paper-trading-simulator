#!/usr/bin/env python3
"""
SENTIMENT AGENT  (node 2)

Polls  GET {BACKEND_URL}/news/{ticker}  for every tracked ticker, sends each
new headline to a local LLM (Ollama) and gets back POSITIVE / NEGATIVE /
NEUTRAL plus a severity score 0-100. Headlines are aggregated into one
sentiment verdict per ticker.

Invented tickers (NMBS, ORVX, ZYLA) are handled identically — the agent has no
idea which tickers are real, it just polls /news/{ticker}. A manually injected
headline shows up here on the next poll like any other news.

It does NOT trade. It publishes signals on:

    GET http://<this-laptop>:8102/signals
    GET http://<this-laptop>:8102/signals/NMBS
    GET http://<this-laptop>:8102/health

Run:
    ollama pull llama3.2
    python sentiment_agent.py
"""

import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import uvicorn
from fastapi import FastAPI

import config

_signals: Dict[str, Dict[str, Any]] = {}
_cache: Dict[str, Tuple[str, int]] = {}   # headline -> (label, severity)
_lock = threading.Lock()

_llm_available = True   # flips to False after repeated Ollama failures


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [SENTIMENT] {msg}", flush=True)


# --------------------------------------------------------------------------
# Backend I/O
# --------------------------------------------------------------------------
def fetch_news(ticker: str) -> List[Dict[str, Any]]:
    url = f"{config.BACKEND_URL}/news/{ticker}"
    try:
        r = requests.get(url, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log(f"{ticker}: news fetch failed ({exc})")
        return []

    if isinstance(data, dict):
        data = data.get("news") or data.get("headlines") or data.get("data") or []
    if not isinstance(data, list):
        return []

    items = []
    for n in data:
        if isinstance(n, str):
            items.append({"headline": n, "ts": None, "source": "unknown"})
        elif isinstance(n, dict) and n.get("headline"):
            items.append({
                "headline": str(n["headline"]),
                "ts": n.get("ts"),
                "source": n.get("source", "unknown"),
            })
    return items


def _ts_seconds(ts: Any) -> Optional[float]:
    """Backends vary: epoch seconds, epoch ms, or ISO string."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return ts / 1000.0 if ts > 1e11 else float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def recent_headlines(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    now = time.time()
    fresh = []
    for it in items:
        t = _ts_seconds(it.get("ts"))
        # Keep anything without a usable timestamp — better to score it than drop it.
        if t is None or (now - t) <= config.NEWS_LOOKBACK_SECONDS:
            fresh.append(it)
    fresh.sort(key=lambda i: _ts_seconds(i.get("ts")) or 0, reverse=True)
    return fresh[: config.MAX_HEADLINES_PER_TICKER]


# --------------------------------------------------------------------------
# LLM classification
# --------------------------------------------------------------------------
PROMPT = """You are a financial news sentiment classifier.

Classify the sentiment of the headline below for the stock {ticker}, from the
point of view of a trader holding that stock.

Headline: "{headline}"

Reply with ONLY a JSON object, no preamble, no markdown fences:
{{"label": "POSITIVE" | "NEGATIVE" | "NEUTRAL", "severity": <integer 0-100>, "why": "<max 12 words>"}}

severity = how strongly this should move the stock (0 = irrelevant noise,
100 = major, market-moving news). A NEUTRAL headline should have low severity.
"""

POSITIVE_WORDS = {
    "beats", "beat", "surge", "surges", "soars", "record", "wins", "win", "upgrade",
    "upgraded", "profit", "growth", "expands", "approval", "approved", "breakthrough",
    "partnership", "contract", "rally", "jumps", "outperform", "raises", "strong",
}
NEGATIVE_WORDS = {
    "misses", "miss", "plunge", "plunges", "falls", "drop", "drops", "lawsuit", "sues",
    "probe", "investigation", "recall", "downgrade", "downgraded", "loss", "losses",
    "layoffs", "fraud", "resigns", "warns", "warning", "slump", "cuts", "delay",
    "delayed", "halt", "halted", "breach", "weak", "sinks", "crash",
}


def heuristic_classify(headline: str) -> Tuple[str, int, str]:
    words = set(re.findall(r"[a-z]+", headline.lower()))
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    if neg > pos:
        return "NEGATIVE", min(90, 45 + 15 * neg), "keyword fallback (LLM unavailable)"
    if pos > neg:
        return "POSITIVE", min(90, 45 + 15 * pos), "keyword fallback (LLM unavailable)"
    return "NEUTRAL", 10, "keyword fallback (LLM unavailable)"


def llm_classify(ticker: str, headline: str) -> Tuple[str, int, str]:
    global _llm_available
    if not _llm_available:
        return heuristic_classify(headline)

    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": PROMPT.format(ticker=ticker, headline=headline.replace('"', "'")),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0},
    }
    try:
        r = requests.post(f"{config.OLLAMA_URL}/api/generate", json=payload, timeout=60)
        r.raise_for_status()
        raw = r.json().get("response", "")
    except Exception as exc:
        log(f"Ollama call failed ({exc}) — falling back to keyword heuristic")
        _llm_available = False
        return heuristic_classify(headline)

    parsed = _parse_json_blob(raw)
    if not parsed:
        log(f"could not parse LLM output: {raw[:120]!r}")
        return heuristic_classify(headline)

    label = str(parsed.get("label", "NEUTRAL")).upper().strip()
    if label not in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
        label = "NEUTRAL"
    try:
        severity = int(float(parsed.get("severity", 0)))
    except (TypeError, ValueError):
        severity = 0
    severity = max(0, min(100, severity))
    why = str(parsed.get("why", ""))[:80]
    return label, severity, why


def _parse_json_blob(text: str) -> Optional[dict]:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def classify_cached(ticker: str, headline: str) -> Tuple[str, int, str]:
    key = f"{ticker}|{headline}"
    with _lock:
        hit = _cache.get(key)
    if hit:
        return hit[0], hit[1], "(cached)"
    label, severity, why = llm_classify(ticker, headline)
    with _lock:
        _cache[key] = (label, severity)
    return label, severity, why


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def build_signal(ticker: str, scored: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = time.time()
    if not scored:
        return {
            "ticker": ticker, "sentiment": "NEUTRAL", "severity": 0,
            "headline_count": 0, "ts": now, "headlines": [],
            "reason": "no recent news",
        }

    # Severity-weighted vote. Newest headline gets a small extra weight.
    pos = neg = 0.0
    for i, s in enumerate(scored):
        w = s["severity"] * (1.15 if i == 0 else 1.0)
        if s["label"] == "POSITIVE":
            pos += w
        elif s["label"] == "NEGATIVE":
            neg += w

    net = pos - neg
    total = pos + neg
    if total == 0 or abs(net) < 20:
        label = "NEUTRAL"
        severity = int(min(100, total / max(1, len(scored))))
    elif net > 0:
        label = "POSITIVE"
        severity = int(min(100, net / max(1, len(scored)) * 1.2))
    else:
        label = "NEGATIVE"
        severity = int(min(100, -net / max(1, len(scored)) * 1.2))

    top = max(scored, key=lambda s: s["severity"])
    return {
        "ticker": ticker,
        "sentiment": label,
        "severity": severity,
        "headline_count": len(scored),
        "ts": now,
        "headlines": [
            {"headline": s["headline"][:140], "label": s["label"],
             "severity": s["severity"], "source": s.get("source", "unknown")}
            for s in scored[:5]
        ],
        "reason": f"{len(scored)} headlines, strongest: \"{top['headline'][:70]}\" "
                  f"({top['label']} {top['severity']})",
    }


# --------------------------------------------------------------------------
# Poll loop
# --------------------------------------------------------------------------
def poll_loop() -> None:
    log(f"backend = {config.BACKEND_URL}")
    log(f"LLM     = {config.OLLAMA_MODEL} @ {config.OLLAMA_URL}")
    log(f"tracking {len(config.TICKERS)} tickers")
    while True:
        cycle_start = time.time()
        for ticker in config.TICKERS:
            items = recent_headlines(fetch_news(ticker))
            scored = []
            for it in items:
                label, severity, why = classify_cached(ticker, it["headline"])
                scored.append({
                    "headline": it["headline"], "label": label,
                    "severity": severity, "why": why, "source": it.get("source"),
                })
                if why != "(cached)":
                    log(f"  {ticker:<6} \"{it['headline'][:70]}\" -> {label} ({severity}) {why}")

            sig = build_signal(ticker, scored)
            with _lock:
                _signals[ticker] = sig
            log(f"{ticker:<6} {sig['sentiment']:<8} severity={sig['severity']:<3} "
                f"n={sig['headline_count']} :: {sig['reason']}")

        elapsed = time.time() - cycle_start
        log(f"cycle done in {elapsed:.1f}s, sleeping {config.SENTIMENT_POLL_INTERVAL}s")
        time.sleep(max(1.0, config.SENTIMENT_POLL_INTERVAL - elapsed))


# --------------------------------------------------------------------------
# Signal server
# --------------------------------------------------------------------------
app = FastAPI(title="Sentiment Agent")


@app.get("/health")
def health():
    with _lock:
        n = len(_signals)
    return {"agent": "sentiment", "ok": True, "tickers": n,
            "llm": config.OLLAMA_MODEL, "llm_available": _llm_available, "ts": time.time()}


@app.get("/signals")
def signals():
    with _lock:
        return {"agent": "sentiment", "ts": time.time(), "signals": dict(_signals)}


@app.get("/signals/{ticker}")
def signal_for(ticker: str):
    with _lock:
        return _signals.get(ticker.upper(), {"ticker": ticker.upper(),
                                             "sentiment": "NEUTRAL", "severity": 0,
                                             "headline_count": 0,
                                             "reason": "no data yet", "ts": time.time()})


def main() -> None:
    threading.Thread(target=poll_loop, daemon=True).start()
    log(f"serving signals on {config.SENTIMENT_BIND_HOST}:{config.SENTIMENT_BIND_PORT}")
    uvicorn.run(app, host=config.SENTIMENT_BIND_HOST, port=config.SENTIMENT_BIND_PORT,
                log_level="warning")


if __name__ == "__main__":
    main()
