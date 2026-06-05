import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_TOKEN   = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID    = os.getenv("DISCORD_GUILD_ID", "")

# ---------------------------------------------------------------------------
# Spot (EUR pairs)
# ---------------------------------------------------------------------------
SPOT_MODE               = os.getenv("SPOT_MODE", "simulation")  # simulation | live
SPOT_TRADING_PAIRS      = [p.strip() for p in os.getenv("SPOT_TRADING_PAIRS", "ETHEUR,SOLEUR").split(",") if p.strip()]
SPOT_SIMULATION_BALANCE = float(os.getenv("SPOT_SIMULATION_BALANCE", "200.0"))
SPOT_FEE                = float(os.getenv("SPOT_FEE", "0.001"))
SPOT_INTERVAL           = os.getenv("SPOT_INTERVAL", "1h")
SPOT_POSITION_SIZE_PCT  = float(os.getenv("SPOT_POSITION_SIZE_PCT", "0.25"))
SPOT_DCA_DROP_PCT       = float(os.getenv("SPOT_DCA_DROP_PCT", "0.04"))
SPOT_DCA_SIZE_PCT       = float(os.getenv("SPOT_DCA_SIZE_PCT", "0.50"))
SPOT_TAKE_PROFIT_PCT    = float(os.getenv("SPOT_TAKE_PROFIT_PCT", "0.10"))
SPOT_TRAILING_STOP_PCT  = float(os.getenv("SPOT_TRAILING_STOP_PCT", "0.05"))
SPOT_PROFIT_FLOOR_PCT   = float(os.getenv("SPOT_PROFIT_FLOOR_PCT", "0.03"))
SPOT_MIN_EXIT_PROFIT_PCT = float(os.getenv("SPOT_MIN_EXIT_PROFIT_PCT", "0.02"))
SPOT_RSI_PERIOD         = int(os.getenv("SPOT_RSI_PERIOD", "14"))
SPOT_RSI_OVERSOLD       = int(os.getenv("SPOT_RSI_OVERSOLD", "30"))
SPOT_RSI_OVERBOUGHT     = int(os.getenv("SPOT_RSI_OVERBOUGHT", "65"))
SPOT_EMA_GAP_PCT        = float(os.getenv("SPOT_EMA_GAP_PCT", "0.02"))
SPOT_DAILY_EMA_FILTER   = os.getenv("SPOT_DAILY_EMA_FILTER", "false").lower() == "true"
SPOT_TIME_STOP_DAYS     = float(os.getenv("SPOT_TIME_STOP_DAYS", "0"))   # 0 = disabled
SPOT_MAX_DRAWDOWN_PCT   = float(os.getenv("SPOT_MAX_DRAWDOWN_PCT", "0")) # 0 = disabled
SPOT_STOP_CHECK_INTERVAL = int(os.getenv("SPOT_STOP_CHECK_INTERVAL", "60"))  # seconds

def rsi_period_for(pair: str) -> int:
    return int(os.getenv(f"SPOT_RSI_PERIOD_{pair}", SPOT_RSI_PERIOD))

def rsi_oversold_for(pair: str) -> int:
    return int(os.getenv(f"SPOT_RSI_OVERSOLD_{pair}", SPOT_RSI_OVERSOLD))

def rsi_overbought_for(pair: str) -> int:
    return int(os.getenv(f"SPOT_RSI_OVERBOUGHT_{pair}", SPOT_RSI_OVERBOUGHT))

def ema_gap_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_EMA_GAP_PCT_{pair}", SPOT_EMA_GAP_PCT))

def min_exit_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_MIN_EXIT_PROFIT_PCT_{pair}", SPOT_MIN_EXIT_PROFIT_PCT))

# ---------------------------------------------------------------------------
# Futures (USDT-M perpetuals)
# ---------------------------------------------------------------------------
FUTURES_ENABLED          = os.getenv("FUTURES_ENABLED", "false").lower() == "true"
FUTURES_MODE             = os.getenv("FUTURES_MODE", "simulation")   # simulation | live
FUTURES_TRADING_PAIRS    = [p.strip() for p in os.getenv("FUTURES_TRADING_PAIRS", "ETHUSDT,SOLUSDT").split(",") if p.strip()]
FUTURES_SIMULATION_BALANCE = float(os.getenv("FUTURES_SIMULATION_BALANCE", "200.0"))
FUTURES_LEVERAGE         = int(os.getenv("FUTURES_LEVERAGE", "2"))
FUTURES_MARGIN_TYPE      = os.getenv("FUTURES_MARGIN_TYPE", "ISOLATED")   # ISOLATED | CROSSED
FUTURES_FEE              = float(os.getenv("FUTURES_FEE", "0.0005"))       # 0.05% taker (funding adds to this over time)
FUTURES_POSITION_SIZE_PCT = float(os.getenv("FUTURES_POSITION_SIZE_PCT", "0.75"))
FUTURES_DCA_DROP_PCT     = float(os.getenv("FUTURES_DCA_DROP_PCT", "0.01"))
FUTURES_DCA_SIZE_PCT     = float(os.getenv("FUTURES_DCA_SIZE_PCT", "0.75"))
FUTURES_TAKE_PROFIT_PCT  = float(os.getenv("FUTURES_TAKE_PROFIT_PCT", "0.05"))
FUTURES_TRAILING_STOP_PCT = float(os.getenv("FUTURES_TRAILING_STOP_PCT", "0.05"))
FUTURES_PROFIT_FLOOR_PCT = float(os.getenv("FUTURES_PROFIT_FLOOR_PCT", "0.03"))
FUTURES_MIN_EXIT_PROFIT_PCT = float(os.getenv("FUTURES_MIN_EXIT_PROFIT_PCT", "0.02"))
FUTURES_MAX_DRAWDOWN_PCT = float(os.getenv("FUTURES_MAX_DRAWDOWN_PCT", "0"))
FUTURES_STOP_CHECK_INTERVAL = int(os.getenv("FUTURES_STOP_CHECK_INTERVAL", "60"))  # seconds

def futures_rsi_period_for(pair: str) -> int:
    return int(os.getenv(f"FUTURES_RSI_PERIOD_{pair}", os.getenv("FUTURES_RSI_PERIOD", "7")))

def futures_rsi_oversold_for(pair: str) -> int:
    # Default of 30 here; .env sets 25 — extreme entries recover faster with lower funding cost.
    # Backtests (Jun 2024–Jun 2026) show RSI<25 avoids slow-recovering positions that bleed funding.
    return int(os.getenv(f"FUTURES_RSI_OVERSOLD_{pair}", os.getenv("FUTURES_RSI_OVERSOLD", "30")))

def futures_rsi_overbought_for(pair: str) -> int:
    return int(os.getenv(f"FUTURES_RSI_OVERBOUGHT_{pair}", os.getenv("FUTURES_RSI_OVERBOUGHT", "75")))

def futures_ema_gap_for(pair: str) -> float:
    return float(os.getenv(f"FUTURES_EMA_GAP_PCT_{pair}", os.getenv("FUTURES_EMA_GAP_PCT", "0.02")))

# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8888"))
