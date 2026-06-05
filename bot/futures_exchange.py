import math
import pandas as pd
from binance.client import Client
from . import config

_client: Client = None
_lot_step_cache: dict = {}
_min_notional_cache: dict = {}


def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(config.BINANCE_API_KEY or None, config.BINANCE_SECRET_KEY or None)
    return _client


def get_klines(symbol: str, interval: str = None, limit: int = 250) -> pd.DataFrame:
    interval = interval or config.SPOT_INTERVAL
    raw = get_client().futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def get_mark_price(symbol: str) -> float:
    """Mark price — Binance uses this for PnL and liquidation calculations."""
    resp = get_client().futures_mark_price(symbol=symbol)
    return float(resp["markPrice"])


def _fetch_exchange_info():
    return get_client().futures_exchange_info()


def get_lot_step(symbol: str) -> float:
    if symbol not in _lot_step_cache:
        for s in _fetch_exchange_info()["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        _lot_step_cache[symbol] = float(f["stepSize"])
                        break
                break
        else:
            _lot_step_cache[symbol] = 0.001
    return _lot_step_cache[symbol]


def get_min_notional(symbol: str) -> float:
    if symbol not in _min_notional_cache:
        val = 5.0
        for s in _fetch_exchange_info()["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "MIN_NOTIONAL":
                        val = float(f.get("notional", 5.0))
                        break
                break
        _min_notional_cache[symbol] = val
    return _min_notional_cache[symbol]


def round_qty(symbol: str, qty: float) -> float:
    step = get_lot_step(symbol)
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(qty / step) * step, precision)


def get_usdt_balance() -> float:
    """Available (free) USDT in the futures wallet."""
    for b in get_client().futures_account_balance():
        if b["asset"] == "USDT":
            return float(b["availableBalance"])
    return 0.0


def set_leverage(symbol: str, leverage: int) -> None:
    get_client().futures_change_leverage(symbol=symbol, leverage=leverage)


def set_margin_type(symbol: str, margin_type: str = "ISOLATED") -> None:
    """ISOLATED or CROSSED. Binance errors if already set — silently ignored."""
    try:
        get_client().futures_change_margin_type(symbol=symbol, marginType=margin_type)
    except Exception as e:
        if "No need to change margin type" not in str(e):
            raise


def get_position(symbol: str) -> dict | None:
    """
    Return the open position for symbol, or None if flat.
    Dict keys: symbol, side, amount, entry_price, unrealized_pnl,
                leverage, liquidation_price, margin.
    """
    for pos in get_client().futures_position_information(symbol=symbol):
        amt = float(pos["positionAmt"])
        if abs(amt) < 1e-9:
            continue
        return {
            "symbol":            symbol,
            "side":              "LONG" if amt > 0 else "SHORT",
            "amount":            abs(amt),
            "entry_price":       float(pos["entryPrice"]),
            "unrealized_pnl":    float(pos["unRealizedProfit"]),
            "leverage":          int(pos["leverage"]),
            "liquidation_price": float(pos["liquidationPrice"]),
            "margin":            float(pos.get("isolatedMargin") or 0),
        }
    return None


def get_funding_rate(symbol: str) -> float:
    """Most recent 8-hour funding rate (e.g. 0.0001 = 0.01%)."""
    rates = get_client().futures_funding_rate(symbol=symbol, limit=1)
    return float(rates[-1]["fundingRate"]) if rates else 0.0


def get_next_funding_time(symbol: str) -> int:
    """Unix ms timestamp of the next funding settlement."""
    resp = get_client().futures_mark_price(symbol=symbol)
    return int(resp["nextFundingTime"])


def place_order(symbol: str, side: str, quantity: float,
                reduce_only: bool = False) -> dict:
    """
    Market order on the futures account.
      side        — "BUY"  to open/add long or close short
                    "SELL" to open/add short or close long
      reduce_only — True ensures the order only reduces an existing position,
                    never opens a new one (safe close guard).
    Returns the raw Binance order response with fill details.
    """
    return get_client().futures_create_order(
        symbol=symbol,
        side=side,
        type="MARKET",
        quantity=round_qty(symbol, quantity),
        reduceOnly=reduce_only,
    )


def close_position(symbol: str) -> dict | None:
    """Fully close any open position for symbol, regardless of size."""
    pos = get_position(symbol)
    if pos is None:
        return None
    close_side = "SELL" if pos["side"] == "LONG" else "BUY"
    return place_order(symbol, close_side, pos["amount"], reduce_only=True)
