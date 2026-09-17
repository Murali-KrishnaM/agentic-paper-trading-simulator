#!/usr/bin/env python3
"""
ORCHESTRATOR AGENT  (node 3)

Polls the quant agent and the sentiment agent, cross-validates their two
signals per ticker into a final verdict, and executes trades against the
teammate's backend using the AGENT wallet only.

Cross-validation matrix (quant anomaly x sentiment):

    anomaly  + NEGATIVE          -> SELL if holding, otherwise BLOCK (no entry)
    anomaly  + NEUTRAL/POSITIVE  -> ESCALATE (unexplained move) / HOLD
    normal   + NEGATIVE          -> HOLD, ESCALATE if the news is severe
    normal   + NEUTRAL/POSITIVE  -> BUY if quant says BUY, else HOLD
                                    (quant SELL while holding -> SELL)

This process owns all trading. The other two agents never touch /trade.

Run:
    python orchestrator.py
"""

import math
import time
from typing import Any, Dict, Optional, Tuple

import requests

import config

_last_trade_at: Dict[str, float] = {}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [ORCH] {msg}", flush=True)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
def fetch_agent_signals(base_url: str, name: str) -> Dict[str, Dict[str, Any]]:
    try:
        r = requests.get(f"{base_url}/signals", timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json().get("signals", {}) or {}
    except Exception as exc:
        log(f"could not reach {name} agent at {base_url} ({exc})")
        return {}


def fetch_prices() -> Dict[str, Dict[str, Any]]:
    try:
        r = requests.get(f"{config.BACKEND_URL}/prices", timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json() or {}
    except Exception as exc:
        log(f"price fetch failed ({exc})")
        return {}


def price_of(prices: Dict[str, Any], ticker: str) -> Optional[float]:
    entry = prices.get(ticker)
    if isinstance(entry, dict):
        try:
            return float(entry.get("price"))
        except (TypeError, ValueError):
            return None
    if isinstance(entry, (int, float)):
        return float(entry)
    return None


def fetch_wallet() -> Dict[str, Any]:
    try:
        r = requests.get(f"{config.BACKEND_URL}/wallet/agent", timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json() or {}
    except Exception as exc:
        log(f"wallet fetch failed ({exc})")
        return {}


def wallet_cash(wallet: Dict[str, Any]) -> float:
    for key in ("cash", "balance", "cash_balance"):
        if key in wallet:
            try:
                return float(wallet[key])
            except (TypeError, ValueError):
                pass
    return 0.0


def holdings_qty(wallet: Dict[str, Any], ticker: str) -> float:
    """Tolerant of several plausible holdings shapes from the backend."""
    holdings = wallet.get("holdings") or wallet.get("positions") or {}
    if isinstance(holdings, dict):
        entry = holdings.get(ticker)
        if isinstance(entry, dict):
            for key in ("qty", "quantity", "shares"):
                if key in entry:
                    try:
                        return float(entry[key])
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0
        if isinstance(entry, (int, float)):
            return float(entry)
    if isinstance(holdings, list):
        for entry in holdings:
            if isinstance(entry, dict) and entry.get("ticker") == ticker:
                for key in ("qty", "quantity", "shares"):
                    if key in entry:
                        try:
                            return float(entry[key])
                        except (TypeError, ValueError):
                            return 0.0
    return 0.0


def is_fresh(sig: Dict[str, Any]) -> bool:
    ts = sig.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    return (time.time() - ts) <= config.SIGNAL_MAX_AGE_SECONDS


# --------------------------------------------------------------------------
# Cross-validation
# --------------------------------------------------------------------------
def decide(ticker: str,
           quant: Dict[str, Any],
           sent: Dict[str, Any],
           held: float) -> Tuple[str, str]:
    """Return (action, reason). Action is BUY / SELL / HOLD / BLOCK / ESCALATE."""
    q_sig = quant.get("signal", "HOLD")
    anomaly = bool(quant.get("anomaly"))
    q_conf = float(quant.get("confidence") or 0.0)
    q_reason = quant.get("reason", "")

    s_label = sent.get("sentiment", "NEUTRAL")
    s_sev = int(sent.get("severity") or 0)
    s_reason = sent.get("reason", "no news")

    ctx = (f"quant={q_sig}(conf {q_conf:.2f}, {'ANOMALY' if anomaly else 'normal'}: {q_reason}); "
           f"sentiment={s_label}({s_sev}): {s_reason}")

    # --- anomaly + negative: the move is real and the news explains it -----
    if anomaly and s_label == "NEGATIVE":
        if held > 0:
            return "SELL", f"Price anomaly confirmed by negative news — exiting position. {ctx}"
        return "BLOCK", f"Price anomaly plus negative news — blocking any new entry. {ctx}"

    # --- anomaly + neutral/positive: unexplained move, don't chase ---------
    if anomaly:
        if s_label == "POSITIVE" and s_sev >= config.SENTIMENT_STRONG and q_sig == "BUY":
            return "ESCALATE", (f"Strong positive news alongside a price anomaly — flagging for "
                                f"review rather than chasing the spike. {ctx}")
        return "ESCALATE", f"Price anomaly not explained by news — holding and flagging. {ctx}"

    # --- normal tape + negative news: news leads price ---------------------
    if s_label == "NEGATIVE":
        if s_sev >= config.SENTIMENT_STRONG:
            if held > 0:
                return "SELL", (f"Severe negative news with no price reaction yet — reducing "
                                f"exposure ahead of the move. {ctx}")
            return "ESCALATE", f"Severe negative news but price is calm — flagging, no entry. {ctx}"
        return "HOLD", f"Mildly negative news, price normal — standing pat. {ctx}"

    # --- normal tape + neutral/positive news ------------------------------
    # Strong positive news on a calm tape counts as a positive signal in its own
    # right — this is what makes an injected headline on a flat ticker trade.
    if (s_label == "POSITIVE" and s_sev >= config.NEWS_ONLY_BUY_SEVERITY
            and q_sig != "SELL"):
        return "BUY", (f"Strong positive news with no adverse technical signal — "
                       f"entering on the news. {ctx}")

    if q_sig == "BUY":
        conviction = "positive news" if s_label == "POSITIVE" else "no contradicting news"
        return "BUY", f"Technical BUY on a normal tape with {conviction}. {ctx}"
    if q_sig == "SELL" and held > 0:
        return "SELL", f"Technical SELL on a normal tape, taking the position off. {ctx}"
    return "HOLD", f"No actionable edge. {ctx}"


# --------------------------------------------------------------------------
# Sizing + execution
# --------------------------------------------------------------------------
def buy_qty(cash: float, price: float, confidence: float) -> int:
    spendable = min(cash * config.MAX_POSITION_FRACTION, max(0.0, cash - config.CASH_BUFFER))
    spendable *= max(0.35, min(1.0, confidence))
    if price <= 0:
        return 0
    qty = int(math.floor(spendable / price))
    return max(0, min(qty, config.MAX_QTY_PER_TRADE))


def sell_qty(held: float, confidence: float) -> int:
    if held <= 0:
        return 0
    portion = math.ceil(held * (0.5 if confidence < 0.6 else 1.0))
    return max(config.MIN_QTY_PER_TRADE, min(int(portion), int(held), config.MAX_QTY_PER_TRADE))


def execute_trade(ticker: str, side: str, qty: int, reason: str) -> bool:
    body = {"wallet": "agent", "ticker": ticker, "side": side, "qty": qty,
            "reason": reason[:400]}
    try:
        r = requests.post(f"{config.BACKEND_URL}/trade", json=body,
                          timeout=config.HTTP_TIMEOUT)
        if r.status_code >= 400:
            log(f"TRADE REJECTED {side.upper()} {qty} {ticker}: HTTP {r.status_code} {r.text[:200]}")
            return False
        payload = r.json() if r.content else {}
        if isinstance(payload, dict) and payload.get("error"):
            log(f"TRADE REJECTED {side.upper()} {qty} {ticker}: {payload['error']}")
            return False
        log(f"TRADE EXECUTED  {side.upper()} {qty} {ticker} | reason: {reason[:120]}")
        _last_trade_at[ticker] = time.time()
        return True
    except Exception as exc:
        log(f"TRADE FAILED {side.upper()} {qty} {ticker}: {exc}")
        return False


def in_cooldown(ticker: str) -> bool:
    last = _last_trade_at.get(ticker)
    return last is not None and (time.time() - last) < config.TRADE_COOLDOWN_SECONDS


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def run_cycle() -> None:
    quant_signals = fetch_agent_signals(config.QUANT_AGENT_URL, "quant")
    sent_signals = fetch_agent_signals(config.SENTIMENT_AGENT_URL, "sentiment")
    prices = fetch_prices()
    wallet = fetch_wallet()
    cash = wallet_cash(wallet)

    log(f"--- cycle | agent cash ${cash:,.2f} | quant sigs {len(quant_signals)} "
        f"| sentiment sigs {len(sent_signals)} ---")

    for ticker in config.TICKERS:
        quant = quant_signals.get(ticker, {})
        sent = sent_signals.get(ticker, {})

        if not quant and not sent:
            log(f"{ticker:<6} SKIP    no signals from either agent yet")
            continue
        if quant and not is_fresh(quant):
            log(f"{ticker:<6} SKIP    quant signal is stale")
            continue
        if sent and not is_fresh(sent):
            sent = {}  # treat stale news as no news rather than skipping the ticker

        held = holdings_qty(wallet, ticker)
        action, reason = decide(ticker, quant, sent, held)
        log(f"{ticker:<6} {action:<8} held={held:g} :: {reason}")

        if action in ("HOLD", "BLOCK"):
            continue
        if action == "ESCALATE":
            log(f"{ticker:<6} -> ESCALATED for human review (no trade taken)")
            continue
        if in_cooldown(ticker):
            log(f"{ticker:<6} -> {action} suppressed, cooldown active")
            continue

        conf = float(quant.get("confidence") or 0.5)

        if action == "BUY":
            px = price_of(prices, ticker)
            if px is None:
                log(f"{ticker:<6} -> BUY skipped, no current price")
                continue
            qty = buy_qty(cash, px, conf)
            if qty < config.MIN_QTY_PER_TRADE:
                log(f"{ticker:<6} -> BUY skipped, size {qty} below minimum "
                    f"(cash ${cash:,.2f}, px ${px:,.2f})")
                continue
            if execute_trade(ticker, "buy", qty, reason):
                cash -= qty * px

        elif action == "SELL":
            qty = sell_qty(held, conf)
            if qty < config.MIN_QTY_PER_TRADE:
                log(f"{ticker:<6} -> SELL skipped, nothing held")
                continue
            execute_trade(ticker, "sell", qty, reason)


def main() -> None:
    log(f"backend   = {config.BACKEND_URL}")
    log(f"quant     = {config.QUANT_AGENT_URL}")
    log(f"sentiment = {config.SENTIMENT_AGENT_URL}")
    log("wallet    = agent (the user wallet is never touched)")
    while True:
        start = time.time()
        try:
            run_cycle()
        except Exception as exc:
            log(f"cycle error: {exc}")
        elapsed = time.time() - start
        time.sleep(max(1.0, config.ORCHESTRATOR_POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
