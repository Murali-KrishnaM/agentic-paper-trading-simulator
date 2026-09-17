"""
Writes news:<TICKER> to Redis (a capped list of recent headlines).
Real tickers -> pulled from Finnhub's company-news endpoint (free tier API key required:
    https://finnhub.io/register  -> set FINNHUB_API_KEY below or as an env var).
Fake tickers -> no automatic news; headlines only appear when manually injected
    via the backend's POST /news/{ticker}/inject endpoint (used live in the demo).

Run this as its own long-lived process:
    python news_feed.py
"""
import os
import json
import time
import requests
import redis
from config import REDIS_HOST, REDIS_PORT, REDIS_DB, REAL_TICKERS, NEWS_POLL_SECONDS
from dotenv import load_dotenv

load_dotenv()
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")


def push_headline(ticker: str, headline: str, source: str = "manual"):
    entry = json.dumps({"headline": headline, "ts": time.time(), "source": source})
    r.rpush(f"news:{ticker}", entry)
    r.ltrim(f"news:{ticker}", -20, -1)


def _fetch_real_news(ticker: str):
    if FINNHUB_API_KEY == "PUT_YOUR_KEY_HERE":
        return  # skip silently until a key is configured
    today = time.strftime("%Y-%m-%d")
    week_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    url = (
        f"https://finnhub.io/api/v1/company-news?symbol={ticker}"
        f"&from={week_ago}&to={today}&token={FINNHUB_API_KEY}"
    )
    try:
        resp = requests.get(url, timeout=10)
        articles = resp.json()
        for a in articles[:5]:
            headline = a.get("headline")
            if headline:
                push_headline(ticker, headline, source="finnhub")
    except Exception as e:
        print(f"[news_feed] failed to fetch news for {ticker}: {e}")


def main():
    print(f"[news_feed] tracking real news for: {REAL_TICKERS}")
    while True:
        for t in REAL_TICKERS:
            _fetch_real_news(t)
        time.sleep(NEWS_POLL_SECONDS)


if __name__ == "__main__":
    main()
