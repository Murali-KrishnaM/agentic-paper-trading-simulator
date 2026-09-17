"""
FastAPI backend — the single API the dashboard and the agents both talk to.
Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import json
import time
from dotenv import load_dotenv

# Load environment variables from .env file before importing other app modules
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis

from config import REDIS_HOST, REDIS_PORT, REDIS_DB, REAL_TICKERS, FAKE_TICKERS
import wallet
from news_feed import push_headline

app = FastAPI(title="Paper Trading Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)


@app.on_event("startup")
def startup():
    wallet.init_wallets()


class TradeRequest(BaseModel):
    wallet: str      # "user" or "agent"
    ticker: str
    side: str        # "buy" or "sell"
    qty: int
    reason: str | None = None


class NewsInject(BaseModel):
    headline: str


class TransferRequest(BaseModel):
    from_wallet: str   # "user" or "agent"
    to_wallet: str     # "user" or "agent"
    amount: float


class RiskSetting(BaseModel):
    value: str       # "conservative" or "aggressive"


@app.get("/tickers")
def get_tickers():
    return {"real": REAL_TICKERS, "fake": FAKE_TICKERS}


@app.get("/prices")
def get_prices():
    out = {}
    for t in REAL_TICKERS + FAKE_TICKERS:
        raw = r.get(f"price:{t}")
        if raw:
            out[t] = json.loads(raw)
    return out


@app.get("/prices/{ticker}/history")
def get_price_history(ticker: str):
    raw_list = r.lrange(f"price_history:{ticker}", 0, -1)
    return [json.loads(x) for x in raw_list]


@app.get("/news/{ticker}")
def get_news(ticker: str):
    raw_list = r.lrange(f"news:{ticker}", 0, -1)
    return [json.loads(x) for x in raw_list]


@app.post("/news/{ticker}/inject")
def inject_news(ticker: str, body: NewsInject):
    push_headline(ticker, body.headline, source="manual")
    return {"ok": True}


@app.get("/wallet/{wallet_id}")
def get_wallet(wallet_id: str):
    try:
        return wallet.get_wallet(wallet_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/trade")
def post_trade(req: TradeRequest):
    result = wallet.execute_trade(req.wallet, req.ticker, req.side, req.qty, req.reason)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/transfer")
def post_transfer(req: TransferRequest):
    """Cash-only move between wallet:user and wallet:agent. Not a trade."""
    result = wallet.execute_transfer(req.from_wallet, req.to_wallet, req.amount)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/transfers")
def get_transfers(limit: int = 50):
    raw_list = r.lrange("wallet_transfer_log", 0, limit - 1)
    return [json.loads(x) for x in raw_list]


@app.get("/trades")
def get_trades(limit: int = 50):
    raw_list = r.lrange("trade_log", 0, limit - 1)
    return [json.loads(x) for x in raw_list]


@app.get("/settings/risk_tolerance")
def get_risk_tolerance():
    return {"value": r.get("settings:risk_tolerance") or "conservative"}


@app.post("/settings/risk_tolerance")
def set_risk_tolerance(body: RiskSetting):
    if body.value not in ("conservative", "aggressive"):
        raise HTTPException(status_code=400, detail="value must be 'conservative' or 'aggressive'")
    r.set("settings:risk_tolerance", body.value)
    return {"ok": True, "value": body.value}