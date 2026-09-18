"""
Shared configuration for the 3-agent trading pipeline.

Every network address here can be overridden with an environment variable,
so moving an agent to another laptop on the LAN is a one-line change
(or one `export`) with no code edits.

    export BACKEND_URL=http://192.168.1.10:8000
    export QUANT_AGENT_URL=http://192.168.1.11:8101
    export SENTIMENT_AGENT_URL=http://192.168.1.12:8102
"""

import os
from dotenv import load_dotenv

load_dotenv()
# --------------------------------------------------------------------------
# The one variable that matters: where the teammate's FastAPI backend lives.
# --------------------------------------------------------------------------
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")

# Where the quant / sentiment agents publish their signals, so the
# orchestrator can poll them across the LAN.
QUANT_AGENT_URL = os.getenv("QUANT_AGENT_URL", "http://localhost:8101").rstrip("/")
SENTIMENT_AGENT_URL = os.getenv("SENTIMENT_AGENT_URL", "http://localhost:8102").rstrip("/")

# Bind addresses for each agent's own little signal server.
QUANT_BIND_HOST = os.getenv("QUANT_BIND_HOST", "0.0.0.0")
QUANT_BIND_PORT = int(os.getenv("QUANT_BIND_PORT", "8101"))
SENTIMENT_BIND_HOST = os.getenv("SENTIMENT_BIND_HOST", "0.0.0.0")
SENTIMENT_BIND_PORT = int(os.getenv("SENTIMENT_BIND_PORT", "8102"))

# --------------------------------------------------------------------------
# Universe
# --------------------------------------------------------------------------
REAL_TICKERS = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD"]
INVENTED_TICKERS = ["NMBS", "ORVX", "ZYLA"]
TICKERS = REAL_TICKERS + INVENTED_TICKERS

# --------------------------------------------------------------------------
# Poll intervals (seconds)
# --------------------------------------------------------------------------
QUANT_POLL_INTERVAL = float(os.getenv("QUANT_POLL_INTERVAL", "10"))
SENTIMENT_POLL_INTERVAL = float(os.getenv("SENTIMENT_POLL_INTERVAL", "20"))
ORCHESTRATOR_POLL_INTERVAL = float(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "15"))

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10"))

# --------------------------------------------------------------------------
# Quant thresholds
# --------------------------------------------------------------------------
MIN_HISTORY_POINTS = 12      # below this we emit HOLD with low confidence
VWAP_WINDOW = 30             # points used for the rolling VWAP/TWAP baseline
ZSCORE_WINDOW = 30
ANOMALY_Z = 2.0              # |z| above this = "anomaly" for cross-validation
SIGNAL_Z = 1.0               # |z| above this = directional lean
VWAP_DEV_PCT = 0.75          # % deviation from VWAP that counts as meaningful
VOL_SPIKE_RATIO = 2.0        # recent vol / baseline vol that counts as a spike

# --------------------------------------------------------------------------
# Sentiment / LLM
# --------------------------------------------------------------------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
NEWS_LOOKBACK_SECONDS = float(os.getenv("NEWS_LOOKBACK_SECONDS", "3600"))
MAX_HEADLINES_PER_TICKER = 8
SENTIMENT_STRONG = 60        # severity at/above this is "strong"
# Severity at which strong positive news alone justifies an entry on a calm
# tape, even if the quant agent is only saying HOLD. This is what makes the
# injected-headline demo on an invented ticker actually trade.
NEWS_ONLY_BUY_SEVERITY = 65

# --------------------------------------------------------------------------
# Orchestrator risk controls
# --------------------------------------------------------------------------
MAX_POSITION_FRACTION = 0.10   # never put more than 10% of cash into one buy
CASH_BUFFER = 100.0            # always leave this much cash untouched
MAX_QTY_PER_TRADE = 25
MIN_QTY_PER_TRADE = 1
TRADE_COOLDOWN_SECONDS = 60    # per-ticker cooldown after any executed trade
SIGNAL_MAX_AGE_SECONDS = 180   # ignore agent signals older than this
