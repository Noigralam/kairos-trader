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
    # com=period-1 gives alpha=1/period, which is Wilder's original smoothing (not SMA)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_signal(
    df: pd.DataFrame,
    rsi_period: int = 7,
    rsi_oversold: int = 30,
    rsi_overbought: int = 65,
    ema_trend: int = 200,
    ema_gap: float = 0.0,
    daily_ema: float = None,  # daily EMA200 value; None = disabled
) -> StrategyResult:
    """
    RSI mean-reversion with EMA200 trend guard.
    BUY  when RSI < rsi_oversold AND price > EMA200 * (1 + ema_gap).
    SELL when RSI > rsi_overbought (recovery complete).
    """
    if len(df) < ema_trend + 2:
        return StrategyResult(Signal.HOLD, "Not enough candles", 0.0, 0.0)

    close = df["close"]
    rsi = float(_rsi(close, rsi_period).iloc[-1])
    ema_t = float(_ema(close, ema_trend).iloc[-1])
    price = float(close.iloc[-1])
    ema_threshold = ema_t * (1 + ema_gap)

    if rsi < rsi_oversold:
        if price < ema_threshold:
            reason = (
                f"BUY blocked — price below EMA{ema_trend} (downtrend)"
                if price < ema_t else
                f"BUY blocked — price within {ema_gap*100:.0f}% gap of EMA{ema_trend}"
            )
            return StrategyResult(Signal.HOLD, reason, rsi, ema_t)
        if daily_ema is not None and price < daily_ema:
            return StrategyResult(Signal.HOLD,
                                  f"BUY blocked — price below daily EMA200 ({daily_ema:,.2f}) — sustained downtrend",
                                  rsi, ema_t)
        return StrategyResult(Signal.BUY, f"RSI oversold ({rsi:.1f})", rsi, ema_t)

    if rsi > rsi_overbought:
        return StrategyResult(Signal.SELL, f"RSI recovered ({rsi:.1f})", rsi, ema_t)

    return StrategyResult(Signal.HOLD, f"No signal — RSI={rsi:.1f}", rsi, ema_t)
