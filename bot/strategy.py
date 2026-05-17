import pandas as pd
from dataclasses import dataclass
from enum import Enum


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class StrategyResult:
    signal: Signal
    reason: str
    rsi: float
    ema_trend: float


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_signal(
    df: pd.DataFrame,
    rsi_period: int = 14,
    rsi_oversold: int = 30,
    rsi_overbought: int = 65,
    ema_trend: int = 200,
) -> StrategyResult:
    """
    RSI mean-reversion with EMA200 trend guard.
    BUY  when RSI < rsi_oversold AND price > EMA200 (uptrend dip).
    SELL when RSI > rsi_overbought (recovery complete).
    """
    if len(df) < ema_trend + 2:
        return StrategyResult(Signal.HOLD, "Not enough candles", 0.0, 0.0)

    close = df["close"]
    rsi = float(_rsi(close, rsi_period).iloc[-1])
    ema_t = float(_ema(close, ema_trend).iloc[-1])
    price = float(close.iloc[-1])

    if rsi < rsi_oversold:
        if price < ema_t:
            return StrategyResult(Signal.HOLD, f"BUY blocked — price below EMA{ema_trend} (downtrend)", rsi, ema_t)
        return StrategyResult(Signal.BUY, f"RSI oversold ({rsi:.1f})", rsi, ema_t)

    if rsi > rsi_overbought:
        return StrategyResult(Signal.SELL, f"RSI recovered ({rsi:.1f})", rsi, ema_t)

    return StrategyResult(Signal.HOLD, f"No signal — RSI={rsi:.1f}", rsi, ema_t)
