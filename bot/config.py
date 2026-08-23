import os
from dotenv import load_dotenv

load_dotenv()

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

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
SPOT_INVESTED            = float(os.getenv("SPOT_INVESTED", "0"))                  # total capital deposited; overrides starting balance display (0 = use first balance_history entry)
SPOT_MODE                = os.getenv("SPOT_MODE", "simulation")                   # simulation | live
SPOT_TRADING_PAIRS       = [p.strip() for p in os.getenv("SPOT_TRADING_PAIRS", "ETHEUR,SOLEUR").split(",") if p.strip()]
SPOT_SIMULATION_BALANCE  = float(os.getenv("SPOT_SIMULATION_BALANCE", "200.0"))  # virtual EUR balance for simulation
SPOT_SHADOW_DEFAULT_BALANCE = float(os.getenv("SPOT_SHADOW_DEFAULT_BALANCE", "200.0"))  # starting balance for new shadows when no live state exists
SPOT_FEE                 = float(os.getenv("SPOT_FEE", "0.001"))                  # 0.1% Binance maker/taker rate
SPOT_INTERVAL            = os.getenv("SPOT_INTERVAL", "1h")                       # candle interval for signal computation
SPOT_POSITION_SIZE_PCT   = float(os.getenv("SPOT_POSITION_SIZE_PCT", "0.25"))     # fraction of balance spent per new position
SPOT_DCA_MAX             = int(os.getenv("SPOT_DCA_MAX", "3"))                    # max number of DCA tranches per position
SPOT_DCA_DROP_PCT        = float(os.getenv("SPOT_DCA_DROP_PCT", "0.04"))          # price drop from entry that triggers first DCA
SPOT_DCA_STEP_PCT        = float(os.getenv("SPOT_DCA_STEP_PCT", "0.01"))          # additional drop required for each subsequent DCA
SPOT_DCA_SIZE_PCT        = float(os.getenv("SPOT_DCA_SIZE_PCT", "0.50"))          # fraction of remaining balance used per DCA tranche
SPOT_TAKE_PROFIT_PCT     = float(os.getenv("SPOT_TAKE_PROFIT_PCT", "0.10"))       # hard take-profit above entry
SPOT_TRAILING_STOP_PCT   = float(os.getenv("SPOT_TRAILING_STOP_PCT", "0.05"))     # trailing stop drop from peak (only above profit floor)
SPOT_PROFIT_FLOOR_PCT    = float(os.getenv("SPOT_PROFIT_FLOOR_PCT", "0.03"))      # minimum profit required before trailing stop can fire
SPOT_MIN_EXIT_PROFIT_PCT = float(os.getenv("SPOT_MIN_EXIT_PROFIT_PCT", "0.02"))   # minimum profit before RSI sell signal can close position
SPOT_RSI_PERIOD          = int(os.getenv("SPOT_RSI_PERIOD", "14"))                # RSI lookback; shorter = more signals
SPOT_RSI_OVERSOLD        = int(os.getenv("SPOT_RSI_OVERSOLD", "30"))              # buy when RSI drops below this
SPOT_RSI_OVERBOUGHT      = int(os.getenv("SPOT_RSI_OVERBOUGHT", "65"))            # sell when RSI rises above this
SPOT_EMA_GAP_PCT         = float(os.getenv("SPOT_EMA_GAP_PCT", "0.02"))           # price must be this % above EMA200 to allow a buy
SPOT_DAILY_EMA_FILTER    = os.getenv("SPOT_DAILY_EMA_FILTER", "false").lower() == "true"  # also require price > daily EMA200
SPOT_TIME_STOP_DAYS      = float(os.getenv("SPOT_TIME_STOP_DAYS", "0"))           # close stalled positions after N days; 0 = disabled
SPOT_MAX_DRAWDOWN_PCT    = float(os.getenv("SPOT_MAX_DRAWDOWN_PCT", "0"))         # pause buys if portfolio drops >X% from peak; 0 = disabled
SPOT_STOP_CHECK_INTERVAL = int(os.getenv("SPOT_STOP_CHECK_INTERVAL", "60"))       # seconds between between-candle stop checks
SPOT_STOP_COOLDOWN_CANDLES  = int(os.getenv("SPOT_STOP_COOLDOWN_CANDLES", "0"))   # block re-entry for N candles after a trailing stop; 0 = disabled
SPOT_VOLUME_FILTER_PERIOD   = int(os.getenv("SPOT_VOLUME_FILTER_PERIOD", "0"))    # volume must exceed rolling mean; 0 = disabled
SPOT_VOLUME_FILTER_MULT     = float(os.getenv("SPOT_VOLUME_FILTER_MULT", "1.5"))  # volume multiple required
SPOT_PARTIAL_CLOSE_PCT      = float(os.getenv("SPOT_PARTIAL_CLOSE_PCT", "0"))     # sell this fraction at TP, hold rest; 0 = disabled
SPOT_PARTIAL_CLOSE_TRAIL_PCT = float(os.getenv("SPOT_PARTIAL_CLOSE_TRAIL_PCT", "0.02"))  # trailing stop on remainder after partial close

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

def time_stop_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_TIME_STOP_DAYS_{pair}", SPOT_TIME_STOP_DAYS))

def take_profit_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_TAKE_PROFIT_PCT_{pair}", SPOT_TAKE_PROFIT_PCT))

def trailing_stop_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_TRAILING_STOP_PCT_{pair}", SPOT_TRAILING_STOP_PCT))

def profit_floor_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_PROFIT_FLOOR_PCT_{pair}", SPOT_PROFIT_FLOOR_PCT))

def dca_drop_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_DCA_DROP_PCT_{pair}", SPOT_DCA_DROP_PCT))

def dca_max_for(pair: str) -> int:
    return int(os.getenv(f"SPOT_DCA_MAX_{pair}", SPOT_DCA_MAX))

def stop_cooldown_for(pair: str) -> int:
    return int(os.getenv(f"SPOT_STOP_COOLDOWN_CANDLES_{pair}", SPOT_STOP_COOLDOWN_CANDLES))

def vol_period_for(pair: str) -> int:
    return int(os.getenv(f"SPOT_VOLUME_FILTER_PERIOD_{pair}", SPOT_VOLUME_FILTER_PERIOD))

def vol_mult_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_VOLUME_FILTER_MULT_{pair}", SPOT_VOLUME_FILTER_MULT))

def partial_close_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_PARTIAL_CLOSE_PCT_{pair}", SPOT_PARTIAL_CLOSE_PCT))

def partial_close_trail_for(pair: str) -> float:
    return float(os.getenv(f"SPOT_PARTIAL_CLOSE_TRAIL_PCT_{pair}", SPOT_PARTIAL_CLOSE_TRAIL_PCT))

# ---------------------------------------------------------------------------
# Futures (USDT-M perpetuals)
# ---------------------------------------------------------------------------
FUTURES_ENABLED             = os.getenv("FUTURES_ENABLED", "false").lower() == "true"  # set to false to disable the futures engine entirely
FUTURES_MODE                = os.getenv("FUTURES_MODE", "simulation")                   # simulation | live
FUTURES_TRADING_PAIRS       = [p.strip() for p in os.getenv("FUTURES_TRADING_PAIRS", "ETHUSDT,SOLUSDT").split(",") if p.strip()]
FUTURES_SIMULATION_BALANCE  = float(os.getenv("FUTURES_SIMULATION_BALANCE", "200.0"))  # virtual USDT balance for simulation
FUTURES_SHADOW_DEFAULT_BALANCE = float(os.getenv("FUTURES_SHADOW_DEFAULT_BALANCE", "200.0"))  # starting balance for new shadows when no live futures state exists
FUTURES_LEVERAGE            = int(os.getenv("FUTURES_LEVERAGE", "2"))                   # notional = margin × leverage
FUTURES_MARGIN_TYPE         = os.getenv("FUTURES_MARGIN_TYPE", "ISOLATED")              # ISOLATED | CROSSED
FUTURES_FEE                 = float(os.getenv("FUTURES_FEE", "0.0005"))                 # 0.05% taker; funding adds on top for open longs
FUTURES_POSITION_SIZE_PCT   = float(os.getenv("FUTURES_POSITION_SIZE_PCT", "0.75"))     # fraction of balance posted as margin per position
FUTURES_DCA_DROP_PCT        = float(os.getenv("FUTURES_DCA_DROP_PCT", "0.01"))          # price drop from entry that triggers DCA
FUTURES_DCA_SIZE_PCT        = float(os.getenv("FUTURES_DCA_SIZE_PCT", "0.75"))          # fraction of remaining balance used for DCA tranche
FUTURES_TAKE_PROFIT_PCT     = float(os.getenv("FUTURES_TAKE_PROFIT_PCT", "0.05"))       # hard take-profit above entry (on notional)
FUTURES_TRAILING_STOP_PCT   = float(os.getenv("FUTURES_TRAILING_STOP_PCT", "0.05"))     # trailing stop drop from peak (only above profit floor)
FUTURES_PROFIT_FLOOR_PCT    = float(os.getenv("FUTURES_PROFIT_FLOOR_PCT", "0.03"))      # minimum profit before trailing stop can fire
FUTURES_MIN_EXIT_PROFIT_PCT = float(os.getenv("FUTURES_MIN_EXIT_PROFIT_PCT", "0.02"))   # minimum profit before signal exit can fire
FUTURES_MAX_DRAWDOWN_PCT    = float(os.getenv("FUTURES_MAX_DRAWDOWN_PCT", "0"))         # pause longs if portfolio drops >X% from peak; 0 = disabled
FUTURES_MAX_FUNDING_RATE    = float(os.getenv("FUTURES_MAX_FUNDING_RATE", "0"))          # skip longs when 8h funding rate exceeds this; 0 = disabled
FUTURES_STOP_CHECK_INTERVAL = int(os.getenv("FUTURES_STOP_CHECK_INTERVAL", "60"))       # seconds between between-candle stop/funding checks

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
# Shadow simulation profiles
# ---------------------------------------------------------------------------

def get_shadow_profiles() -> list[str]:
    raw = os.getenv("SPOT_SHADOW_PROFILES", "")
    return [p.strip().upper() for p in raw.split(",") if p.strip()]


def get_shadow_overrides(name: str) -> dict:
    """Parse SPOT_SHADOW_{NAME}_* env vars into an overrides dict for SpotShadowSimulator."""
    prefix = f"SPOT_SHADOW_{name.upper()}_"
    # fmt: off  (keep columns aligned for readability)
    _param_map: dict[str, tuple[str, type]] = {
        # ── Entry signal ──────────────────────────────────────────
        "RSI_OVERSOLD":            ("spot_rsi_oversold",        int),
        "RSI_OVERBOUGHT":          ("spot_rsi_overbought",      int),
        "RSI_PERIOD":              ("spot_rsi_period",          int),
        "EMA_GAP_PCT":             ("spot_ema_gap",             float),
        # ── Exit / risk ───────────────────────────────────────────
        "TRAILING_STOP_PCT":       ("spot_trail_pct",           float),
        "PROFIT_FLOOR_PCT":        ("spot_floor_pct",           float),
        "TAKE_PROFIT_PCT":         ("spot_tp_pct",              float),
        "MIN_EXIT_PROFIT_PCT":     ("spot_min_exit",            float),
        "TIME_STOP_DAYS":          ("spot_time_stop_days",      float),
        "STOP_COOLDOWN_CANDLES":   ("spot_stop_cooldown",       int),
        "REENTRY_DROP_PCT":        ("spot_reentry_drop_pct",   float),
        "PARTIAL_CLOSE_PCT":       ("spot_partial_close_pct",   float),
        "PARTIAL_CLOSE_TRAIL_PCT": ("spot_partial_close_trail", float),
        # ── Position sizing / DCA ─────────────────────────────────
        "POSITION_SIZE_PCT":       ("spot_pos_pct",             float),
        "DCA_DROP_PCT":            ("spot_dca_drop",            float),
        "DCA_SIZE_PCT":            ("spot_dca_pct",             float),
        "DCA_MAX":                 ("spot_dca_max",             int),
        "DCA_STEP_PCT":            ("spot_dca_step",            float),
        "VOLUME_FILTER_PERIOD":    ("spot_vol_period",          int),
        "VOLUME_FILTER_MULT":      ("spot_vol_mult",            float),
        # ── Sentiment filter ──────────────────────────────────────
        "FNG_MAX":                 ("spot_fng_max",             int),
        # ── Simulation / grid ─────────────────────────────────────
        "BALANCE":                 ("spot_balance",             float),
        "GRID_SPACING":            ("spot_grid_spacing",        float),
        "GRID_LEVELS":             ("spot_grid_levels",         int),
    }
    # fmt: on
    result: dict = {}
    # string params
    type_val = os.getenv(f"{prefix}TYPE")
    if type_val:
        result["type"] = type_val.lower().strip()
    for env_suffix, (key, cast) in _param_map.items():
        val = os.getenv(f"{prefix}{env_suffix}")
        if val is not None:
            try:
                result[key] = cast(val)
            except ValueError:
                pass
    pairs_raw = os.getenv(f"{prefix}PAIRS")
    if pairs_raw:
        result["pairs"] = [p.strip().upper() for p in pairs_raw.split(",") if p.strip()]
    interval_raw = os.getenv(f"{prefix}INTERVAL")
    if interval_raw:
        result["interval"] = interval_raw.strip()
    return result


def get_futures_shadow_profiles() -> list[str]:
    raw = os.getenv("FUTURES_SHADOW_PROFILES", "")
    return [p.strip().upper() for p in raw.split(",") if p.strip()]


def get_futures_shadow_overrides(name: str) -> dict:
    """Parse FUTURES_SHADOW_{NAME}_* env vars into an overrides dict for FuturesShadowSimulator."""
    prefix = f"FUTURES_SHADOW_{name.upper()}_"
    # fmt: off
    _param_map: dict[str, tuple[str, type]] = {
        # ── Futures-specific ──────────────────────────────────────
        "LEVERAGE":          ("futures_leverage",         int),
        "POSITION_SIZE_PCT": ("futures_pos_pct",          float),
        "MAX_FUNDING_RATE":  ("futures_max_funding_rate", float),
        # ── Exit / risk ───────────────────────────────────────────
        "TAKE_PROFIT_PCT":   ("futures_tp_pct",           float),
        "TRAILING_STOP_PCT": ("futures_trail_pct",        float),
        "PROFIT_FLOOR_PCT":  ("futures_floor_pct",        float),
        "DCA_DROP_PCT":      ("futures_dca_drop",         float),
        "DCA_SIZE_PCT":      ("futures_dca_pct",          float),
        # ── Entry signal ──────────────────────────────────────────
        "RSI_OVERSOLD":      ("futures_rsi_oversold",     int),
        "RSI_OVERBOUGHT":    ("futures_rsi_overbought",   int),
        "RSI_PERIOD":        ("futures_rsi_period",       int),
        "EMA_GAP_PCT":       ("futures_ema_gap",          float),
        # ── Simulation ────────────────────────────────────────────
        "BALANCE":           ("futures_balance",          float),
    }
    # fmt: on
    result: dict = {}
    for env_suffix, (key, cast) in _param_map.items():
        val = os.getenv(f"{prefix}{env_suffix}")
        if val is not None:
            try:
                result[key] = cast(val)
            except ValueError:
                pass
    symbols_raw = os.getenv(f"{prefix}SYMBOLS")
    if symbols_raw:
        result["symbols"] = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    return result

# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")  # 0.0.0.0 to expose on LAN
WEB_PORT = int(os.getenv("WEB_PORT", "8888"))
