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
    ema_fast: float
    ema_slow: float


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
    ema_fast: int = 9,
    ema_slow: int = 21,
    ema_trend: int = 50,
) -> StrategyResult:
    """
    RSI + EMA crossover strategy.
    BUY  when EMA-fast crosses above EMA-slow AND RSI < 60 AND price > EMA-trend.
    SELL when EMA-fast crosses below EMA-slow OR RSI > 70 (overbought).
    """
    if len(df) < ema_trend + 2:
        return StrategyResult(Signal.HOLD, "Not enough candles", 0.0, 0.0, 0.0)

    close = df["close"]
    rsi_series = _rsi(close, rsi_period)
    ema_f_series = _ema(close, ema_fast)
    ema_s_series = _ema(close, ema_slow)

    ema_trend_series = _ema(close, ema_trend)

    rsi = float(rsi_series.iloc[-1])
    ema_f = float(ema_f_series.iloc[-1])
    ema_s = float(ema_s_series.iloc[-1])
    ema_t = float(ema_trend_series.iloc[-1])
    prev_ema_f = float(ema_f_series.iloc[-2])
    prev_ema_s = float(ema_s_series.iloc[-2])

    crossed_above = prev_ema_f <= prev_ema_s and ema_f > ema_s
    crossed_below = prev_ema_f >= prev_ema_s and ema_f < ema_s
    price = float(close.iloc[-1])

    if crossed_above and rsi < 65:
        if price < ema_t:
            return StrategyResult(Signal.HOLD, f"BUY blocked — price below EMA{ema_trend} (downtrend)", rsi, ema_f, ema_s)
        return StrategyResult(Signal.BUY, f"EMA crossover ↑, RSI={rsi:.1f}", rsi, ema_f, ema_s)
    if crossed_below or rsi > 65:
        reason = "EMA crossover ↓" if crossed_below else f"RSI overbought ({rsi:.1f})"
        return StrategyResult(Signal.SELL, reason, rsi, ema_f, ema_s)

    return StrategyResult(Signal.HOLD, f"No signal — RSI={rsi:.1f}", rsi, ema_f, ema_s)
