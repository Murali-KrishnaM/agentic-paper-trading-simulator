"""
Writes price:<TICKER> and price_history:<TICKER> to Redis.
Real tickers -> pulled from Finnhub's /quote endpoint (same API key as news_feed.py).
Fake tickers -> gentle random walk, so the invented tickers still move on screen.

Why Finnhub instead of yfinance: yfinance scrapes an undocumented Yahoo endpoint
that requires a cookie+crumb handshake through fc.yahoo.com. On many networks
(campus/corporate DNS, some ISPs, VPNs) that host is blocked or fails to resolve,
causing the exact "Cookie/crumb fetch failed (DNSError)" error. Finnhub is a real,
documented API and doesn't have this problem.

Run this as its own long-lived process:
    python price_feed.py
"""
import os
import json
import time
import random
import requests
import redis
from dotenv import load_dotenv
from config import (
    REDIS_HOST, REDIS_PORT, REDIS_DB, REAL_TICKERS, FAKE_TICKERS,
    FAKE_TICKER_META, PRICE_POLL_SECONDS, FAKE_TICK_SECONDS,
)

load_dotenv()

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

_fake_prices = {t: FAKE_TICKER_META[t]["base_price"] for t in FAKE_TICKERS}


def _push_price(ticker: str, price: float):
    entry = json.dumps({"price": round(price, 2), "ts": time.time()})
    r.set(f"price:{ticker}", entry)
    r.rpush(f"price_history:{ticker}", entry)
    r.ltrim(f"price_history:{ticker}", -400, -1)


def _tick_fake_tickers():
    for t in FAKE_TICKERS:
        vol = FAKE_TICKER_META[t]["vol"]
        _fake_prices[t] = max(0.5, _fake_prices[t] + random.gauss(0, vol * 0.15))
        _push_price(t, _fake_prices[t])


def _refresh_real_tickers():
    if not REAL_TICKERS:
        return
    if not FINNHUB_API_KEY:
        print("[price_feed] WARNING: FINNHUB_API_KEY not set, skipping real tickers")
        return
    for t in REAL_TICKERS:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": t, "token": FINNHUB_API_KEY},
                timeout=10,
            )
            data = resp.json()
            price = data.get("c")  # current price
            if price and price > 0:
                _push_price(t, price)
            else:
                print(f"[price_feed] no price in response for {t}: {data}")
        except Exception as e:
            print(f"[price_feed] could not fetch price for {t}: {e}")


def main():
    print(f"[price_feed] tracking real: {REAL_TICKERS} | fake: {FAKE_TICKERS}")
    last_real_poll = 0
    while True:
        now = time.time()
        if now - last_real_poll >= PRICE_POLL_SECONDS:
            _refresh_real_tickers()
            last_real_poll = now
        _tick_fake_tickers()
        time.sleep(FAKE_TICK_SECONDS)


if __name__ == "__main__":
    main()