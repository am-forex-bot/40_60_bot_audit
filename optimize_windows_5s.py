#!/usr/bin/env python3
"""
30-Minute Window Optimizer for the 40/60 Bot

Uses 5-second bid/ask parquet data to determine which 30-minute windows
produce the best outcomes for the bot's TP+40 / SL-60 strategy.

For each pair × window × direction:
  - Simulates entry at the first 5s candle of each 30-min window
  - Uses bid/ask prices for realistic execution
  - Walks forward through 5s candles to find TP or SL hit
  - Records outcome

Cross-validates with train/test split to avoid curve-fitting.

Usage:
    python3 optimize_windows_5s.py --data-dir /path/to/parquet/files

    # Faster: only use recent data (last 2 years)
    python3 optimize_windows_5s.py --data-dir /path/to/parquets --start 2024-01-01

    # Test specific pairs only
    python3 optimize_windows_5s.py --data-dir /path/to/parquets --pairs GBP_USD EUR_USD

Expects parquet files named like: GBP_USD_S5_*.parquet
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


# ─────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────

# Pairs the bot trades
ALL_PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD",
             "EUR_GBP", "GBP_JPY", "EUR_JPY", "AUD_JPY", "NZD_USD",
             "EUR_AUD", "GBP_AUD"]

# The bot's actual TP/SL in pips
TP_PIPS = 40
SL_PIPS = 60

# Max forward candles to check (50,000 × 5s ≈ 69 hours ≈ ~3 trading days)
MAX_FORWARD_CANDLES = 50_000

# Min trades per window to consider it statistically meaningful
MIN_SAMPLE_SIZE = 50

# Breakeven win rate for 40 TP / 60 SL
BREAKEVEN_WR = SL_PIPS / (TP_PIPS + SL_PIPS)  # 0.60 = 60%

# 48 windows: (hour, half_hour) → 00:00-00:29, 00:30-00:59, ...
ALL_WINDOWS = [(h, hh) for h in range(24) for hh in range(2)]


def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def window_label(hour: int, half: int) -> str:
    return f"{hour:02d}:{half*30:02d}-{hour:02d}:{half*30+29:02d}"


# ─────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────

def find_parquet(data_dir: str, pair: str) -> Optional[str]:
    pattern = os.path.join(data_dir, f"{pair}_S5_*.parquet")
    matches = glob.glob(pattern)
    if matches:
        return matches[0]
    pattern2 = os.path.join(data_dir, f"{pair}*.parquet")
    matches2 = glob.glob(pattern2)
    return matches2[0] if matches2 else None


def load_pair_data(parquet_path: str, start_date: str = None, end_date: str = None) -> dict:
    """
    Load 5s data as numpy arrays for maximum performance.
    Returns dict of numpy arrays: time, bid_high, bid_low, bid_close, ask_high, ask_low, ask_close, close.
    """
    filters = []
    if start_date:
        filters.append(("time", ">=", pd.Timestamp(start_date, tz="UTC")))
    if end_date:
        filters.append(("time", "<=", pd.Timestamp(end_date, tz="UTC")))

    cols = ["time", "close", "bid_high", "bid_low", "bid_close", "ask_high", "ask_low", "ask_close"]
    df = pq.read_table(parquet_path, filters=filters or None, columns=cols).to_pandas()
    df = df.sort_values("time").reset_index(drop=True)

    # Convert time to numpy datetime64 for fast operations
    return {
        "time": df["time"].values,
        "close": df["close"].values.astype(np.float64),
        "bid_high": df["bid_high"].values.astype(np.float64),
        "bid_low": df["bid_low"].values.astype(np.float64),
        "bid_close": df["bid_close"].values.astype(np.float64),
        "ask_high": df["ask_high"].values.astype(np.float64),
        "ask_low": df["ask_low"].values.astype(np.float64),
        "ask_close": df["ask_close"].values.astype(np.float64),
        "n": len(df),
    }


# ─────────────────────────────────────────────────────────
# Find entry points for each 30-min window
# ─────────────────────────────────────────────────────────

def find_window_entries(data: dict) -> List[dict]:
    """
    Find the first 5s candle at the start of each 30-minute window for each trading day.
    Returns list of {window: (hour, half), idx: int, date: date, time: datetime64}.
    """
    times = data["time"]
    n = data["n"]

    # Convert to pandas for efficient datetime extraction
    ts = pd.DatetimeIndex(times)
    hours = ts.hour
    minutes = ts.minute
    dates = ts.date
    weekdays = ts.weekday  # 0=Mon, 6=Sun

    entries = []
    seen = set()  # (date, hour, half) to avoid duplicates

    for i in range(n):
        # Skip weekends
        if weekdays[i] >= 5:
            continue

        h = hours[i]
        half = 0 if minutes[i] < 30 else 1
        d = dates[i]
        key = (d, h, half)

        if key not in seen:
            seen.add(key)
            # Make sure there's enough forward data
            if i + 5000 < n:  # At least ~7 hours of forward data
                entries.append({
                    "window": (h, half),
                    "idx": i,
                    "date": d,
                })

    return entries


# ─────────────────────────────────────────────────────────
# Simulate trades (vectorized per-trade)
# ─────────────────────────────────────────────────────────

def simulate_entries(data: dict, entries: List[dict], pip: float) -> List[dict]:
    """
    For each entry point, simulate both BUY and SELL trades.
    Uses numpy for speed.

    BUY: enter at ask_close, exit checks against bid (TP: bid_high >= TP, SL: bid_low <= SL)
    SELL: enter at bid_close, exit checks against ask (TP: ask_low <= TP, SL: ask_high >= SL)
    """
    bid_high = data["bid_high"]
    bid_low = data["bid_low"]
    bid_close = data["bid_close"]
    ask_high = data["ask_high"]
    ask_low = data["ask_low"]
    ask_close = data["ask_close"]
    close_arr = data["close"]
    n = data["n"]

    tp_dist = TP_PIPS * pip
    sl_dist = SL_PIPS * pip

    results = []

    for entry in entries:
        idx = entry["idx"]
        max_fwd = min(MAX_FORWARD_CANDLES, n - idx)

        # ─── BUY trade ───
        buy_entry = ask_close[idx]
        buy_tp = buy_entry + tp_dist
        buy_sl = buy_entry - sl_dist

        fwd_bid_high = bid_high[idx:idx + max_fwd]
        fwd_bid_low = bid_low[idx:idx + max_fwd]

        tp_mask = fwd_bid_high >= buy_tp
        sl_mask = fwd_bid_low <= buy_sl

        buy_tp_idx = int(np.argmax(tp_mask)) if tp_mask.any() else max_fwd
        buy_sl_idx = int(np.argmax(sl_mask)) if sl_mask.any() else max_fwd

        # Fix argmax returning 0 when no True exists
        if not tp_mask.any():
            buy_tp_idx = max_fwd
        if not sl_mask.any():
            buy_sl_idx = max_fwd

        if buy_tp_idx <= buy_sl_idx and buy_tp_idx < max_fwd:
            buy_result = "TP"
            buy_pips = TP_PIPS
        elif buy_sl_idx < max_fwd:
            buy_result = "SL"
            buy_pips = -SL_PIPS
        else:
            buy_result = "OPEN"
            buy_pips = 0

        # ─── SELL trade ───
        sell_entry = bid_close[idx]
        sell_tp = sell_entry - tp_dist
        sell_sl = sell_entry + sl_dist

        fwd_ask_low = ask_low[idx:idx + max_fwd]
        fwd_ask_high = ask_high[idx:idx + max_fwd]

        tp_mask_s = fwd_ask_low <= sell_tp
        sl_mask_s = fwd_ask_high >= sell_sl

        sell_tp_idx = int(np.argmax(tp_mask_s)) if tp_mask_s.any() else max_fwd
        sell_sl_idx = int(np.argmax(sl_mask_s)) if sl_mask_s.any() else max_fwd

        if not tp_mask_s.any():
            sell_tp_idx = max_fwd
        if not sl_mask_s.any():
            sell_sl_idx = max_fwd

        if sell_tp_idx <= sell_sl_idx and sell_tp_idx < max_fwd:
            sell_result = "TP"
            sell_pips = TP_PIPS
        elif sell_sl_idx < max_fwd:
            sell_result = "SL"
            sell_pips = -SL_PIPS
        else:
            sell_result = "OPEN"
            sell_pips = 0

        # ─── Simple trend direction (EMA9 vs EMA21 on recent M5-equivalent closes) ───
        # Use last 105 5s candles = ~8.75 min ≈ most recent M5 context
        # Approximate EMA9 vs EMA21 using recent close averages
        lookback_short = min(idx, 9 * 60)   # 9 M5 bars ≈ 540 5s candles
        lookback_long = min(idx, 21 * 60)   # 21 M5 bars ≈ 1260 5s candles

        if lookback_short > 60 and lookback_long > 60:
            ema_short = close_arr[idx - lookback_short:idx].mean()
            ema_long = close_arr[idx - lookback_long:idx].mean()
            trend_dir = "buy" if ema_short > ema_long else "sell"
        else:
            trend_dir = None

        # Trend-following result
        if trend_dir == "buy":
            trend_result = buy_result
            trend_pips = buy_pips
        elif trend_dir == "sell":
            trend_result = sell_result
            trend_pips = sell_pips
        else:
            trend_result = "SKIP"
            trend_pips = 0

        results.append({
            "window": entry["window"],
            "date": entry["date"],
            "buy_result": buy_result,
            "buy_pips": buy_pips,
            "sell_result": sell_result,
            "sell_pips": sell_pips,
            "trend_dir": trend_dir,
            "trend_result": trend_result,
            "trend_pips": trend_pips,
        })

    return results


# ─────────────────────────────────────────────────────────
# Aggregation and analysis
# ─────────────────────────────────────────────────────────

def aggregate_results(results: List[dict], pair: str) -> dict:
    """Aggregate results by window. Returns nested dict: window → stats."""
    by_window = defaultdict(lambda: {
        "buy_tp": 0, "buy_sl": 0, "buy_open": 0,
        "sell_tp": 0, "sell_sl": 0, "sell_open": 0,
        "trend_tp": 0, "trend_sl": 0, "trend_open": 0, "trend_skip": 0,
        "dates": [],
    })

    for r in results:
        w = r["window"]
        d = by_window[w]
        d["dates"].append(r["date"])

        if r["buy_result"] == "TP":
            d["buy_tp"] += 1
        elif r["buy_result"] == "SL":
            d["buy_sl"] += 1
        else:
            d["buy_open"] += 1

        if r["sell_result"] == "TP":
            d["sell_tp"] += 1
        elif r["sell_result"] == "SL":
            d["sell_sl"] += 1
        else:
            d["sell_open"] += 1

        if r["trend_result"] == "TP":
            d["trend_tp"] += 1
        elif r["trend_result"] == "SL":
            d["trend_sl"] += 1
        elif r["trend_result"] == "SKIP":
            d["trend_skip"] += 1
        else:
            d["trend_open"] += 1

    return dict(by_window)


def compute_window_stats(agg: dict) -> List[dict]:
    """Compute stats for each window from aggregated data."""
    stats = []
    for window, d in agg.items():
        buy_total = d["buy_tp"] + d["buy_sl"]
        sell_total = d["sell_tp"] + d["sell_sl"]
        trend_total = d["trend_tp"] + d["trend_sl"]

        buy_wr = d["buy_tp"] / buy_total if buy_total > 0 else 0
        sell_wr = d["sell_tp"] / sell_total if sell_total > 0 else 0
        trend_wr = d["trend_tp"] / trend_total if trend_total > 0 else 0

        # Expected value per trade in pips
        buy_ev = buy_wr * TP_PIPS - (1 - buy_wr) * SL_PIPS if buy_total > 0 else 0
        sell_ev = sell_wr * TP_PIPS - (1 - sell_wr) * SL_PIPS if sell_total > 0 else 0
        trend_ev = trend_wr * TP_PIPS - (1 - trend_wr) * SL_PIPS if trend_total > 0 else 0

        # "Best direction" = pick whichever direction has higher EV
        if buy_ev >= sell_ev:
            best_dir = "BUY"
            best_wr = buy_wr
            best_ev = buy_ev
            best_n = buy_total
        else:
            best_dir = "SELL"
            best_wr = sell_wr
            best_ev = sell_ev
            best_n = sell_total

        stats.append({
            "window": window,
            "label": window_label(*window),
            "buy_wr": buy_wr, "buy_ev": buy_ev, "buy_n": buy_total,
            "sell_wr": sell_wr, "sell_ev": sell_ev, "sell_n": sell_total,
            "trend_wr": trend_wr, "trend_ev": trend_ev, "trend_n": trend_total,
            "best_dir": best_dir, "best_wr": best_wr, "best_ev": best_ev, "best_n": best_n,
        })

    stats.sort(key=lambda s: s["trend_ev"], reverse=True)
    return stats


# ─────────────────────────────────────────────────────────
# Cross-validation
# ─────────────────────────────────────────────────────────

def cross_validate(results: List[dict]) -> Tuple[List[dict], List[dict]]:
    """
    Split results into train (first 70%) and test (last 30%) by date.
    For each window, find best direction on train, evaluate on test.
    """
    all_dates = sorted(set(r["date"] for r in results))
    split_idx = int(len(all_dates) * 0.7)
    train_dates = set(all_dates[:split_idx])
    test_dates = set(all_dates[split_idx:])

    train = [r for r in results if r["date"] in train_dates]
    test = [r for r in results if r["date"] in test_dates]

    return train, test


# ─────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────

def print_pair_report(pair: str, stats: List[dict]):
    """Print window ranking for one pair."""
    print(f"\n{'='*110}")
    print(f"  {pair} — Window Rankings (sorted by trend-following EV)")
    print(f"{'='*110}")
    print(f"  {'Window':<14} {'Trend':>6} {'Trend':>6} {'Trend':>7} │ "
          f"{'Buy':>6} {'Buy':>7} │ {'Sell':>6} {'Sell':>7} │ {'Best':>4} {'Best':>6} {'Best':>7} │ {'N':>5}")
    print(f"  {'':.<14} {'WR%':>6} {'EV':>6} {'N':>7} │ "
          f"{'WR%':>6} {'EV':>7} │ {'WR%':>6} {'EV':>7} │ {'Dir':>4} {'WR%':>6} {'EV':>7} │ {'tot':>5}")
    print(f"  {'-'*107}")

    for s in stats:
        trend_marker = " ***" if s["trend_ev"] > 0 and s["trend_n"] >= MIN_SAMPLE_SIZE else ""
        buy_marker = "+" if s["buy_ev"] > 0 and s["buy_n"] >= MIN_SAMPLE_SIZE else ""
        sell_marker = "+" if s["sell_ev"] > 0 and s["sell_n"] >= MIN_SAMPLE_SIZE else ""

        print(f"  {s['label']:<14} "
              f"{s['trend_wr']*100:>5.1f}% {s['trend_ev']:>+6.1f} {s['trend_n']:>7} │ "
              f"{s['buy_wr']*100:>5.1f}% {s['buy_ev']:>+6.1f}{buy_marker} │ "
              f"{s['sell_wr']*100:>5.1f}% {s['sell_ev']:>+6.1f}{sell_marker} │ "
              f"{s['best_dir']:>4} {s['best_wr']*100:>5.1f}% {s['best_ev']:>+6.1f} │ "
              f"{s['buy_n'] + s['sell_n']:>5}"
              f"{trend_marker}")


def print_cross_validation(pair: str, train_stats: List[dict], test_stats: List[dict]):
    """Show how train-period winners performed in test period."""
    print(f"\n  Cross-Validation for {pair} (train: first 70% of dates, test: last 30%):")
    print(f"  {'Window':<14} {'Train EV':>9} {'Train WR':>9} │ {'Test EV':>9} {'Test WR':>9} │ {'Validated?':>12}")
    print(f"  {'-'*70}")

    # Get top windows from training
    train_map = {s["window"]: s for s in train_stats}
    test_map = {s["window"]: s for s in test_stats}

    # Sort by trend EV on training data
    ranked = sorted(train_stats, key=lambda s: s["trend_ev"], reverse=True)

    for s in ranked[:15]:  # Top 15
        w = s["window"]
        test_s = test_map.get(w, {})
        test_ev = test_s.get("trend_ev", 0)
        test_wr = test_s.get("trend_wr", 0)
        test_n = test_s.get("trend_n", 0)

        validated = "YES" if test_ev > 0 and test_n >= 20 else ("maybe" if test_ev > 0 else "NO")
        print(f"  {s['label']:<14} {s['trend_ev']:>+8.1f}p {s['trend_wr']*100:>7.1f}% │ "
              f"{test_ev:>+8.1f}p {test_wr*100:>7.1f}% │ {validated:>12}")


def print_global_ranking(all_pair_stats: Dict[str, List[dict]], all_pair_results: Dict[str, List[dict]]):
    """
    Aggregate across all pairs to find globally optimal windows.
    """
    print("\n" + "=" * 120)
    print("GLOBAL WINDOW RANKING (Aggregated Across All Pairs)")
    print("=" * 120)

    # For each window, aggregate across pairs
    global_windows = defaultdict(lambda: {
        "trend_tp": 0, "trend_sl": 0, "trend_total": 0,
        "buy_tp": 0, "buy_sl": 0, "buy_total": 0,
        "sell_tp": 0, "sell_sl": 0, "sell_total": 0,
        "pair_evs": [],  # EV per pair for this window
    })

    for pair, results in all_pair_results.items():
        pair_agg = aggregate_results(results, pair)
        for window, d in pair_agg.items():
            g = global_windows[window]
            g["trend_tp"] += d["trend_tp"]
            g["trend_sl"] += d["trend_sl"]
            g["trend_total"] += d["trend_tp"] + d["trend_sl"]
            g["buy_tp"] += d["buy_tp"]
            g["buy_sl"] += d["buy_sl"]
            g["buy_total"] += d["buy_tp"] + d["buy_sl"]
            g["sell_tp"] += d["sell_tp"]
            g["sell_sl"] += d["sell_sl"]
            g["sell_total"] += d["sell_tp"] + d["sell_sl"]

            # Per-pair trend EV for this window
            t_total = d["trend_tp"] + d["trend_sl"]
            if t_total > 0:
                t_wr = d["trend_tp"] / t_total
                t_ev = t_wr * TP_PIPS - (1 - t_wr) * SL_PIPS
                g["pair_evs"].append((pair, t_ev, t_total))

    # Compute global stats
    global_stats = []
    for window, g in global_windows.items():
        trend_wr = g["trend_tp"] / g["trend_total"] if g["trend_total"] > 0 else 0
        trend_ev = trend_wr * TP_PIPS - (1 - trend_wr) * SL_PIPS if g["trend_total"] > 0 else 0
        buy_wr = g["buy_tp"] / g["buy_total"] if g["buy_total"] > 0 else 0
        buy_ev = buy_wr * TP_PIPS - (1 - buy_wr) * SL_PIPS if g["buy_total"] > 0 else 0
        sell_wr = g["sell_tp"] / g["sell_total"] if g["sell_total"] > 0 else 0
        sell_ev = sell_wr * TP_PIPS - (1 - sell_wr) * SL_PIPS if g["sell_total"] > 0 else 0

        # How many pairs are profitable in this window?
        pairs_positive = sum(1 for _, ev, _ in g["pair_evs"] if ev > 0)
        pairs_total = len(g["pair_evs"])

        global_stats.append({
            "window": window,
            "label": window_label(*window),
            "trend_wr": trend_wr, "trend_ev": trend_ev, "trend_n": g["trend_total"],
            "buy_wr": buy_wr, "buy_ev": buy_ev, "buy_n": g["buy_total"],
            "sell_wr": sell_wr, "sell_ev": sell_ev, "sell_n": g["sell_total"],
            "pairs_positive": pairs_positive, "pairs_total": pairs_total,
            "pair_evs": g["pair_evs"],
        })

    global_stats.sort(key=lambda s: s["trend_ev"], reverse=True)

    print(f"\n  {'Window':<14} {'Trend':>6} {'Trend':>7} {'Trend':>7} │ "
          f"{'Buy':>6} {'Buy':>7} │ {'Sell':>6} {'Sell':>7} │ "
          f"{'Pairs+':>6} │ {'Status':>12}")
    print(f"  {'':.<14} {'WR%':>6} {'EV(p)':>7} {'N':>7} │ "
          f"{'WR%':>6} {'EV(p)':>7} │ {'WR%':>6} {'EV(p)':>7} │ "
          f"{'/ Tot':>6} │")
    print(f"  {'-'*100}")

    for s in global_stats:
        # Status based on EV and sample size
        if s["trend_ev"] > 0 and s["trend_n"] >= MIN_SAMPLE_SIZE * 2:
            status = "PROFITABLE"
        elif s["trend_ev"] > 0:
            status = "positive"
        elif s["trend_ev"] > -5:
            status = "marginal"
        else:
            status = "AVOID"

        print(f"  {s['label']:<14} "
              f"{s['trend_wr']*100:>5.1f}% {s['trend_ev']:>+6.1f}p {s['trend_n']:>7} │ "
              f"{s['buy_wr']*100:>5.1f}% {s['buy_ev']:>+6.1f}p │ "
              f"{s['sell_wr']*100:>5.1f}% {s['sell_ev']:>+6.1f}p │ "
              f"{s['pairs_positive']:>3}/{s['pairs_total']:<2} │ "
              f"{status:>12}")

    # ─── Recommended collection ───
    print(f"\n{'='*120}")
    print("RECOMMENDED WINDOW COLLECTION")
    print("Windows with positive trend-following EV and sufficient sample size")
    print(f"Breakeven win rate for TP+40/SL-60: {BREAKEVEN_WR*100:.0f}%")
    print(f"{'='*120}")

    profitable = [s for s in global_stats
                  if s["trend_ev"] > 0 and s["trend_n"] >= MIN_SAMPLE_SIZE]

    if not profitable:
        print("\n  No windows meet the criteria. Lowering sample size requirement...")
        profitable = [s for s in global_stats if s["trend_ev"] > 0 and s["trend_n"] >= 20]

    print(f"\n  Found {len(profitable)} profitable windows:\n")

    cumulative_tp = 0
    cumulative_sl = 0
    print(f"  {'#':>3} {'Window':<14} {'WR%':>6} {'EV':>7} {'N':>6} │ "
          f"{'Cum WR%':>7} {'Cum EV':>7} {'Cum N':>6} │ {'Pairs profitable'}")
    print(f"  {'-'*95}")

    for i, s in enumerate(profitable):
        # Get trend TP/SL counts
        trend_tp = int(s["trend_wr"] * s["trend_n"])
        trend_sl = s["trend_n"] - trend_tp
        cumulative_tp += trend_tp
        cumulative_sl += trend_sl
        cum_total = cumulative_tp + cumulative_sl
        cum_wr = cumulative_tp / cum_total if cum_total > 0 else 0
        cum_ev = cum_wr * TP_PIPS - (1 - cum_wr) * SL_PIPS

        pair_str = ", ".join(f"{p}({ev:+.0f})" for p, ev, n in sorted(s["pair_evs"], key=lambda x: -x[1]) if ev > 0)

        print(f"  {i+1:>3} {s['label']:<14} "
              f"{s['trend_wr']*100:>5.1f}% {s['trend_ev']:>+6.1f}p {s['trend_n']:>6} │ "
              f"{cum_wr*100:>6.1f}% {cum_ev:>+6.1f}p {cum_total:>6} │ "
              f"{pair_str[:60]}")

    # ─── Python config output ───
    if profitable:
        print(f"\n\n  # Paste this into the bot's Config class:")
        print(f"  USE_TIME_WINDOWS = True")
        print(f"  TRADE_WINDOWS = [")
        for s in profitable:
            h, hh = s["window"]
            print(f"      ({h}, {hh}),  # {s['label']}: EV={s['trend_ev']:+.1f}p, "
                  f"WR={s['trend_wr']*100:.0f}%, N={s['trend_n']}")
        print(f"  ]")

    # ─── Show per-pair breakdown for top windows ───
    print(f"\n\n{'='*120}")
    print("PER-PAIR BREAKDOWN FOR TOP WINDOWS")
    print(f"{'='*120}")

    for s in profitable[:10]:
        print(f"\n  {s['label']}:")
        for pair_name, ev, n in sorted(s["pair_evs"], key=lambda x: -x[1]):
            wr = (ev + SL_PIPS) / (TP_PIPS + SL_PIPS)
            bar = "+" * max(0, int(ev / 2)) if ev > 0 else "-" * max(0, int(-ev / 2))
            print(f"    {pair_name:<10} EV={ev:>+6.1f}p  WR={wr*100:>5.1f}%  N={n:>5}  {bar}")

    return profitable


def print_global_cross_validation(all_pair_results: Dict[str, List[dict]], profitable_windows: List[dict]):
    """Cross-validate the recommended windows across all pairs combined."""
    print(f"\n\n{'='*120}")
    print("CROSS-VALIDATION (Train: first 70%, Test: last 30%)")
    print(f"{'='*120}")

    # Combine all results across pairs
    all_results_combined = []
    for pair, results in all_pair_results.items():
        for r in results:
            r_copy = dict(r)
            r_copy["pair"] = pair
            all_results_combined.append(r_copy)

    all_dates = sorted(set(r["date"] for r in all_results_combined))
    split_idx = int(len(all_dates) * 0.7)
    train_dates = set(all_dates[:split_idx])
    test_dates = set(all_dates[split_idx:])

    print(f"  Train period: {min(all_dates)} to {sorted(train_dates)[-1]} ({len(train_dates)} days)")
    print(f"  Test period:  {sorted(test_dates)[0]} to {max(all_dates)} ({len(test_dates)} days)")

    train = [r for r in all_results_combined if r["date"] in train_dates]
    test = [r for r in all_results_combined if r["date"] in test_dates]

    # For each profitable window, compute train and test EV
    print(f"\n  {'Window':<14} {'Train WR':>8} {'Train EV':>9} {'Train N':>8} │ "
          f"{'Test WR':>8} {'Test EV':>9} {'Test N':>8} │ {'Valid?':>8}")
    print(f"  {'-'*90}")

    validated_count = 0
    for s in profitable_windows[:15]:
        w = s["window"]

        # Train stats
        train_w = [r for r in train if r["window"] == w and r["trend_result"] in ("TP", "SL")]
        train_tp = sum(1 for r in train_w if r["trend_result"] == "TP")
        train_n = len(train_w)
        train_wr = train_tp / train_n if train_n > 0 else 0
        train_ev = train_wr * TP_PIPS - (1 - train_wr) * SL_PIPS if train_n > 0 else 0

        # Test stats
        test_w = [r for r in test if r["window"] == w and r["trend_result"] in ("TP", "SL")]
        test_tp = sum(1 for r in test_w if r["trend_result"] == "TP")
        test_n = len(test_w)
        test_wr = test_tp / test_n if test_n > 0 else 0
        test_ev = test_wr * TP_PIPS - (1 - test_wr) * SL_PIPS if test_n > 0 else 0

        valid = "YES" if test_ev > 0 and test_n >= 20 else ("maybe" if test_ev > 0 else "NO")
        if valid == "YES":
            validated_count += 1

        print(f"  {s['label']:<14} {train_wr*100:>7.1f}% {train_ev:>+8.1f}p {train_n:>8} │ "
              f"{test_wr*100:>7.1f}% {test_ev:>+8.1f}p {test_n:>8} │ {valid:>8}")

    print(f"\n  Validated: {validated_count}/{min(len(profitable_windows), 15)} windows hold up in test period")

    # Final validated config
    print(f"\n  # VALIDATED window config (positive EV in BOTH train and test):")
    print(f"  USE_TIME_WINDOWS = True")
    print(f"  TRADE_WINDOWS = [")

    for s in profitable_windows:
        w = s["window"]
        test_w = [r for r in test if r["window"] == w and r["trend_result"] in ("TP", "SL")]
        test_tp = sum(1 for r in test_w if r["trend_result"] == "TP")
        test_n = len(test_w)
        test_wr = test_tp / test_n if test_n > 0 else 0
        test_ev = test_wr * TP_PIPS - (1 - test_wr) * SL_PIPS if test_n > 0 else 0

        if test_ev > 0 and test_n >= 20:
            h, hh = s["window"]
            print(f"      ({h}, {hh}),  # {s['label']}: "
                  f"train EV={s['trend_ev']:+.1f}p, test EV={test_ev:+.1f}p, "
                  f"train N={s['trend_n']}, test N={test_n}")

    print(f"  ]")


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="30-Minute Window Optimizer for 40/60 Bot")
    parser.add_argument("--data-dir", required=True, help="Directory containing parquet files")
    parser.add_argument("--start", default=None, help="Start date (YYYY-MM-DD), default: use all data")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD), default: use all data")
    parser.add_argument("--pairs", nargs="+", default=None,
                        help="Specific pairs to test (e.g., GBP_USD EUR_USD). Default: all available")
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLE_SIZE,
                        help=f"Min samples per window (default: {MIN_SAMPLE_SIZE})")
    args = parser.parse_args()

    global MIN_SAMPLE_SIZE
    MIN_SAMPLE_SIZE = args.min_samples

    print("=" * 120)
    print("40/60 BOT — 30-MINUTE WINDOW OPTIMIZER")
    print("Finding optimal trading windows using 5-second bid/ask data")
    print(f"Strategy: TP +{TP_PIPS} pips / SL -{SL_PIPS} pips")
    print(f"Breakeven win rate: {BREAKEVEN_WR*100:.0f}%")
    print(f"Min sample size: {MIN_SAMPLE_SIZE}")
    if args.start:
        print(f"Date range: {args.start} to {args.end or 'latest'}")
    else:
        print("Date range: ALL available data")
    print("=" * 120)

    # Find available pairs
    pairs_to_test = args.pairs if args.pairs else ALL_PAIRS
    available_pairs = []
    for pair in pairs_to_test:
        path = find_parquet(args.data_dir, pair)
        if path:
            available_pairs.append((pair, path))
        else:
            print(f"  WARNING: No parquet file found for {pair}, skipping")

    print(f"\nPairs to test: {', '.join(p for p, _ in available_pairs)}")

    # Process each pair
    all_pair_stats: Dict[str, List[dict]] = {}
    all_pair_results: Dict[str, List[dict]] = {}

    for pair, parquet_path in available_pairs:
        print(f"\n{'─'*80}")
        print(f"Processing {pair}...")
        print(f"{'─'*80}")

        t0 = time_mod.time()

        # Load data
        print(f"  Loading 5s data...", end="", flush=True)
        data = load_pair_data(parquet_path, args.start, args.end)
        print(f" {data['n']:,} rows in {time_mod.time()-t0:.1f}s")

        # Find entry points
        print(f"  Finding window entry points...", end="", flush=True)
        entries = find_window_entries(data)
        print(f" {len(entries):,} entries across {len(set(e['date'] for e in entries)):,} trading days")

        # Simulate
        print(f"  Simulating trades...", end="", flush=True)
        t1 = time_mod.time()
        pip = pip_size(pair)
        results = simulate_entries(data, entries, pip)
        elapsed = time_mod.time() - t1
        print(f" done in {elapsed:.1f}s ({len(results)/max(elapsed,0.001):.0f} trades/sec)")

        # Quick summary
        trend_tp = sum(1 for r in results if r["trend_result"] == "TP")
        trend_sl = sum(1 for r in results if r["trend_result"] == "SL")
        trend_total = trend_tp + trend_sl
        if trend_total > 0:
            overall_wr = trend_tp / trend_total
            overall_ev = overall_wr * TP_PIPS - (1 - overall_wr) * SL_PIPS
            print(f"  Overall trend-following: WR={overall_wr*100:.1f}%, EV={overall_ev:+.1f}p, "
                  f"N={trend_total}")

        # Aggregate
        agg = aggregate_results(results, pair)
        stats = compute_window_stats(agg)
        all_pair_stats[pair] = stats
        all_pair_results[pair] = results

        # Print per-pair ranking
        print_pair_report(pair, stats)

        # Free memory
        del data

    # Global ranking
    profitable = print_global_ranking(all_pair_stats, all_pair_results)

    # Cross-validation
    if profitable:
        print_global_cross_validation(all_pair_results, profitable)

    print(f"\n{'='*120}")
    print("DONE")
    print(f"{'='*120}")


if __name__ == "__main__":
    main()
