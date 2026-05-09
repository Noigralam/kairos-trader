import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

MODE = os.getenv("MODE", "simulation")  # simulation | live
TRADING_PAIRS = ["BTCEUR", "ETHEUR"]
SIMULATION_BALANCE = float(os.getenv("SIMULATION_BALANCE", "200.0"))
BINANCE_FEE = float(os.getenv("BINANCE_FEE", "0.001"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.10"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.05"))
POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.25"))
INTERVAL = os.getenv("INTERVAL", "1h")

WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8888"))
