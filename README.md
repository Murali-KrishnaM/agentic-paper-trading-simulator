# Backend — Data + Wallet Foundation

This is Track A+B from the roadmap: real price/news ingestion, wallets, and trade
execution, all backed by Redis and exposed over a small HTTP API.

## Setup

```bash
# 1. Install Redis (if not already) and start it
#    macOS: brew install redis && redis-server
#    Ubuntu: sudo apt install redis-server && redis-server
#    Windows: use WSL or Docker (docker run -p 6379:6379 redis)

sudo service redis-server start

# 2. Install Python deps
pip install -r requirements.txt

# 3. (Optional but recommended) get a free Finnhub API key:
#    https://finnhub.io/register
#    then: export FINNHUB_API_KEY=your_key_here

# 4. Run all three processes (separate terminals):
python price_feed.py
python news_feed.py
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Once running, check it's alive: `http://localhost:8000/prices` should return live prices
within ~15 seconds.

## For your teammates

Send them `REDIS_CONTRACT.md` — it's the exact shape of every Redis key, pub/sub
channel, and API endpoint they need to read/write. As long as they match that
contract, their piece (dashboard or agents) will plug straight into this backend
with zero changes on either side.

## Moving to 3 laptops later

Only `REDIS_HOST` in `config.py` needs to change on each machine to point at
whichever laptop is running Redis. No other code changes required.
