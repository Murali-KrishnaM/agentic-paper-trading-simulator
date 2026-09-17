"""
Central config: the list of tickers is the single source of truth.
Everyone (dashboard, agents) should read tickers from GET /tickers on the API,
not hardcode their own list, so we never get out of sync.
"""

# Real, liquid tickers with active news flow — good for correlated signals
REAL_TICKERS = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD"]

# Invented tickers — no real market data, price is a gentle random walk,
# and news is manually injected via POST /news/{ticker}/inject during the demo.
# This guarantees a reliable "agent reacts to news" moment on stage.
FAKE_TICKERS = ["NMBS", "ORVX", "ZYLA"]  # Nimbus Robotics, Orivex Materials, Zylabs AI

ALL_TICKERS = REAL_TICKERS + FAKE_TICKERS

FAKE_TICKER_META = {
    "NMBS": {"name": "Nimbus Robotics", "base_price": 64.20, "vol": 0.9},
    "ORVX": {"name": "Orivex Materials", "base_price": 28.75, "vol": 0.7},
    "ZYLA": {"name": "Zylabs AI", "base_price": 112.40, "vol": 1.4},
}

REDIS_HOST = "localhost"  # change this on each laptop to point at the shared Redis host
REDIS_PORT = 6379
REDIS_DB = 0

STARTING_CASH_USER = 100_000.0
STARTING_CASH_AGENT = 50_000.0

PRICE_POLL_SECONDS = 15       # how often price_feed.py refreshes real prices
FAKE_TICK_SECONDS = 2         # how often fake tickers tick
NEWS_POLL_SECONDS = 300       # how often news_feed.py refreshes real news
