import json
import time
import redis
from config import REDIS_HOST, REDIS_PORT, REDIS_DB, STARTING_CASH_USER, STARTING_CASH_AGENT

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)


def _wallet_key(wallet_id: str) -> str:
    return f"wallet:{wallet_id}"


def init_wallets():
    """Create user/agent wallets if they don't exist yet. Safe to call repeatedly."""
    if not r.get(_wallet_key("user")):
        r.set(_wallet_key("user"), json.dumps({"cash": STARTING_CASH_USER, "holdings": {}}))
    if not r.get(_wallet_key("agent")):
        r.set(_wallet_key("agent"), json.dumps({"cash": STARTING_CASH_AGENT, "holdings": {}}))
    if not r.get("settings:risk_tolerance"):
        r.set("settings:risk_tolerance", "conservative")


def get_wallet(wallet_id: str) -> dict:
    raw = r.get(_wallet_key(wallet_id))
    if not raw:
        raise ValueError(f"unknown wallet '{wallet_id}'")
    return json.loads(raw)


def _save_wallet(wallet_id: str, wallet: dict):
    r.set(_wallet_key(wallet_id), json.dumps(wallet))


def get_price(ticker: str) -> float:
    raw = r.get(f"price:{ticker}")
    if not raw:
        raise ValueError(f"no price available for '{ticker}' yet")
    return json.loads(raw)["price"]


def _log_trade(wallet_id, ticker, side, qty, price, reason):
    entry = {
        "wallet": wallet_id, "ticker": ticker, "side": side,
        "qty": qty, "price": price, "ts": time.time(), "reason": reason or "manual",
    }
    r.lpush("trade_log", json.dumps(entry))
    r.ltrim("trade_log", 0, 199)  # keep last 200


def execute_trade(wallet_id: str, ticker: str, side: str, qty: int, reason: str = None) -> dict:
    """
    Executes a buy or sell against the given wallet.
    Returns {"ok": True, "wallet": {...}} or {"ok": False, "error": "..."}
    """
    if qty <= 0:
        return {"ok": False, "error": "quantity must be positive"}

    price = get_price(ticker)
    wallet = get_wallet(wallet_id)
    cost = price * qty

    if side == "buy":
        if cost > wallet["cash"]:
            return {"ok": False, "error": "insufficient cash"}
        wallet["cash"] -= cost
        pos = wallet["holdings"].get(ticker, {"qty": 0, "avg_cost": 0.0})
        new_qty = pos["qty"] + qty
        pos["avg_cost"] = (pos["avg_cost"] * pos["qty"] + cost) / new_qty
        pos["qty"] = new_qty
        wallet["holdings"][ticker] = pos
        _save_wallet(wallet_id, wallet)
        _log_trade(wallet_id, ticker, "buy", qty, price, reason)
        return {"ok": True, "wallet": wallet}

    elif side == "sell":
        pos = wallet["holdings"].get(ticker)
        if not pos or pos["qty"] < qty:
            return {"ok": False, "error": "not enough shares to sell"}
        pos["qty"] -= qty
        proceeds = cost

        # Sell-proceeds routing rule: only applies to the AGENT wallet.
        if wallet_id == "agent":
            risk = r.get("settings:risk_tolerance") or "conservative"
            if risk == "conservative":
                user_wallet = get_wallet("user")
                user_wallet["cash"] += proceeds
                _save_wallet("user", user_wallet)
            else:  # aggressive -> proceeds stay in agent wallet
                wallet["cash"] += proceeds
        else:
            wallet["cash"] += proceeds

        wallet["holdings"][ticker] = pos
        _save_wallet(wallet_id, wallet)
        _log_trade(wallet_id, ticker, "sell", qty, price, reason)
        return {"ok": True, "wallet": get_wallet(wallet_id)}

    return {"ok": False, "error": "side must be 'buy' or 'sell'"}
