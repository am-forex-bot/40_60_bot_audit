#!/usr/bin/env python3
"""
30-Minute Window Optimizer V2 for the 40/60 Bot

Changes from V1:
  - No forward candle limit — trades resolve fully against all available data
  - Concurrent trade limit: 1 per pair (matching bot's actual behavior)
  - Per-pair per-window analysis (not just global windows)
  - Walk-forward validation (expanding window, 3-month non-overlapping test periods)
  - Drawdown & max consecutive loss analysis
  - Net slippage modeling (adverse slippage on entry)
  - Stricter validation: BOTH train AND test must beat breakeven WR per fold

Uses the bot's ACTUAL signal generation logic (momentum pullback, trend following,
volatility breakout) against 5-second parquet data resampled to M1/M5/M15/H1/H4.

For each pair:
  - Resamples 5s data to all timeframes the bot uses
  - Evaluates the bot's signal logic at every M5 bar
  - When a signal fires, checks no concurrent trade is open on that pair
  - Simulates the trade using 5s bid/ask data (with slippage)
  - Records outcome grouped by 30-minute window
  - Walk-forward validates each pair-window combination

Requirements:
    pip install pandas numpy pyarrow ta-lib

Usage:
    python3 optimize_windows_5s.py --data-dir /path/to/parquet/files

    # With custom slippage
    python3 optimize_windows_5s.py --data-dir /path/to/parquets --slippage 0.5

    # Specific pairs
    python3 optimize_windows_5s.py --data-dir /path/to/parquets --pairs GBP_USD EUR_USD
"""

import argparse
import glob
import os
import sys
import time as time_mod
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import talib


# ─────────────────────────────────────────────────────────
# Bot's configuration (extracted from forex_bot_40_60.py)
# ─────────────────────────────────────────────────────────

TP_PIPS = 40
SL_PIPS = 60
CONFIDENCE_THRESHOLD = 0.60
MIN_RISK_REWARD = 0.5
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
BB_STDDEV = 2.0
ATR_PERIOD = 14
MIN_ATR_PIPS = 5
MAX_ATR_PIPS = 100
HURST_THRESHOLD = 0.52
HURST_WINDOW = 100
HURST_MIN_WINDOW = 10
HURST_MAX_WINDOW = 50
HURST_NUM_WINDOWS = 15

# No forward candle limit — trades resolve against all remaining data

# Min samples for a pair-window to be considered
MIN_SAMPLE_SIZE = 100

# Walk-forward validation defaults
WF_TRAIN_MONTHS = 9
WF_TEST_MONTHS = 3
MIN_FOLD_TRADES = 15
MIN_FOLDS_REQUIRED = 3
MIN_PASS_RATE = 0.60  # 60% of folds must pass

# Slippage (adverse, applied to entry)
SLIPPAGE_DEFAULT = 0.3  # pips

BREAKEVEN_WR = SL_PIPS / (TP_PIPS + SL_PIPS)  # 0.60

ALL_PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD",
             "EUR_GBP", "GBP_JPY", "EUR_JPY", "AUD_JPY", "NZD_USD",
             "EUR_AUD", "GBP_AUD"]

# Timeframe weights for multi-TF bias (from bot)
TF_WEIGHTS = {'M1': 0.05, 'M5': 0.20, 'M15': 0.30, 'H1': 0.25, 'H4': 0.20}


def pip_val(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def window_label(h: int, hh: int) -> str:
    return f"{h:02d}:{hh*30:02d}-{h:02d}:{hh*30+29:02d}"


# ─────────────────────────────────────────────────────────
# Data loading & resampling
# ─────────────────────────────────────────────────────────

def find_parquet(data_dir: str, pair: str) -> Optional[str]:
    for pattern in [f"{pair}_S5_*.parquet", f"{pair}*.parquet"]:
        matches = glob.glob(os.path.join(data_dir, pattern))
        if matches:
            return matches[0]
    return None


def load_5s(path: str, start: str = None, end: str = None) -> pd.DataFrame:
    """Load 5s parquet with all columns needed."""
    filters = []
    if start:
        filters.append(("time", ">=", pd.Timestamp(start, tz="UTC")))
    if end:
        filters.append(("time", "<=", pd.Timestamp(end, tz="UTC")))

    df = pq.read_table(path, filters=filters or None).to_pandas()
    df = df.sort_values("time").reset_index(drop=True)
    df = df.set_index("time")
    return df


def resample_ohlcv(df_5s: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample 5s data to a higher timeframe. freq: '1min','5min','15min','1h','4h'"""
    ohlc = df_5s[["open", "high", "low", "close"]].resample(freq).agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()

    if "volume" in df_5s.columns:
        vol = df_5s["volume"].resample(freq).sum()
        ohlc["volume"] = vol
    else:
        ohlc["volume"] = 0

    return ohlc


# ─────────────────────────────────────────────────────────
# Bot's indicator computation (exact copy from bot)
# ─────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Exact replica of the bot's _add_indicators()"""
    if len(df) < 26:
        return df

    close = df["close"].values.astype(np.float64)
    high = df["high"].values.astype(np.float64)
    low = df["low"].values.astype(np.float64)
    vol = df["volume"].values.astype(np.float64) if "volume" in df.columns else np.ones(len(df))

    df["sma_20"] = talib.SMA(close, timeperiod=20)
    df["sma_50"] = talib.SMA(close, timeperiod=50)
    df["ema_9"] = talib.EMA(close, timeperiod=9)
    df["ema_21"] = talib.EMA(close, timeperiod=21)
    df["rsi"] = talib.RSI(close, timeperiod=RSI_PERIOD)

    macd, macd_sig, macd_hist = talib.MACD(close, fastperiod=MACD_FAST,
                                            slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)
    df["macd"] = macd
    df["macd_signal"] = macd_sig
    df["macd_hist"] = macd_hist

    upper, middle, lower = talib.BBANDS(close, timeperiod=BB_PERIOD,
                                         nbdevup=BB_STDDEV, nbdevdn=BB_STDDEV)
    df["bb_upper"] = upper
    df["bb_middle"] = middle
    df["bb_lower"] = lower

    df["atr"] = talib.ATR(high, low, close, timeperiod=ATR_PERIOD)

    stoch_k, stoch_d = talib.STOCH(high, low, close,
                                     fastk_period=14, slowk_period=3, slowd_period=3)
    df["stoch_k"] = stoch_k
    df["stoch_d"] = stoch_d

    df["momentum"] = df["close"] - df["close"].shift(10)
    df["roc"] = talib.ROC(close, timeperiod=10)

    if vol.sum() > 0:
        df["volume_sma"] = talib.SMA(vol, timeperiod=20)
        df["volume_ratio"] = np.where(df["volume_sma"] > 0, df["volume"] / df["volume_sma"], 1.0)
    else:
        df["volume_ratio"] = 1.0

    return df


# ─────────────────────────────────────────────────────────
# Bot's analysis functions (exact replicas)
# ─────────────────────────────────────────────────────────

def get_timeframe_trend(df_tf: pd.DataFrame) -> float:
    """Exact replica of bot's _get_timeframe_trend()"""
    if df_tf.empty or len(df_tf) < 20:
        return 0.0
    latest = df_tf.iloc[-1]
    if "ema_9" in df_tf.columns and "ema_21" in df_tf.columns:
        if pd.notna(latest["ema_9"]) and pd.notna(latest["ema_21"]):
            if latest["ema_9"] > latest["ema_21"] and latest["close"] > latest["ema_9"]:
                return 1.0
            elif latest["ema_9"] < latest["ema_21"] and latest["close"] < latest["ema_9"]:
                return -1.0
    return 0.0


def get_multi_tf_bias(tf_dataframes: Dict[str, pd.DataFrame]) -> float:
    """Exact replica of bot's get_multi_timeframe_bias()"""
    weighted_bias = 0.0
    total_weight = 0.0
    for tf_name, weight in TF_WEIGHTS.items():
        if tf_name in tf_dataframes:
            trend = get_timeframe_trend(tf_dataframes[tf_name])
            weighted_bias += trend * weight
            total_weight += weight
    if total_weight > 0 and total_weight != 1.0:
        weighted_bias /= total_weight
    return np.clip(weighted_bias, -1.0, 1.0)


def calculate_trend_strength(df: pd.DataFrame) -> float:
    """Exact replica of bot's _calculate_trend_strength()"""
    if len(df) < 50:
        return 0.0
    latest = df.iloc[-1]
    score = 0.0

    if "ema_9" in df.columns and "ema_21" in df.columns:
        if pd.notna(latest["ema_9"]) and pd.notna(latest["ema_21"]):
            score += 0.3 if latest["ema_9"] > latest["ema_21"] else -0.3

    if "sma_20" in df.columns and pd.notna(latest.get("sma_20")):
        score += 0.2 if latest["close"] > latest["sma_20"] else -0.2

    if "macd" in df.columns and "macd_signal" in df.columns:
        if pd.notna(latest.get("macd")) and pd.notna(latest.get("macd_signal")):
            score += 0.25 if latest["macd"] > latest["macd_signal"] else -0.25

    if "momentum" in df.columns and pd.notna(latest.get("momentum")):
        score += 0.25 if latest["momentum"] > 0 else -0.25

    return np.clip(score, -1.0, 1.0)


def compute_hurst(closes: np.ndarray) -> float:
    """Exact replica of bot's compute_hurst_exponent() — data already provided."""
    if len(closes) < HURST_WINDOW:
        return 0.5

    closes = closes[-HURST_WINDOW:]
    returns = np.diff(np.log(closes))

    if len(returns) < HURST_MIN_WINDOW * 2:
        return 0.5

    window_sizes = np.unique(np.logspace(
        np.log10(HURST_MIN_WINDOW),
        np.log10(min(HURST_MAX_WINDOW, len(returns) // 2)),
        HURST_NUM_WINDOWS
    ).astype(int))

    if len(window_sizes) < 3:
        return 0.5

    rs_values = []
    for w in window_sizes:
        rs_list = []
        n_windows = len(returns) // w
        for i in range(n_windows):
            chunk = returns[i * w:(i + 1) * w]
            mean_chunk = np.mean(chunk)
            deviations = np.cumsum(chunk - mean_chunk)
            R = np.max(deviations) - np.min(deviations)
            S = np.std(chunk, ddof=1)
            if S > 1e-10:
                rs_list.append(R / S)
        if rs_list:
            rs_values.append((w, np.mean(rs_list)))

    if len(rs_values) < 3:
        return 0.5

    log_sizes = np.log(np.array([x[0] for x in rs_values]))
    log_rs = np.log(np.array([x[1] for x in rs_values]))

    n = len(log_sizes)
    sum_x = np.sum(log_sizes)
    sum_y = np.sum(log_rs)
    sum_xy = np.sum(log_sizes * log_rs)
    sum_x2 = np.sum(log_sizes ** 2)
    denom = n * sum_x2 - sum_x ** 2
    if abs(denom) < 1e-10:
        return 0.5

    hurst = float((n * sum_xy - sum_x * sum_y) / denom)
    return float(np.clip(hurst, 0.0, 1.0))


def get_order_flow_bias(df_m1: pd.DataFrame) -> float:
    """Exact replica of bot's get_order_flow_bias() — uses last 60 M1 bars."""
    if df_m1.empty or len(df_m1) < 60:
        return 0.0

    df = df_m1.iloc[-60:]
    vwap = ((df["high"] + df["low"] + df["close"]) / 3).mean()
    current_price = df["close"].iloc[-1]

    buying_bars = len(df[df["close"] > df["open"]])
    buying_pressure = buying_bars / len(df)

    rejections_up = 0
    rejections_down = 0
    for i in range(-10, -1):
        candle = df.iloc[i]
        body = abs(candle["close"] - candle["open"])
        upper_wick = candle["high"] - max(candle["close"], candle["open"])
        lower_wick = min(candle["close"], candle["open"]) - candle["low"]
        if body > 0:
            if upper_wick > body * 2:
                rejections_up += 1
            if lower_wick > body * 2:
                rejections_down += 1

    bias = 0.0
    if current_price > vwap and buying_pressure > 0.52:
        bias = (buying_pressure - 0.5) * 2
    elif current_price < vwap and buying_pressure < 0.48:
        bias = -((0.5 - buying_pressure) * 2)

    if rejections_up > 2:
        bias -= 0.2
    if rejections_down > 2:
        bias += 0.2

    return float(np.clip(bias, -1.0, 1.0))


def get_session_quality(utc_hour: int, weekday: int) -> Tuple[str, float]:
    """Replica of bot's get_current_session() using UTC hour."""
    if weekday >= 5:
        return "weekend", 0.0

    # Approximate: UTC ≈ London time (close enough for GMT/BST overlap)
    # Tokyo: 0-8 UTC, London: 7-16 UTC, NY: 13-21 UTC
    tokyo_active = 0 <= utc_hour < 8
    london_active = 7 <= utc_hour < 16
    ny_active = 13 <= utc_hour < 21

    if london_active and ny_active:
        return "london_ny_overlap", 1.0
    if tokyo_active and london_active:
        return "tokyo_london_overlap", 0.85
    if london_active:
        return "london", 0.9
    if ny_active:
        return "ny", 0.8
    if tokyo_active:
        return "tokyo", 0.7
    return "dead", 0.3


# ─────────────────────────────────────────────────────────
# Bot's signal strategies (exact replicas)
# ─────────────────────────────────────────────────────────

def momentum_pullback_signal(df_m5: pd.DataFrame, market: dict, pair: str) -> Optional[dict]:
    """Exact replica of bot's momentum_pullback_signal()"""
    if df_m5.empty or len(df_m5) < 20:
        return None

    latest = df_m5.iloc[-1]
    if abs(market["multi_tf_bias"]) < 0.5:
        return None

    momentum = (df_m5["close"].iloc[-1] / df_m5["close"].iloc[-12] - 1)
    pv = 100 if "JPY" in pair else 10000
    momentum_pips = momentum * pv

    if abs(momentum_pips) < 10:
        return None

    signal = None
    confidence = 0.5
    last_3 = df_m5.iloc[-3:]

    if momentum_pips > 0 and market["multi_tf_bias"] > 0:
        pullback = last_3["low"].min() < df_m5["close"].iloc[-4]
        if pullback and latest["rsi"] < 70 and market["order_flow"] > -0.3:
            signal = "buy"
            confidence += 0.2
            if market["session_quality"] > 0.8:
                confidence += 0.1
            if market["trend_direction"] == "bullish":
                confidence += 0.1
            confidence += market["multi_tf_bias"] * 0.1

    elif momentum_pips < 0 and market["multi_tf_bias"] < 0:
        pullback = last_3["high"].max() > df_m5["close"].iloc[-4]
        if pullback and latest["rsi"] > 30 and market["order_flow"] < 0.3:
            signal = "sell"
            confidence += 0.2
            if market["session_quality"] > 0.8:
                confidence += 0.1
            if market["trend_direction"] == "bearish":
                confidence += 0.1
            confidence += abs(market["multi_tf_bias"]) * 0.1

    if not signal:
        return None

    return {"direction": signal, "strategy": "momentum_pullback",
            "confidence": min(confidence, 0.95)}


def trend_following_signal(df_m5: pd.DataFrame, market: dict, pair: str) -> Optional[dict]:
    """Exact replica of bot's trend_following_signal()"""
    if abs(market["trend_strength"]) < 0.3:
        return None
    if abs(market["multi_tf_bias"]) < 0.5:
        return None
    if df_m5.empty or len(df_m5) < 10:
        return None

    latest = df_m5.iloc[-1]
    if latest["rsi"] > 75 or latest["rsi"] < 25:
        return None

    signal = None
    confidence = 0.5

    if len(df_m5) >= 2:
        prev = df_m5.iloc[-2]
        macd_cross_up = latest["macd"] > latest["macd_signal"] and prev["macd"] <= prev["macd_signal"]
        macd_cross_down = latest["macd"] < latest["macd_signal"] and prev["macd"] >= prev["macd_signal"]
        momentum_up = latest["momentum"] > 0 and latest["rsi"] > 45
        momentum_down = latest["momentum"] < 0 and latest["rsi"] < 55

        if market["trend_direction"] == "bullish" and market["multi_tf_bias"] > 0:
            if macd_cross_up or momentum_up:
                signal = "buy"
                confidence += 0.3
                if market["order_flow"] > 0:
                    confidence += 0.1
                confidence += market["multi_tf_bias"] * 0.1

        elif market["trend_direction"] == "bearish" and market["multi_tf_bias"] < 0:
            if macd_cross_down or momentum_down:
                signal = "sell"
                confidence += 0.3
                if market["order_flow"] < 0:
                    confidence += 0.1
                confidence += abs(market["multi_tf_bias"]) * 0.1

    if not signal:
        return None

    return {"direction": signal, "strategy": "trend_following",
            "confidence": min(confidence, 0.95)}


def volatility_breakout_signal(df_m5: pd.DataFrame, market: dict, pair: str) -> Optional[dict]:
    """Exact replica of bot's volatility_breakout_signal()"""
    if df_m5.empty or len(df_m5) < 20:
        return None

    latest = df_m5.iloc[-1]
    recent_atr = df_m5["atr"].iloc[-5:].mean()
    longer_atr = df_m5["atr"].iloc[-20:].mean()

    if pd.isna(recent_atr) or pd.isna(longer_atr) or recent_atr <= longer_atr * 1.15:
        return None

    signal = None
    confidence = 0.5

    if pd.notna(latest.get("bb_upper")) and latest["close"] > latest["bb_upper"]:
        signal = "buy"
        confidence += 0.2
        if 60 < latest["rsi"] < 80:
            confidence += 0.1
        if market["multi_tf_bias"] > 0:
            confidence += 0.1
    elif pd.notna(latest.get("bb_lower")) and latest["close"] < latest["bb_lower"]:
        signal = "sell"
        confidence += 0.2
        if 20 < latest["rsi"] < 40:
            confidence += 0.1
        if market["multi_tf_bias"] < 0:
            confidence += 0.1

    if not signal:
        return None

    vr = latest.get("volume_ratio", 1.0)
    if pd.notna(vr) and vr > 1.5:
        confidence += 0.1

    if (signal == "buy" and market["order_flow"] > 0) or \
       (signal == "sell" and market["order_flow"] < 0):
        confidence += 0.1

    if confidence < CONFIDENCE_THRESHOLD:
        return None

    return {"direction": signal, "strategy": "volatility_breakout",
            "confidence": min(confidence, 0.95)}


# ─────────────────────────────────────────────────────────
# Signal orchestrator (exact replica of bot's generate_signals)
# ─────────────────────────────────────────────────────────

def generate_signal(df_m5: pd.DataFrame, market: dict, pair: str) -> Optional[dict]:
    """
    Run all three active strategies, pick the best signal above confidence threshold.
    Exact replica of the bot's generate_signals() minus API/logging/dedup.
    """
    # Pre-checks (from generate_signals)
    if not market["tradeable"]:
        return None
    if abs(market["multi_tf_bias"]) < 0.5:
        return None
    if market["hurst"] < HURST_THRESHOLD:
        return None

    signals = []
    for strategy_fn in [momentum_pullback_signal, trend_following_signal, volatility_breakout_signal]:
        sig = strategy_fn(df_m5, market, pair)
        if sig and sig["confidence"] >= CONFIDENCE_THRESHOLD:
            # R/R for 40/60 is always 40/60 = 0.667, which passes MIN_RISK_REWARD of 0.5
            sig["risk_reward"] = TP_PIPS / SL_PIPS
            signals.append(sig)

    if not signals:
        return None

    # Pick best by confidence × R/R (position_multiplier = 1.0 in backtest)
    best = max(signals, key=lambda s: s["confidence"] * s["risk_reward"])
    return best


# ─────────────────────────────────────────────────────────
# Trade simulation using 5s data (V2: no forward limit)
# ─────────────────────────────────────────────────────────

def simulate_trade_5s(direction: str, entry_idx: int, entry_price: float,
                      bid_high: np.ndarray, bid_low: np.ndarray,
                      ask_high: np.ndarray, ask_low: np.ndarray,
                      pip: float, n_total: int) -> Optional[Tuple[str, int]]:
    """
    Simulate a single trade from entry_idx forward through ALL remaining 5s data.
    No artificial forward limit — trade resolves when TP or SL is hit.
    Returns ('TP', exit_5s_idx), ('SL', exit_5s_idx), or None (unresolved at end of data).
    """
    max_fwd = n_total - entry_idx
    if max_fwd < 2:
        return None

    tp_dist = TP_PIPS * pip
    sl_dist = SL_PIPS * pip

    if direction == "buy":
        tp_level = entry_price + tp_dist
        sl_level = entry_price - sl_dist
        fwd_high = bid_high[entry_idx:entry_idx + max_fwd]
        fwd_low = bid_low[entry_idx:entry_idx + max_fwd]
    else:
        tp_level = entry_price - tp_dist
        sl_level = entry_price + sl_dist
        fwd_high = ask_high[entry_idx:entry_idx + max_fwd]  # worst for short
        fwd_low = ask_low[entry_idx:entry_idx + max_fwd]    # best for short

    if direction == "buy":
        tp_mask = fwd_high >= tp_level
        sl_mask = fwd_low <= sl_level
    else:
        tp_mask = fwd_low <= tp_level
        sl_mask = fwd_high >= sl_level

    tp_hit = tp_mask.any()
    sl_hit = sl_mask.any()

    if not tp_hit and not sl_hit:
        return None  # Neither hit — trade still open at end of data

    tp_idx = int(np.argmax(tp_mask)) if tp_hit else max_fwd
    sl_idx = int(np.argmax(sl_mask)) if sl_hit else max_fwd

    if tp_idx <= sl_idx and tp_hit:
        return ("TP", entry_idx + tp_idx)
    elif sl_hit:
        return ("SL", entry_idx + sl_idx)
    return None


# ─────────────────────────────────────────────────────────
# Main processing loop (V2: concurrent tracking + slippage)
# ─────────────────────────────────────────────────────────

def process_pair(pair: str, df_5s: pd.DataFrame, slippage_pips: float = 0.3) -> Tuple[List[dict], dict]:
    """
    Process one pair: resample, compute indicators on all timeframes,
    evaluate signals at each M5 bar, simulate trades with:
      - 1 concurrent trade max per pair (matching bot)
      - Adverse slippage on entry
      - No forward candle limit
    Returns (results_list, stats_dict).
    """
    pip = pip_val(pair)
    slippage_dist = slippage_pips * pip
    results = []

    # ─── Resample to all timeframes ───
    print(f"    Resampling to M1/M5/M15/H1/H4...", end="", flush=True)
    t0 = time_mod.time()

    df_m1_full = resample_ohlcv(df_5s, "1min")
    df_m5_full = resample_ohlcv(df_5s, "5min")
    df_m15_full = resample_ohlcv(df_5s, "15min")
    df_h1_full = resample_ohlcv(df_5s, "1h")
    df_h4_full = resample_ohlcv(df_5s, "4h")

    print(f" done ({time_mod.time()-t0:.1f}s)")

    # ─── Add indicators to all timeframes ───
    print(f"    Computing indicators on all timeframes...", end="", flush=True)
    t0 = time_mod.time()

    df_m1_full = add_indicators(df_m1_full)
    df_m5_full = add_indicators(df_m5_full)
    df_m15_full = add_indicators(df_m15_full)
    df_h1_full = add_indicators(df_h1_full)
    df_h4_full = add_indicators(df_h4_full)

    print(f" done ({time_mod.time()-t0:.1f}s)")

    # ─── Prepare 5s arrays for fast simulation ───
    bid_high_5s = df_5s["bid_high"].values.astype(np.float64)
    bid_low_5s = df_5s["bid_low"].values.astype(np.float64)
    ask_high_5s = df_5s["ask_high"].values.astype(np.float64)
    ask_low_5s = df_5s["ask_low"].values.astype(np.float64)
    ask_close_5s = df_5s["ask_close"].values.astype(np.float64)
    bid_close_5s = df_5s["bid_close"].values.astype(np.float64)
    times_5s = df_5s.index
    n_5s = len(df_5s)

    # Build a time → 5s index lookup for fast alignment
    time_to_5s_idx = pd.Series(np.arange(n_5s), index=times_5s)

    # ─── Pre-compute Hurst at each H1 bar ───
    print(f"    Pre-computing Hurst exponents...", end="", flush=True)
    t0 = time_mod.time()
    h1_closes = df_h1_full["close"].values
    hurst_by_h1_idx = {}
    for i in range(HURST_WINDOW, len(h1_closes)):
        hurst_by_h1_idx[i] = compute_hurst(h1_closes[:i+1])
    print(f" done ({time_mod.time()-t0:.1f}s)")

    # ─── Evaluate signal at each M5 bar ───
    m5_times = df_m5_full.index
    n_m5 = len(m5_times)
    print(f"    Evaluating {n_m5:,} M5 bars (1 trade/pair, slippage={slippage_pips}p)...",
          end="", flush=True)
    t0 = time_mod.time()

    signals_found = 0
    trades_simulated = 0
    trades_skipped_concurrent = 0
    trades_unresolved = 0
    last_print = t0

    # Track concurrent trades: 1 max per pair (matching bot's behavior)
    open_trade_exit_5s_idx = -1

    for i in range(max(50, MACD_SLOW + MACD_SIGNAL), n_m5):
        m5_time = m5_times[i]

        # Progress
        now = time_mod.time()
        if now - last_print > 10:
            elapsed = now - t0
            pct = i / n_m5 * 100
            rate = i / max(elapsed, 0.001)
            eta = (n_m5 - i) / max(rate, 1)
            print(f"\r    Evaluating {n_m5:,} M5 bars (1 trade/pair, slippage={slippage_pips}p)... "
                  f"{pct:.0f}% ({signals_found} sig, {trades_simulated} trades, "
                  f"{trades_skipped_concurrent} concurrent-skip, ETA {eta:.0f}s)",
                  end="", flush=True)
            last_print = now

        # Skip weekends
        if m5_time.weekday() >= 5:
            continue

        # ─── Build market condition dict (replica of get_market_condition) ───

        # Session quality
        utc_hour = m5_time.hour
        session, session_quality = get_session_quality(utc_hour, m5_time.weekday())
        if session_quality <= 0.5:
            continue

        # M5 slice (last 200 bars up to current)
        df_m5 = df_m5_full.iloc[max(0, i-199):i+1]
        if len(df_m5) < 50:
            continue

        latest = df_m5.iloc[-1]

        # ATR check
        atr = latest.get("atr")
        if pd.isna(atr) or atr == 0:
            continue
        atr_pips = atr * (100 if "JPY" in pair else 10000)
        if not (MIN_ATR_PIPS <= atr_pips <= MAX_ATR_PIPS):
            continue

        # Multi-timeframe bias
        m1_loc = df_m1_full.index.searchsorted(m5_time, side="right")
        m15_loc = df_m15_full.index.searchsorted(m5_time, side="right")
        h1_loc = df_h1_full.index.searchsorted(m5_time, side="right")
        h4_loc = df_h4_full.index.searchsorted(m5_time, side="right")

        tf_frames = {
            "M1": df_m1_full.iloc[max(0, m1_loc-200):m1_loc],
            "M5": df_m5,
            "M15": df_m15_full.iloc[max(0, m15_loc-200):m15_loc],
            "H1": df_h1_full.iloc[max(0, h1_loc-200):h1_loc],
            "H4": df_h4_full.iloc[max(0, h4_loc-200):h4_loc],
        }

        mtf_bias = get_multi_tf_bias(tf_frames)

        # Trend strength
        trend_strength = calculate_trend_strength(df_m5)
        trend_dir = "bullish" if trend_strength > 0.2 else "bearish" if trend_strength < -0.2 else "neutral"

        # Order flow (M1 last 60 bars)
        df_m1_slice = tf_frames["M1"]
        order_flow = get_order_flow_bias(df_m1_slice)

        # Hurst (use pre-computed from nearest H1 bar)
        hurst = hurst_by_h1_idx.get(h1_loc - 1, 0.5) if h1_loc > 0 else 0.5

        market = {
            "tradeable": True,
            "session": session,
            "session_quality": session_quality,
            "multi_tf_bias": mtf_bias,
            "trend_strength": trend_strength,
            "trend_direction": trend_dir,
            "order_flow": order_flow,
            "hurst": hurst,
        }

        # ─── Generate signal ───
        sig = generate_signal(df_m5, market, pair)
        if sig is None:
            continue

        signals_found += 1

        # ─── Find entry point in 5s data ───
        entry_5s_idx = time_to_5s_idx.index.searchsorted(m5_time, side="left")
        if entry_5s_idx >= n_5s - 2:
            continue

        # ─── Check concurrent trade (1 max per pair) ───
        if entry_5s_idx <= open_trade_exit_5s_idx:
            trades_skipped_concurrent += 1
            continue

        # ─── Entry price with adverse slippage ───
        if sig["direction"] == "buy":
            entry_price = ask_close_5s[entry_5s_idx] + slippage_dist
        else:
            entry_price = bid_close_5s[entry_5s_idx] - slippage_dist

        # ─── Simulate trade (no forward limit) ───
        result = simulate_trade_5s(
            sig["direction"], entry_5s_idx, entry_price,
            bid_high_5s, bid_low_5s, ask_high_5s, ask_low_5s,
            pip, n_5s
        )

        if result is None:
            # Trade unresolved at end of data — pair locked for remaining data
            open_trade_exit_5s_idx = n_5s
            trades_unresolved += 1
            continue

        outcome, exit_5s_idx = result
        open_trade_exit_5s_idx = exit_5s_idx
        trades_simulated += 1

        # Record result with window info
        half = 0 if m5_time.minute < 30 else 1
        results.append({
            "pair": pair,
            "time": m5_time,
            "date": m5_time.date(),
            "window": (m5_time.hour, half),
            "direction": sig["direction"],
            "strategy": sig["strategy"],
            "confidence": sig["confidence"],
            "result": outcome,
            "entry_5s_idx": entry_5s_idx,
            "exit_5s_idx": exit_5s_idx,
            "session": session,
            "session_quality": session_quality,
            "hurst": hurst,
            "mtf_bias": mtf_bias,
        })

    elapsed = time_mod.time() - t0
    print(f"\r    Evaluated {n_m5:,} M5 bars in {elapsed:.0f}s — "
          f"{signals_found} signals, {trades_simulated} trades, "
          f"{trades_skipped_concurrent} skipped (concurrent), "
          f"{trades_unresolved} unresolved")

    pair_info = {
        "signals": signals_found,
        "trades": trades_simulated,
        "skipped_concurrent": trades_skipped_concurrent,
        "unresolved": trades_unresolved,
    }

    return results, pair_info


# ─────────────────────────────────────────────────────────
# Walk-forward validation
# ─────────────────────────────────────────────────────────

def walk_forward_validate(trades: List[dict], train_months: int = 9,
                          test_months: int = 3, min_fold_trades: int = 15) -> List[dict]:
    """
    Walk-forward validation for a list of trades (one pair, one window).
    Uses expanding window: anchored start, expanding training set.
    Test periods are non-overlapping, sequential.

    Returns list of fold dicts with train/test stats.
    """
    if not trades:
        return []

    # Get all unique (year, month) tuples, sorted
    all_months = sorted(set((t['date'].year, t['date'].month) for t in trades))

    if len(all_months) < train_months + test_months:
        return []

    # Index trades by month for fast lookup
    trades_by_month = defaultdict(list)
    for t in trades:
        trades_by_month[(t['date'].year, t['date'].month)].append(t)

    folds = []
    test_start_idx = train_months

    while test_start_idx + test_months <= len(all_months):
        train_month_keys = set(all_months[:test_start_idx])
        test_month_keys = all_months[test_start_idx:test_start_idx + test_months]
        test_month_set = set(test_month_keys)

        train_trades = []
        for mk in train_month_keys:
            train_trades.extend(trades_by_month.get(mk, []))

        test_trades = []
        for mk in test_month_set:
            test_trades.extend(trades_by_month.get(mk, []))

        train_n = len(train_trades)
        test_n = len(test_trades)

        train_tp = sum(1 for t in train_trades if t['result'] == 'TP')
        test_tp = sum(1 for t in test_trades if t['result'] == 'TP')

        train_wr = train_tp / train_n if train_n > 0 else 0
        test_wr = test_tp / test_n if test_n > 0 else 0

        train_ev = train_wr * TP_PIPS - (1 - train_wr) * SL_PIPS if train_n > 0 else 0
        test_ev = test_wr * TP_PIPS - (1 - test_wr) * SL_PIPS if test_n > 0 else 0

        # Fold is counted if test has enough trades
        counted = test_n >= min_fold_trades

        # Fold passes if BOTH train and test beat breakeven WR
        passed = counted and train_wr > BREAKEVEN_WR and test_wr > BREAKEVEN_WR

        # Label for this test period
        y0, m0 = test_month_keys[0]
        y1, m1 = test_month_keys[-1]
        label = f"{y0}-{m0:02d}→{y1}-{m1:02d}"

        folds.append({
            'label': label,
            'train_n': train_n, 'train_tp': train_tp,
            'train_wr': train_wr, 'train_ev': train_ev,
            'test_n': test_n, 'test_tp': test_tp,
            'test_wr': test_wr, 'test_ev': test_ev,
            'counted': counted, 'passed': passed,
        })

        test_start_idx += test_months

    # Also handle partial last fold if remaining months >= 1
    if test_start_idx < len(all_months):
        remaining = all_months[test_start_idx:]
        if len(remaining) >= 1:
            train_month_keys = set(all_months[:test_start_idx])
            test_month_set = set(remaining)

            train_trades = []
            for mk in train_month_keys:
                train_trades.extend(trades_by_month.get(mk, []))
            test_trades = []
            for mk in test_month_set:
                test_trades.extend(trades_by_month.get(mk, []))

            train_n = len(train_trades)
            test_n = len(test_trades)
            train_tp = sum(1 for t in train_trades if t['result'] == 'TP')
            test_tp = sum(1 for t in test_trades if t['result'] == 'TP')
            train_wr = train_tp / train_n if train_n > 0 else 0
            test_wr = test_tp / test_n if test_n > 0 else 0
            train_ev = train_wr * TP_PIPS - (1 - train_wr) * SL_PIPS if train_n > 0 else 0
            test_ev = test_wr * TP_PIPS - (1 - test_wr) * SL_PIPS if test_n > 0 else 0

            counted = test_n >= min_fold_trades
            passed = counted and train_wr > BREAKEVEN_WR and test_wr > BREAKEVEN_WR

            y0, m0 = remaining[0]
            y1, m1 = remaining[-1]
            label = f"{y0}-{m0:02d}→{y1}-{m1:02d}"

            folds.append({
                'label': label,
                'train_n': train_n, 'train_tp': train_tp,
                'train_wr': train_wr, 'train_ev': train_ev,
                'test_n': test_n, 'test_tp': test_tp,
                'test_wr': test_wr, 'test_ev': test_ev,
                'counted': counted, 'passed': passed,
            })

    return folds


# ─────────────────────────────────────────────────────────
# Drawdown & streak analysis
# ─────────────────────────────────────────────────────────

def compute_drawdown(trades: List[dict]) -> dict:
    """
    Compute drawdown and streak stats from chronologically-ordered trades.
    Each trade is +TP_PIPS (win) or -SL_PIPS (loss).
    """
    if not trades:
        return {'max_drawdown_pips': 0, 'max_consecutive_losses': 0,
                'profit_factor': 0, 'total_pnl': 0, 'n_trades': 0}

    results = [t['result'] for t in trades]
    pnl = np.array([TP_PIPS if r == 'TP' else -SL_PIPS for r in results], dtype=np.float64)
    cum_pnl = np.cumsum(pnl)

    # Max drawdown
    peak = np.maximum.accumulate(cum_pnl)
    drawdown = peak - cum_pnl
    max_dd = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

    # Max consecutive losses
    max_consec_loss = 0
    current_consec = 0
    for r in results:
        if r == 'SL':
            current_consec += 1
            max_consec_loss = max(max_consec_loss, current_consec)
        else:
            current_consec = 0

    # Max consecutive wins
    max_consec_win = 0
    current_consec = 0
    for r in results:
        if r == 'TP':
            current_consec += 1
            max_consec_win = max(max_consec_win, current_consec)
        else:
            current_consec = 0

    # Profit factor
    total_profit = sum(TP_PIPS for r in results if r == 'TP')
    total_loss = sum(SL_PIPS for r in results if r == 'SL')
    profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')

    # Worst month (by month PnL)
    monthly_pnl = defaultdict(float)
    for t in trades:
        key = (t['date'].year, t['date'].month)
        monthly_pnl[key] += TP_PIPS if t['result'] == 'TP' else -SL_PIPS
    worst_month_pnl = min(monthly_pnl.values()) if monthly_pnl else 0
    worst_month_key = min(monthly_pnl, key=monthly_pnl.get) if monthly_pnl else None
    best_month_pnl = max(monthly_pnl.values()) if monthly_pnl else 0

    return {
        'max_drawdown_pips': max_dd,
        'max_consecutive_losses': max_consec_loss,
        'max_consecutive_wins': max_consec_win,
        'profit_factor': profit_factor,
        'total_pnl': float(cum_pnl[-1]) if len(cum_pnl) > 0 else 0.0,
        'n_trades': len(results),
        'worst_month_pnl': worst_month_pnl,
        'worst_month': f"{worst_month_key[0]}-{worst_month_key[1]:02d}" if worst_month_key else "N/A",
        'best_month_pnl': best_month_pnl,
        'n_months': len(monthly_pnl),
    }


# ─────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────

def print_strategy_breakdown(results: List[dict]):
    """Show how each strategy performs overall."""
    print(f"\n{'='*120}")
    print(f"  STRATEGY PERFORMANCE (with concurrent trade limit + slippage)")
    print(f"{'='*120}")

    by_strat = defaultdict(lambda: {"tp": 0, "sl": 0})
    for r in results:
        if r["result"] == "TP":
            by_strat[r["strategy"]]["tp"] += 1
        else:
            by_strat[r["strategy"]]["sl"] += 1

    print(f"  {'Strategy':<25} {'WR%':>6} {'EV(p)':>7} {'Trades':>7} {'TP':>6} {'SL':>6} {'PF':>5}")
    print(f"  {'-'*70}")
    for strat, d in sorted(by_strat.items(), key=lambda x: -(x[1]["tp"]/(x[1]["tp"]+x[1]["sl"]) if x[1]["tp"]+x[1]["sl"]>0 else 0)):
        total = d["tp"] + d["sl"]
        wr = d["tp"] / total if total > 0 else 0
        ev = wr * TP_PIPS - (1 - wr) * SL_PIPS
        pf = (d["tp"] * TP_PIPS) / (d["sl"] * SL_PIPS) if d["sl"] > 0 else float('inf')
        print(f"  {strat:<25} {wr*100:>5.1f}% {ev:>+6.1f}p {total:>7} {d['tp']:>6} {d['sl']:>6} {pf:>5.2f}")

    total_tp = sum(d["tp"] for d in by_strat.values())
    total_sl = sum(d["sl"] for d in by_strat.values())
    total = total_tp + total_sl
    wr = total_tp / total if total > 0 else 0
    ev = wr * TP_PIPS - (1 - wr) * SL_PIPS
    pf = (total_tp * TP_PIPS) / (total_sl * SL_PIPS) if total_sl > 0 else float('inf')
    print(f"  {'-'*70}")
    print(f"  {'TOTAL':<25} {wr*100:>5.1f}% {ev:>+6.1f}p {total:>7} {total_tp:>6} {total_sl:>6} {pf:>5.2f}")


def print_pair_window_ranking(results: List[dict], pair: str):
    """Print window ranking for a single pair."""
    if not results:
        return

    by_window = defaultdict(lambda: {"tp": 0, "sl": 0,
                                      "strategies": defaultdict(lambda: {"tp": 0, "sl": 0})})

    for r in results:
        w = r["window"]
        d = by_window[w]
        if r["result"] == "TP":
            d["tp"] += 1
            d["strategies"][r["strategy"]]["tp"] += 1
        else:
            d["sl"] += 1
            d["strategies"][r["strategy"]]["sl"] += 1

    stats = []
    for window, d in by_window.items():
        total = d["tp"] + d["sl"]
        wr = d["tp"] / total if total > 0 else 0
        ev = wr * TP_PIPS - (1 - wr) * SL_PIPS

        # Strategy breakdown string
        strat_parts = []
        for sname in ["volatility_breakout", "momentum_pullback", "trend_following"]:
            sd = d["strategies"].get(sname, {"tp": 0, "sl": 0})
            sn = sd["tp"] + sd["sl"]
            if sn > 0:
                swr = sd["tp"] / sn
                sev = swr * TP_PIPS - (1 - swr) * SL_PIPS
                abbrev = {"volatility_breakout": "VB", "momentum_pullback": "MP",
                          "trend_following": "TF"}[sname]
                strat_parts.append(f"{abbrev}:{sn}@{sev:+.0f}p")

        stats.append({
            "window": window, "label": window_label(*window),
            "wr": wr, "ev": ev, "n": total, "tp": d["tp"], "sl": d["sl"],
            "strat_str": " | ".join(strat_parts),
        })

    stats.sort(key=lambda s: s["ev"], reverse=True)

    print(f"\n{'='*120}")
    print(f"  {pair} — Per-Pair Window Rankings")
    print(f"  Breakeven WR for TP+{TP_PIPS}/SL-{SL_PIPS}: {BREAKEVEN_WR*100:.0f}%")
    print(f"{'='*120}")
    print(f"  {'Window':<14} {'WR%':>6} {'EV(p)':>7} {'Trades':>7} {'TP':>5} {'SL':>5} "
          f"{'Status':>12}  Strategy Breakdown")
    print(f"  {'-'*110}")

    for s in stats:
        if s["ev"] > 0 and s["n"] >= MIN_SAMPLE_SIZE:
            status = "PROFITABLE"
        elif s["ev"] > 0 and s["n"] >= 30:
            status = "positive"
        elif s["ev"] > -5:
            status = "marginal"
        else:
            status = "avoid"

        marker = " ***" if status == "PROFITABLE" else ""
        print(f"  {s['label']:<14} {s['wr']*100:>5.1f}% {s['ev']:>+6.1f}p {s['n']:>7} "
              f"{s['tp']:>5} {s['sl']:>5} "
              f"{status:>12}{marker}  {s['strat_str']}")


def validate_pair_windows(pair_results: List[dict], pair: str,
                          train_months: int, test_months: int,
                          min_fold_trades: int, min_pass_rate: float,
                          min_total_n: int) -> List[dict]:
    """
    For one pair, walk-forward validate each window.
    Returns list of validated pair-window entries sorted by overall test EV.
    """
    # Group trades by window
    by_window = defaultdict(list)
    for r in pair_results:
        by_window[r['window']].append(r)

    validated = []
    considered = []

    for window in sorted(by_window.keys()):
        trades = sorted(by_window[window], key=lambda t: t['time'])
        n = len(trades)
        tp = sum(1 for t in trades if t['result'] == 'TP')
        wr = tp / n if n > 0 else 0
        ev = wr * TP_PIPS - (1 - wr) * SL_PIPS

        # Skip windows that don't meet basic full-dataset criteria
        if n < min_total_n or wr <= BREAKEVEN_WR:
            continue

        considered.append((window, n, wr, ev))

        # Walk-forward validate
        folds = walk_forward_validate(trades, train_months, test_months, min_fold_trades)

        counted_folds = [f for f in folds if f['counted']]
        passed_folds = [f for f in counted_folds if f['passed']]

        if len(counted_folds) < MIN_FOLDS_REQUIRED:
            continue

        pass_rate = len(passed_folds) / len(counted_folds)
        if pass_rate < min_pass_rate:
            continue

        # Overall test stats (sum across all counted folds)
        total_test_tp = sum(f['test_tp'] for f in counted_folds)
        total_test_n = sum(f['test_n'] for f in counted_folds)
        overall_test_wr = total_test_tp / total_test_n if total_test_n > 0 else 0
        overall_test_ev = overall_test_wr * TP_PIPS - (1 - overall_test_wr) * SL_PIPS

        # Must also be profitable across all test folds combined
        if overall_test_wr <= BREAKEVEN_WR:
            continue

        # Compute drawdown
        dd = compute_drawdown(trades)

        validated.append({
            'pair': pair,
            'window': window,
            'label': window_label(*window),
            'wr': wr, 'ev': ev, 'n': n, 'tp': tp, 'sl': n - tp,
            'folds': folds,
            'counted_folds': len(counted_folds),
            'passed_folds': len(passed_folds),
            'pass_rate': pass_rate,
            'overall_test_wr': overall_test_wr,
            'overall_test_ev': overall_test_ev,
            'overall_test_n': total_test_n,
            'drawdown': dd,
        })

    # Sort by overall test EV (out-of-sample performance)
    validated.sort(key=lambda v: v['overall_test_ev'], reverse=True)
    return validated


def print_walk_forward_detail(validated: List[dict], pair: str):
    """Print detailed walk-forward results for one pair."""
    print(f"\n{'='*120}")
    print(f"  {pair} — WALK-FORWARD VALIDATION")
    print(f"  Expanding window: train from start, {WF_TEST_MONTHS}-month non-overlapping test periods")
    print(f"  Pass criteria: BOTH train AND test WR > {BREAKEVEN_WR*100:.0f}% per fold, "
          f">={MIN_PASS_RATE*100:.0f}% of folds pass, overall test WR > {BREAKEVEN_WR*100:.0f}%")
    print(f"{'='*120}")

    if not validated:
        print(f"  No windows passed walk-forward validation for {pair}.")
        return

    for v in validated:
        print(f"\n  {v['label']}  |  Full: WR={v['wr']*100:.1f}% EV={v['ev']:+.1f}p N={v['n']}  |  "
              f"Test: WR={v['overall_test_wr']*100:.1f}% EV={v['overall_test_ev']:+.1f}p N={v['overall_test_n']}  |  "
              f"Folds: {v['passed_folds']}/{v['counted_folds']} passed ({v['pass_rate']*100:.0f}%)")
        print(f"  {'Fold':<16} {'Train':>7} {'Trn WR':>7} {'Trn EV':>8} │ "
              f"{'Test':>6} {'Tst WR':>7} {'Tst EV':>8} │ {'Result':>8}")
        print(f"  {'-'*90}")

        for f in v['folds']:
            if f['counted']:
                result_str = "PASS ✓" if f['passed'] else "FAIL ✗"
            else:
                result_str = f"skip (n={f['test_n']})"

            print(f"  {f['label']:<16} {f['train_n']:>7} {f['train_wr']*100:>6.1f}% {f['train_ev']:>+7.1f}p │ "
                  f"{f['test_n']:>6} {f['test_wr']*100:>6.1f}% {f['test_ev']:>+7.1f}p │ {result_str:>8}")

        # Drawdown summary
        dd = v['drawdown']
        print(f"  Drawdown: max={dd['max_drawdown_pips']:.0f}p | "
              f"max consec losses={dd['max_consecutive_losses']} | "
              f"profit factor={dd['profit_factor']:.2f} | "
              f"worst month={dd['worst_month']} ({dd['worst_month_pnl']:+.0f}p)")


def print_recommended_config(all_validated: Dict[str, List[dict]]):
    """Print the final recommended PAIR_WINDOWS config."""
    print(f"\n{'='*120}")
    print(f"  RECOMMENDED PAIR_WINDOWS CONFIG")
    print(f"  Walk-forward validated, per-pair per-window, with drawdown analysis")
    print(f"{'='*120}")

    # Count total validated
    total_validated = sum(len(v) for v in all_validated.values())
    if total_validated == 0:
        print(f"\n  No pair-windows passed walk-forward validation.")
        print(f"  The bot's signal logic may not have a robust time-of-day edge with these filters.")
        return

    # Print config dict
    print(f"\n  # Paste into bot's Config class:")
    print(f"  USE_TIME_WINDOWS = True")
    print(f"  USE_PAIR_WINDOWS = True")
    print(f"  PAIR_WINDOWS = {{")

    for pair in sorted(all_validated.keys()):
        windows = all_validated[pair]
        if not windows:
            continue
        window_strs = []
        for v in windows:
            h, hh = v['window']
            window_strs.append(f"({h}, {hh})")
        print(f"      \"{pair}\": [{', '.join(window_strs)}],")
    print(f"  }}")

    # Detailed summary table
    print(f"\n  {'Pair':<10} {'Window':<14} {'Full WR':>7} {'Full EV':>8} {'N':>6} │ "
          f"{'Test WR':>7} {'Test EV':>8} {'Folds':>7} │ "
          f"{'MaxDD':>7} {'MaxLoss':>8} {'PF':>5}")
    print(f"  {'-'*110}")

    total_n = 0
    total_test_n = 0

    for pair in sorted(all_validated.keys()):
        windows = all_validated[pair]
        for v in windows:
            dd = v['drawdown']
            print(f"  {pair:<10} {v['label']:<14} {v['wr']*100:>6.1f}% {v['ev']:>+7.1f}p {v['n']:>6} │ "
                  f"{v['overall_test_wr']*100:>6.1f}% {v['overall_test_ev']:>+7.1f}p "
                  f"{v['passed_folds']}/{v['counted_folds']:>3} │ "
                  f"{dd['max_drawdown_pips']:>6.0f}p {dd['max_consecutive_losses']:>7}L "
                  f"{dd['profit_factor']:>5.2f}")
            total_n += v['n']
            total_test_n += v['overall_test_n']

    print(f"\n  Total validated pair-windows: {total_validated}")
    print(f"  Total trades in validated windows: {total_n:,}")
    print(f"  Total out-of-sample test trades: {total_test_n:,}")

    # Global test stats
    all_test_tp = 0
    all_test_n = 0
    for pair in all_validated:
        for v in all_validated[pair]:
            counted = [f for f in v['folds'] if f['counted']]
            all_test_tp += sum(f['test_tp'] for f in counted)
            all_test_n += sum(f['test_n'] for f in counted)

    if all_test_n > 0:
        global_test_wr = all_test_tp / all_test_n
        global_test_ev = global_test_wr * TP_PIPS - (1 - global_test_wr) * SL_PIPS
        print(f"\n  GLOBAL OUT-OF-SAMPLE: WR={global_test_wr*100:.1f}%, EV={global_test_ev:+.1f}p, N={all_test_n:,}")

    # Cross-pair window summary (which windows are profitable for multiple pairs)
    print(f"\n  Cross-pair window coverage:")
    window_pairs = defaultdict(list)
    for pair in all_validated:
        for v in all_validated[pair]:
            window_pairs[v['window']].append((pair, v['overall_test_ev']))

    for w in sorted(window_pairs.keys()):
        pairs = window_pairs[w]
        pairs_str = ", ".join(f"{p}({ev:+.1f}p)" for p, ev in sorted(pairs, key=lambda x: -x[1]))
        print(f"    {window_label(*w)}: {len(pairs)} pairs — {pairs_str}")


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main():
    global MIN_SAMPLE_SIZE, WF_TRAIN_MONTHS, WF_TEST_MONTHS, MIN_FOLD_TRADES
    global MIN_FOLDS_REQUIRED, MIN_PASS_RATE, SLIPPAGE_DEFAULT

    parser = argparse.ArgumentParser(
        description="Window Optimizer V2 — per-pair per-window, walk-forward validated")
    parser.add_argument("--data-dir", required=True, help="Directory with parquet files")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--pairs", nargs="+", default=None, help="Pairs to test")
    parser.add_argument("--slippage", type=float, default=SLIPPAGE_DEFAULT,
                        help=f"Adverse slippage in pips (default: {SLIPPAGE_DEFAULT})")
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLE_SIZE,
                        help=f"Min trades for a pair-window (default: {MIN_SAMPLE_SIZE})")
    parser.add_argument("--wf-train", type=int, default=WF_TRAIN_MONTHS,
                        help=f"Walk-forward initial training months (default: {WF_TRAIN_MONTHS})")
    parser.add_argument("--wf-test", type=int, default=WF_TEST_MONTHS,
                        help=f"Walk-forward test period months (default: {WF_TEST_MONTHS})")
    parser.add_argument("--min-fold-trades", type=int, default=MIN_FOLD_TRADES,
                        help=f"Min trades per WF test fold (default: {MIN_FOLD_TRADES})")
    parser.add_argument("--min-pass-rate", type=float, default=MIN_PASS_RATE,
                        help=f"Min fraction of folds that must pass (default: {MIN_PASS_RATE})")
    args = parser.parse_args()

    MIN_SAMPLE_SIZE = args.min_samples
    WF_TRAIN_MONTHS = args.wf_train
    WF_TEST_MONTHS = args.wf_test
    MIN_FOLD_TRADES = args.min_fold_trades
    MIN_PASS_RATE = args.min_pass_rate
    SLIPPAGE_DEFAULT = args.slippage

    print("=" * 120)
    print("40/60 BOT — WINDOW OPTIMIZER V2 (per-pair per-window, walk-forward validated)")
    print("=" * 120)
    print(f"  Strategies: momentum_pullback, trend_following, volatility_breakout")
    print(f"  Filters: Hurst>{HURST_THRESHOLD}, MTF bias>0.5, ATR {MIN_ATR_PIPS}-{MAX_ATR_PIPS}p, session>0.5")
    print(f"  TP/SL: +{TP_PIPS}/-{SL_PIPS} pips | Confidence threshold: {CONFIDENCE_THRESHOLD}")
    print(f"  Breakeven WR: {BREAKEVEN_WR*100:.0f}%")
    print(f"  Concurrent trades: 1 per pair (matching bot)")
    print(f"  Adverse slippage: {args.slippage} pips on entry")
    print(f"  Trade forward limit: NONE (trades resolve fully)")
    print(f"  Walk-forward: {WF_TRAIN_MONTHS}m initial train, {WF_TEST_MONTHS}m test periods, "
          f"expanding window")
    print(f"  Validation: both train+test WR>{BREAKEVEN_WR*100:.0f}% per fold, "
          f">={MIN_PASS_RATE*100:.0f}% folds pass")
    print(f"  Min samples: {MIN_SAMPLE_SIZE} total, {MIN_FOLD_TRADES} per test fold")
    if args.start:
        print(f"  Date range: {args.start} to {args.end or 'latest'}")
    print("=" * 120)

    pairs = args.pairs or ALL_PAIRS
    available = []
    for p in pairs:
        path = find_parquet(args.data_dir, p)
        if path:
            available.append((p, path))
        else:
            print(f"  Skip {p}: no parquet found")

    print(f"\nPairs: {', '.join(p for p, _ in available)}")

    all_results = []
    pair_results_map = {}
    pair_info_map = {}

    for pair, path in available:
        print(f"\n{'─'*80}")
        print(f"  {pair}")
        print(f"{'─'*80}")

        print(f"    Loading 5s data...", end="", flush=True)
        t0 = time_mod.time()
        df_5s = load_5s(path, args.start, args.end)
        print(f" {len(df_5s):,} rows in {time_mod.time()-t0:.1f}s")

        results, pair_info = process_pair(pair, df_5s, slippage_pips=args.slippage)
        all_results.extend(results)
        pair_results_map[pair] = results
        pair_info_map[pair] = pair_info

        # Quick per-pair summary
        if results:
            tp = sum(1 for r in results if r["result"] == "TP")
            sl = sum(1 for r in results if r["result"] == "SL")
            total = tp + sl
            wr = tp / total if total > 0 else 0
            ev = wr * TP_PIPS - (1 - wr) * SL_PIPS
            print(f"    {pair} summary: {total} trades, WR={wr*100:.1f}%, EV={ev:+.1f}p")

        del df_5s  # Free memory

    if not all_results:
        print("\nNo trades generated. The bot's signal logic produced no signals in this data range.")
        return

    # ─── Processing summary ───
    print(f"\n{'='*120}")
    print(f"  PROCESSING SUMMARY")
    print(f"{'='*120}")
    for pair in [p for p, _ in available]:
        info = pair_info_map.get(pair, {})
        print(f"  {pair:<10}: {info.get('signals', 0):>6} signals, "
              f"{info.get('trades', 0):>6} trades, "
              f"{info.get('skipped_concurrent', 0):>6} skipped (concurrent), "
              f"{info.get('unresolved', 0):>4} unresolved")

    # ─── Strategy breakdown ───
    print_strategy_breakdown(all_results)

    # ─── Per-pair window rankings ───
    for pair, _ in available:
        results = pair_results_map.get(pair, [])
        if results:
            print_pair_window_ranking(results, pair)

    # ─── Walk-forward validation per pair ───
    all_validated = {}

    for pair, _ in available:
        results = pair_results_map.get(pair, [])
        if not results:
            all_validated[pair] = []
            continue

        validated = validate_pair_windows(
            results, pair,
            train_months=WF_TRAIN_MONTHS,
            test_months=WF_TEST_MONTHS,
            min_fold_trades=MIN_FOLD_TRADES,
            min_pass_rate=MIN_PASS_RATE,
            min_total_n=MIN_SAMPLE_SIZE,
        )
        all_validated[pair] = validated

        # Print walk-forward detail
        print_walk_forward_detail(validated, pair)

    # ─── Recommended config ───
    print_recommended_config(all_validated)

    print(f"\n{'='*120}")
    print("DONE")
    print(f"{'='*120}")


if __name__ == "__main__":
    main()
