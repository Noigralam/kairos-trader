import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

MODE = os.getenv("MODE", "simulation")  # simulation | live
TRADING_PAIRS = ["BTCEUR", "ETHEUR"]
SIMULATION_BALANCE = float(os.getenv("SIMULATION_BALANCE", "200.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))
POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.10"))
INTERVAL = os.getenv("INTERVAL", "1h")

WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))
