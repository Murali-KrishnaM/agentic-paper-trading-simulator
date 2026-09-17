#!/usr/bin/env python3
"""
QUANT AGENT  (node 1)

Polls  GET {BACKEND_URL}/prices/{ticker}/history  for every tracked ticker,
computes technical signals (VWAP deviation, z-score momentum, volatility
spike) and emits a BUY / SELL / HOLD signal per ticker.

It does NOT trade. It publishes its signals on a tiny HTTP endpoint that the
orchestrator polls:

    GET http://<this-laptop>:8101/signals          -> all tickers
    GET http://<this-laptop>:8101/signals/AAPL     -> one ticker
    GET http://<this-laptop>:8101/health

Run:
    pip install -r requirements.txt
    python quant_agent.py
"""

import math
import statistics
import threading
import time
from typing import Any, Dict, List, Optional

import requests
import uvicorn
from fastapi import FastAPI

import config

# --------------------------------------------------------------------------
# Shared signal store (written by the poll loop, read by the HTTP handlers)
# --------------------------------------------------------------------------
_signals: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [QUANT] {msg}", flush=True)


# --------------------------------------------------------------------------
# Backend I/O
# --------------------------------------------------------------------------
def fetch_history(ticker: str) -> List[Dict[str, Any]]:
    """GET /prices/{ticker}/history -> [{"price": float, "ts": ...}, ...]"""
    url = f"{config.BACKEND_URL}/prices/{ticker}/history"
    try:
        r = requests.get(url, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log(f"{ticker}: history fetch failed ({exc})")
        return []

    # Be tolerant about the exact envelope the backend uses.
    if isinstance(data, dict):
        data = data.get("history") or data.get("prices") or data.get("data") or []
    if not isinstance(data, list):
        return []

    points = []
    for p in data:
        if not isinstance(p, dict):
            continue
        try:
            points.append({"price": float(p["price"]), "ts": p.get("ts")})
        except (KeyError, TypeError, ValueError):
            continue
    return points


# --------------------------------------------------------------------------
# Technical indicators
# --------------------------------------------------------------------------
def vwap_deviation(prices: List[float], window: int) -> Optional[float]:
    """
    Percentage deviation of the latest price from the rolling average price.

    The backend's history has no volume field, so this is a time-weighted
    average price (TWAP) used as the VWAP stand-in. If volume is added to the
    history payload later, weight the mean here and nothing else changes.
    """
    w = prices[-window:]
    if len(w) < 2:
        return None
    baseline = sum(w) / len(w)
    if baseline == 0:
        return None
    return (prices[-1] - baseline) / baseline * 100.0


def zscore(prices: List[float], window: int) -> Optional[float]:
    """How many standard deviations the latest price sits from the window mean."""
    w = prices[-window:]
    if len(w) < 3:
        return None
    mean = statistics.fmean(w)
    try:
        sd = statistics.pstdev(w)
    except statistics.StatisticsError:
        return None
    if sd < 1e-9:
        return 0.0
    return (prices[-1] - mean) / sd


def returns(prices: List[float]) -> List[float]:
    out = []
    for prev, cur in zip(prices, prices[1:]):
        if prev:
            out.append((cur - prev) / prev)
    return out


def volatility_ratio(prices: List[float]) -> Optional[float]:
    """Recent realised vol vs the longer-run baseline. >1 means things heated up."""
    rets = returns(prices)
    if len(rets) < 10:
        return None
    recent = rets[-5:]
    baseline = rets[-30:] if len(rets) >= 30 else rets
    try:
        r_sd = statistics.pstdev(recent)
        b_sd = statistics.pstdev(baseline)
    except statistics.StatisticsError:
        return None
    if b_sd < 1e-12:
        return None
    return r_sd / b_sd


def momentum_pct(prices: List[float]) -> Optional[float]:
    """Short MA vs long MA, as a percentage."""
    if len(prices) < 10:
        return None
    short = statistics.fmean(prices[-5:])
    long = statistics.fmean(prices[-20:]) if len(prices) >= 20 else statistics.fmean(prices)
    if long == 0:
        return None
    return (short - long) / long * 100.0


# --------------------------------------------------------------------------
# Signal construction
# --------------------------------------------------------------------------
def build_signal(ticker: str, points: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = time.time()
    prices = [p["price"] for p in points]

    base = {
        "ticker": ticker,
        "signal": "HOLD",
        "anomaly": False,
        "confidence": 0.0,
        "ts": now,
        "metrics": {},
        "reason": "",
    }

    if len(prices) < config.MIN_HISTORY_POINTS:
        base["reason"] = f"insufficient history ({len(prices)} pts)"
        return base

    z = zscore(prices, config.ZSCORE_WINDOW)
    vdev = vwap_deviation(prices, config.VWAP_WINDOW)
    vol = volatility_ratio(prices)
    mom = momentum_pct(prices)

    base["metrics"] = {
        "last_price": round(prices[-1], 4),
        "zscore": None if z is None else round(z, 3),
        "vwap_dev_pct": None if vdev is None else round(vdev, 3),
        "vol_ratio": None if vol is None else round(vol, 3),
        "momentum_pct": None if mom is None else round(mom, 3),
        "points": len(prices),
    }

    # --- anomaly flag: the thing the orchestrator cross-validates against ---
    anomaly = False
    anomaly_bits = []
    if z is not None and abs(z) >= config.ANOMALY_Z:
        anomaly = True
        anomaly_bits.append(f"z={z:+.2f}")
    if vol is not None and vol >= config.VOL_SPIKE_RATIO:
        anomaly = True
        anomaly_bits.append(f"vol x{vol:.1f}")
    if vdev is not None and abs(vdev) >= config.VWAP_DEV_PCT * 3:
        anomaly = True
        anomaly_bits.append(f"VWAP {vdev:+.2f}%")
    base["anomaly"] = anomaly

    # --- directional score: mean-reversion on stretch, trend on momentum ---
    score = 0.0
    bits = []

    if z is not None and abs(z) >= config.SIGNAL_Z:
        # Stretched far from the mean -> lean toward reversion.
        score -= max(-2.0, min(2.0, z)) * 0.5
        bits.append(f"z-score {z:+.2f}")

    if vdev is not None and abs(vdev) >= config.VWAP_DEV_PCT:
        score -= max(-2.0, min(2.0, vdev / config.VWAP_DEV_PCT)) * 0.35
        bits.append(f"VWAP dev {vdev:+.2f}%")

    if mom is not None and abs(mom) >= 0.3:
        # Trend gets a smaller weight than reversion, and pulls the other way.
        score += max(-2.0, min(2.0, mom / 1.0)) * 0.30
        bits.append(f"momentum {mom:+.2f}%")

    if vol is not None and vol >= config.VOL_SPIKE_RATIO:
        # High volatility shrinks conviction rather than flipping direction.
        score *= 0.6
        bits.append(f"vol spike x{vol:.1f}")

    if score >= 0.45:
        signal = "BUY"
    elif score <= -0.45:
        signal = "SELL"
    else:
        signal = "HOLD"

    base["signal"] = signal
    base["confidence"] = round(min(1.0, abs(score) / 1.5), 3)
    reason = ", ".join(bits) if bits else "no meaningful deviation"
    if anomaly_bits:
        reason += " | ANOMALY: " + ", ".join(anomaly_bits)
    base["reason"] = reason
    return base


# --------------------------------------------------------------------------
# Poll loop
# --------------------------------------------------------------------------
def poll_loop() -> None:
    log(f"backend = {config.BACKEND_URL}")
    log(f"tracking {len(config.TICKERS)} tickers: {', '.join(config.TICKERS)}")
    while True:
        cycle_start = time.time()
        for ticker in config.TICKERS:
            points = fetch_history(ticker)
            sig = build_signal(ticker, points)
            with _lock:
                _signals[ticker] = sig

            m = sig["metrics"]
            flag = "ANOMALY" if sig["anomaly"] else "normal"
            log(
                f"{ticker:<6} {sig['signal']:<4} conf={sig['confidence']:.2f} [{flag}] "
                f"px={m.get('last_price')} z={m.get('zscore')} "
                f"vwap_dev={m.get('vwap_dev_pct')}% vol={m.get('vol_ratio')} "
                f":: {sig['reason']}"
            )
        elapsed = time.time() - cycle_start
        log(f"cycle done in {elapsed:.1f}s, sleeping {config.QUANT_POLL_INTERVAL}s")
        time.sleep(max(1.0, config.QUANT_POLL_INTERVAL - elapsed))


# --------------------------------------------------------------------------
# Signal server
# --------------------------------------------------------------------------
app = FastAPI(title="Quant Agent")


@app.get("/health")
def health():
    with _lock:
        n = len(_signals)
    return {"agent": "quant", "ok": True, "tickers": n, "ts": time.time()}


@app.get("/signals")
def signals():
    with _lock:
        return {"agent": "quant", "ts": time.time(), "signals": dict(_signals)}


@app.get("/signals/{ticker}")
def signal_for(ticker: str):
    with _lock:
        return _signals.get(ticker.upper(), {"ticker": ticker.upper(), "signal": "HOLD",
                                             "anomaly": False, "confidence": 0.0,
                                             "reason": "no data yet", "ts": time.time()})


def main() -> None:
    threading.Thread(target=poll_loop, daemon=True).start()
    log(f"serving signals on {config.QUANT_BIND_HOST}:{config.QUANT_BIND_PORT}")
    uvicorn.run(app, host=config.QUANT_BIND_HOST, port=config.QUANT_BIND_PORT,
                log_level="warning")


if __name__ == "__main__":
    main()
