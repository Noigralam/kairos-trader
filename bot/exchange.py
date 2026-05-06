import pandas as pd
from binance.client import Client
from . import config

_client: Client = None


def get_client() -> Client:
    global _client
    if _client is None:
        # Public endpoints (market data) work without keys in simulation mode
        _client = Client(config.BINANCE_API_KEY or None, config.BINANCE_SECRET_KEY or None)
    return _client


def get_klines(pair: str, interval: str = None, limit: int = 100) -> pd.DataFrame:
    interval = interval or config.INTERVAL
    raw = get_client().get_klines(symbol=pair, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


def get_price(pair: str) -> float:
    ticker = get_client().get_symbol_ticker(symbol=pair)
    return float(ticker["price"])


def place_order(pair: str, side: str, quantity: float) -> dict:
    """Live trading only — never called in simulation mode."""
    return get_client().create_order(
        symbol=pair,
        side=side,
        type="MARKET",
        quantity=round(quantity, 6),
    )
