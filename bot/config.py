import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_TOKEN   = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID    = os.getenv("DISCORD_GUILD_ID", "")

MODE = os.getenv("MODE", "simulation")  # simulation | live
TRADING_PAIRS = [p.strip() for p in os.getenv("TRADING_PAIRS", "ETHEUR,SOLEUR").split(",") if p.strip()]
SIMULATION_BALANCE = float(os.getenv("SIMULATION_BALANCE", "200.0"))
BINANCE_FEE = float(os.getenv("BINANCE_FEE", "0.001"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.10"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.05"))
POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.25"))
INTERVAL = os.getenv("INTERVAL", "1h")
DCA_DROP_PCT      = float(os.getenv("DCA_DROP_PCT", "0.04"))
DCA_SIZE_PCT      = float(os.getenv("DCA_SIZE_PCT", "0.50"))
MIN_EXIT_PROFIT_PCT = float(os.getenv("MIN_EXIT_PROFIT_PCT", "0.02"))
PROFIT_FLOOR_PCT    = float(os.getenv("PROFIT_FLOOR_PCT", "0.03"))
RSI_PERIOD          = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD        = int(os.getenv("RSI_OVERSOLD", "30"))
RSI_OVERBOUGHT      = int(os.getenv("RSI_OVERBOUGHT", "65"))
EMA_GAP_PCT         = float(os.getenv("EMA_GAP_PCT", "0.02"))
DAILY_EMA_FILTER    = os.getenv("DAILY_EMA_FILTER", "false").lower() == "true"
TIME_STOP_DAYS      = float(os.getenv("TIME_STOP_DAYS", "0"))    # 0 = disabled
MAX_DRAWDOWN_PCT    = float(os.getenv("MAX_DRAWDOWN_PCT", "0"))  # 0 = disabled

def rsi_period_for(pair: str) -> int:
    return int(os.getenv(f"RSI_PERIOD_{pair}", RSI_PERIOD))

def rsi_overbought_for(pair: str) -> int:
    return int(os.getenv(f"RSI_OVERBOUGHT_{pair}", RSI_OVERBOUGHT))

def min_exit_for(pair: str) -> float:
    return float(os.getenv(f"MIN_EXIT_PROFIT_PCT_{pair}", MIN_EXIT_PROFIT_PCT))

WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8888"))
