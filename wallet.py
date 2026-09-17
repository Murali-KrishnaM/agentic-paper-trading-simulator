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


# Cash-only movement between the two existing wallets. Deliberately separate
# from trade_log / execute_trade: a transfer is not a trade, never touches
# holdings, and must never be confused with BUY/SELL semantics.
_VALID_WALLETS = ("user", "agent")


def _log_transfer(from_wallet: str, to_wallet: str, amount: float):
    entry = {
        "from": from_wallet, "to": to_wallet,
        "amount": amount, "ts": time.time(),
    }
    r.lpush("wallet_transfer_log", json.dumps(entry))
    r.ltrim("wallet_transfer_log", 0, 199)  # keep last 200


def execute_transfer(from_wallet: str, to_wallet: str, amount: float) -> dict:
    """
    Moves CASH ONLY from one existing wallet to the other. Holdings are never
    touched. Atomic: uses Redis WATCH/MULTI/EXEC on both wallet keys so a
    failed operation cannot leave one wallet debited without the other
    credited (either both updates land, or neither does).

    Returns {"ok": True, "from_wallet": {...}, "to_wallet": {...}} or
    {"ok": False, "error": "..."}.
    """
    if from_wallet not in _VALID_WALLETS or to_wallet not in _VALID_WALLETS:
        return {"ok": False, "error": "wallet must be 'user' or 'agent'"}
    if from_wallet == to_wallet:
        return {"ok": False, "error": "source and destination wallet must differ"}
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "amount must be a number"}
    if not (amount > 0):
        return {"ok": False, "error": "amount must be positive"}

    from_key = _wallet_key(from_wallet)
    to_key = _wallet_key(to_wallet)

    with r.pipeline() as pipe:
        try:
            pipe.watch(from_key, to_key)

            from_raw = pipe.get(from_key)
            to_raw = pipe.get(to_key)
            if not from_raw or not to_raw:
                pipe.unwatch()
                return {"ok": False, "error": "unknown wallet"}

            from_w = json.loads(from_raw)
            to_w = json.loads(to_raw)

            if amount > from_w["cash"]:
                pipe.unwatch()
                return {"ok": False, "error": "insufficient cash"}

            from_w["cash"] -= amount
            to_w["cash"] += amount

            pipe.multi()
            pipe.set(from_key, json.dumps(from_w))
            pipe.set(to_key, json.dumps(to_w))
            pipe.execute()  # raises WatchError if either key changed concurrently
        except redis.WatchError:
            return {"ok": False, "error": "wallet state changed concurrently, please retry"}

    _log_transfer(from_wallet, to_wallet, amount)
    return {"ok": True, "from_wallet": get_wallet(from_wallet), "to_wallet": get_wallet(to_wallet)}


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