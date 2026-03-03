#!/usr/bin/env python3
"""
5-Second Trailing Stop Simulation for 40/60 Bot

Run this locally where your parquet files are stored.

Usage:
    python3 simulate_trailing_stops_5s.py --data-dir /path/to/parquet/files

Expects parquet files named like:
    GBP_USD_S5_20191101_20260303.parquet
    EUR_USD_S5_*.parquet
    etc.

Each parquet must have columns:
    time, bid_open, bid_high, bid_low, bid_close,
    ask_open, ask_high, ask_low, ask_close
"""

import argparse
import csv
import glob
import os
import sys
import time as time_mod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

import pandas as pd
import pyarrow.parquet as pq

# ─────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────

TRANSACTION_CSV = "transactions_101-004-31618463-003 (14).csv"

# Date range to load from parquet (buffer 1 day either side)
DATE_START = pd.Timestamp("2026-02-17", tz="UTC")
DATE_END = pd.Timestamp("2026-03-04", tz="UTC")

# Trailing stop strategies to test
STRATEGIES = {
    "A_baseline":    {"desc": "Current: TP+40 SL-60",              "tp_pips": 40, "sl_pips": 60, "be_at": None, "trail_activate": None, "trail_distance": None},
    "B_BE20":        {"desc": "BE@20: SL→BE at +20, TP+40",       "tp_pips": 40, "sl_pips": 60, "be_at": 20,   "trail_activate": None, "trail_distance": None},
    "C_BE15":        {"desc": "BE@15: SL→BE at +15, TP+40",       "tp_pips": 40, "sl_pips": 60, "be_at": 15,   "trail_activate": None, "trail_distance": None},
    "D_trail20_15":  {"desc": "Trail@20/15: no TP, trail 15p",    "tp_pips": None, "sl_pips": 60, "be_at": None, "trail_activate": 20, "trail_distance": 15},
    "E_trail20_10":  {"desc": "Trail@20/10: no TP, trail 10p",    "tp_pips": None, "sl_pips": 60, "be_at": None, "trail_activate": 20, "trail_distance": 10},
    "F_trail30_20":  {"desc": "Trail@30/20: no TP, trail 20p",    "tp_pips": None, "sl_pips": 60, "be_at": None, "trail_activate": 30, "trail_distance": 20},
    "G_trail15_10":  {"desc": "Trail@15/10: no TP, trail 10p",    "tp_pips": None, "sl_pips": 60, "be_at": None, "trail_activate": 15, "trail_distance": 10},
    "H_BE20_TP60":   {"desc": "BE@20 + TP60: wider target",       "tp_pips": 60, "sl_pips": 60, "be_at": 20,   "trail_activate": None, "trail_distance": None},
    "I_BE20_trail30": {"desc": "BE@20 then trail@30/15",          "tp_pips": None, "sl_pips": 60, "be_at": 20,  "trail_activate": 30, "trail_distance": 15},
}


# ─────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────

@dataclass
class Trade:
    ticket: str
    instrument: str          # e.g. "GBP/USD"
    instrument_oanda: str    # e.g. "GBP_USD"
    direction: str           # "buy" or "sell"
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    exit_time: datetime
    exit_price: float
    exit_reason: str         # "TP" or "SL"
    pl: float
    conversion_rate: float
    is_preexisting: bool = False


@dataclass
class SimResult:
    strategy: str
    exit_reason: str         # "TP", "SL", "BE", "TRAIL"
    exit_price: float
    exit_time: datetime
    exit_pips: float         # pips from entry at exit
    pl_estimate: float       # estimated P&L using conversion rate
    mfe_pips: float          # max favourable excursion in pips
    mae_pips: float          # max adverse excursion in pips
    trail_activated: bool = False


def pip_size(instrument: str) -> float:
    return 0.01 if "JPY" in instrument else 0.0001


# ─────────────────────────────────────────────────────────
# Parse transaction CSV
# ─────────────────────────────────────────────────────────

def parse_transactions(csv_path: str) -> List[Trade]:
    """Parse the IG/OANDA transaction log into Trade objects."""
    entries = []
    exits = []

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticket = None
            for k, v in row.items():
                if "TICKET" in k:
                    ticket = v.strip().strip('"')
                    break
            if not ticket:
                continue

            tx_type = row["TRANSACTION TYPE"].strip()
            details = row["DETAILS"].strip()
            instrument = row["INSTRUMENT"].strip()
            time_str = row["TRANSACTION DATE"].strip()
            if not time_str:
                continue
            try:
                tx_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S GMT").replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            price_str = row["PRICE"].strip()
            price = float(price_str) if price_str else None
            units_str = row["UNITS"].strip()
            units = float(units_str) if units_str else None
            direction = row["DIRECTION"].strip().lower()
            sl_str = row["STOP LOSS"].strip()
            tp_str = row["TAKE PROFIT"].strip()
            pl_str = row["PL"].strip()
            conv_str = row["CONVERSION RATE"].strip()

            if tx_type == "ORDER_FILL" and details in ("LIMIT_ORDER", "MARKET_ORDER") and instrument and price:
                entries.append({
                    "ticket": ticket, "time": tx_time, "instrument": instrument,
                    "price": price, "units": units, "direction": direction,
                })

            if tx_type in ("TAKE_PROFIT_ORDER", "STOP_LOSS_ORDER") and details == "ON_FILL":
                if entries:
                    if tx_type == "TAKE_PROFIT_ORDER" and price:
                        entries[-1]["tp"] = price
                    elif tx_type == "STOP_LOSS_ORDER" and price:
                        entries[-1]["sl"] = price

            if tx_type == "ORDER_FILL" and details in ("TAKE_PROFIT_ORDER", "STOP_LOSS_ORDER") and instrument:
                exits.append({
                    "ticket": ticket, "time": tx_time, "instrument": instrument,
                    "price": price, "direction": direction,
                    "reason": "TP" if "TAKE_PROFIT" in details else "SL",
                    "pl": float(pl_str) if pl_str else 0,
                    "conv": float(conv_str) if conv_str else 1.0,
                })

    # Match entries to exits
    available_entries = defaultdict(list)
    for e in entries:
        available_entries[e["instrument"]].append(e)

    trades = []
    for exit_info in exits:
        inst = exit_info["instrument"]
        matched = None

        if inst in available_entries and available_entries[inst]:
            for i, e in enumerate(available_entries[inst]):
                if e["time"] < exit_info["time"]:
                    matched = available_entries[inst].pop(i)
                    break

        if matched:
            oanda_inst = inst.replace("/", "_")
            trades.append(Trade(
                ticket=matched["ticket"],
                instrument=inst,
                instrument_oanda=oanda_inst,
                direction=matched["direction"],
                entry_time=matched["time"],
                entry_price=matched["price"],
                stop_loss=matched.get("sl", 0),
                take_profit=matched.get("tp", 0),
                exit_time=exit_info["time"],
                exit_price=exit_info["price"],
                exit_reason=exit_info["reason"],
                pl=exit_info["pl"],
                conversion_rate=exit_info["conv"],
                is_preexisting=False,
            ))
        else:
            oanda_inst = inst.replace("/", "_")
            trades.append(Trade(
                ticket=exit_info["ticket"],
                instrument=inst,
                instrument_oanda=oanda_inst,
                direction="sell" if exit_info["direction"] == "Buy" else "buy",
                entry_time=exit_info["time"] - timedelta(hours=12),
                entry_price=0,
                stop_loss=0,
                take_profit=0,
                exit_time=exit_info["time"],
                exit_price=exit_info["price"],
                exit_reason=exit_info["reason"],
                pl=exit_info["pl"],
                conversion_rate=exit_info["conv"],
                is_preexisting=True,
            ))

    trades.sort(key=lambda t: t.exit_time)
    return trades


# ─────────────────────────────────────────────────────────
# Load parquet data
# ─────────────────────────────────────────────────────────

def find_parquet_file(data_dir: str, instrument_oanda: str) -> Optional[str]:
    """Find the parquet file for an instrument."""
    pattern = os.path.join(data_dir, f"{instrument_oanda}_S5_*.parquet")
    matches = glob.glob(pattern)
    if matches:
        return matches[0]
    # Try without the date suffix
    pattern2 = os.path.join(data_dir, f"{instrument_oanda}*.parquet")
    matches2 = glob.glob(pattern2)
    if matches2:
        return matches2[0]
    return None


def load_price_data(parquet_path: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Load 5s price data for a date range from parquet. Uses row-group filtering."""
    print(f"  Loading {os.path.basename(parquet_path)} [{start.date()} to {end.date()}]...", end="", flush=True)
    t0 = time_mod.time()

    # Use pyarrow for efficient filtering
    filters = [
        ("time", ">=", start),
        ("time", "<=", end),
    ]

    df = pq.read_table(
        parquet_path,
        filters=filters,
        columns=["time", "bid_high", "bid_low", "bid_close", "ask_high", "ask_low", "ask_close"],
    ).to_pandas()

    elapsed = time_mod.time() - t0
    print(f" {len(df):,} rows in {elapsed:.1f}s")
    return df.sort_values("time").reset_index(drop=True)


# ─────────────────────────────────────────────────────────
# Core simulation
# ─────────────────────────────────────────────────────────

def simulate_trade(trade: Trade, price_data: pd.DataFrame, strategy: dict) -> Optional[SimResult]:
    """
    Simulate a single trade through 5s price data with a given strategy.

    Uses bid prices for long exits, ask prices for short exits (matching broker execution).

    For a LONG (buy) position:
        - Profit = bid_now - entry_price
        - SL hit when bid_low <= SL level
        - TP hit when bid_high >= TP level
        - Trailing stop tracks highest bid_high, trail below it

    For a SHORT (sell) position:
        - Profit = entry_price - ask_now
        - SL hit when ask_high >= SL level
        - TP hit when ask_low <= TP level
        - Trailing stop tracks lowest ask_low, trail above it
    """
    pip = pip_size(trade.instrument)
    entry = trade.entry_price
    is_long = trade.direction == "buy"

    # Strategy parameters
    tp_pips = strategy["tp_pips"]
    sl_pips = strategy["sl_pips"]
    be_at = strategy["be_at"]            # pips in profit to move SL to breakeven
    trail_activate = strategy["trail_activate"]  # pips in profit to start trailing
    trail_distance = strategy["trail_distance"]  # pip distance of trail

    # Calculate absolute levels
    if is_long:
        tp_level = entry + tp_pips * pip if tp_pips else None
        sl_level = entry - sl_pips * pip
    else:
        tp_level = entry - tp_pips * pip if tp_pips else None
        sl_level = entry + sl_pips * pip

    # State
    be_activated = False
    trailing = False
    trail_level = None
    best_price = entry  # Best price seen (highest bid for long, lowest ask for short)
    mfe = 0.0  # max favourable excursion in pips
    mae = 0.0  # max adverse excursion in pips

    # Slice price data from entry to 24h after exit (generous window for trailing strategies)
    mask = (price_data["time"] >= trade.entry_time) & \
           (price_data["time"] <= trade.exit_time + timedelta(hours=24))
    candles = price_data.loc[mask]

    if len(candles) == 0:
        return None

    for _, candle in candles.iterrows():
        candle_time = candle["time"]

        if is_long:
            # For long: check against bid prices (what we'd sell at)
            check_high = candle["bid_high"]   # best price this candle
            check_low = candle["bid_low"]     # worst price this candle
            check_close = candle["bid_close"]

            profit_pips_high = (check_high - entry) / pip
            profit_pips_low = (check_low - entry) / pip
        else:
            # For short: check against ask prices (what we'd buy back at)
            check_high = candle["ask_high"]   # worst price this candle (for short)
            check_low = candle["ask_low"]     # best price this candle (for short)
            check_close = candle["ask_close"]

            profit_pips_high = (entry - check_low) / pip    # best profit
            profit_pips_low = (entry - check_high) / pip    # worst profit (most negative)

        # Update MFE/MAE
        mfe = max(mfe, profit_pips_high)
        mae = min(mae, profit_pips_low)

        # ─── Check SL first (conservative: if both SL and TP could hit in same candle, SL wins) ───

        sl_hit = False
        if is_long:
            sl_hit = check_low <= sl_level
        else:
            sl_hit = check_high >= sl_level

        # ─── Check TP ───
        tp_hit = False
        if tp_level is not None:
            if is_long:
                tp_hit = check_high >= tp_level
            else:
                tp_hit = check_low <= tp_level

        # ─── Check trailing stop ───
        trail_hit = False
        if trailing and trail_level is not None:
            if is_long:
                trail_hit = check_low <= trail_level
            else:
                trail_hit = check_high >= trail_level

        # ─── Determine what triggered ───

        # Within a single candle, we can't know the exact sequence.
        # Conservative rule: SL before trail before TP.
        # Exception: if BE is active and SL is at BE (≥0 pips), that's still "BE exit" not "SL loss".
        if sl_hit and not tp_hit and not trail_hit:
            # Pure SL hit
            exit_price = sl_level
            exit_pips = -sl_pips if not be_activated else max(0, (sl_level - entry) / pip if is_long else (entry - sl_level) / pip)
            reason = "SL" if not be_activated else "BE"
            return _make_result(strategy, reason, exit_price, candle_time, exit_pips, trade, mfe, mae, trailing)

        if trail_hit and not sl_hit:
            exit_price = trail_level
            exit_pips = (trail_level - entry) / pip if is_long else (entry - trail_level) / pip
            return _make_result(strategy, "TRAIL", exit_price, candle_time, exit_pips, trade, mfe, mae, True)

        if sl_hit and trail_hit and not tp_hit:
            # Both could hit — use trail if it's better (tighter), SL otherwise
            if is_long:
                # trail_level should be above SL if trailing is active
                exit_level = max(trail_level, sl_level) if trail_level else sl_level
            else:
                exit_level = min(trail_level, sl_level) if trail_level else sl_level
            exit_pips = (exit_level - entry) / pip if is_long else (entry - exit_level) / pip
            reason = "TRAIL" if trail_level and ((is_long and trail_level > sl_level) or (not is_long and trail_level < sl_level)) else ("BE" if be_activated else "SL")
            return _make_result(strategy, reason, exit_level, candle_time, exit_pips, trade, mfe, mae, trailing)

        if tp_hit and not sl_hit and not trail_hit:
            exit_pips = tp_pips
            return _make_result(strategy, "TP", tp_level, candle_time, exit_pips, trade, mfe, mae, trailing)

        if tp_hit and (sl_hit or trail_hit):
            # Ambiguous candle — conservative: assume adverse movement first
            if sl_hit:
                exit_price = sl_level
                exit_pips = -sl_pips if not be_activated else 0
                reason = "SL" if not be_activated else "BE"
            else:
                exit_price = trail_level
                exit_pips = (trail_level - entry) / pip if is_long else (entry - trail_level) / pip
                reason = "TRAIL"
            return _make_result(strategy, reason, exit_price, candle_time, exit_pips, trade, mfe, mae, trailing)

        # ─── Update trailing stop state ───

        # Breakeven activation
        if be_at and not be_activated and profit_pips_high >= be_at:
            be_activated = True
            if is_long:
                sl_level = entry + 1 * pip  # +1 pip above entry (cover spread/commission)
            else:
                sl_level = entry - 1 * pip

        # Trailing stop activation and update
        if trail_activate and profit_pips_high >= trail_activate:
            if is_long:
                new_best = check_high
                if new_best > best_price:
                    best_price = new_best
                new_trail = best_price - trail_distance * pip
                if not trailing or new_trail > trail_level:
                    trail_level = new_trail
                    trailing = True
            else:
                new_best = check_low
                if new_best < best_price or not trailing:
                    best_price = new_best
                new_trail = best_price + trail_distance * pip
                if not trailing or new_trail < trail_level:
                    trail_level = new_trail
                    trailing = True

    # If we get here, the trade didn't close within our data window
    # Use the last available close price
    last = candles.iloc[-1]
    if is_long:
        exit_price = last["bid_close"]
    else:
        exit_price = last["ask_close"]
    exit_pips = (exit_price - entry) / pip if is_long else (entry - exit_price) / pip
    return _make_result(strategy, "OPEN", exit_price, last["time"], exit_pips, trade, mfe, mae, trailing)


def _make_result(strategy, reason, price, time, pips, trade, mfe, mae, trailing):
    """Build a SimResult, estimating P&L from pips and conversion rate."""
    pip = pip_size(trade.instrument)
    # Estimate P&L: use the actual trade's P&L per pip ratio
    if trade.exit_reason == "TP":
        actual_pips = 40
    else:
        actual_pips = -60
    pl_per_pip = trade.pl / actual_pips if actual_pips != 0 else 0
    estimated_pl = pips * pl_per_pip

    return SimResult(
        strategy=strategy.get("desc", "?"),
        exit_reason=reason,
        exit_price=price,
        exit_time=time,
        exit_pips=pips,
        pl_estimate=estimated_pl,
        mfe_pips=mfe,
        mae_pips=mae,
        trail_activated=trailing,
    )


# ─────────────────────────────────────────────────────────
# Chain analysis
# ─────────────────────────────────────────────────────────

def simulate_chain_hold(chain: List[Trade], price_data: pd.DataFrame, strategy: dict) -> Optional[SimResult]:
    """
    Instead of TP→re-enter→TP→re-enter→SL, hold the FIRST trade's position
    with a given strategy through the entire chain duration + 24h buffer.

    Uses the first trade's entry, but extends the exit window to cover the
    entire chain + extra time for trailing to play out.
    """
    if not chain or chain[0].is_preexisting:
        return None

    first = chain[0]
    last = chain[-1]

    # Create a synthetic trade that spans the entire chain
    synthetic = Trade(
        ticket=first.ticket,
        instrument=first.instrument,
        instrument_oanda=first.instrument_oanda,
        direction=first.direction,
        entry_time=first.entry_time,
        entry_price=first.entry_price,
        stop_loss=first.stop_loss,
        take_profit=first.take_profit,
        exit_time=last.exit_time + timedelta(hours=24),  # Extend window
        exit_price=last.exit_price,
        exit_reason="CHAIN",
        pl=sum(t.pl for t in chain),
        conversion_rate=first.conversion_rate,
    )

    return simulate_trade(synthetic, price_data, strategy)


# ─────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────

def print_trade_detail(trade: Trade, results: Dict[str, SimResult]):
    """Print detailed results for one trade across all strategies."""
    pre = "PRE " if trade.is_preexisting else ""
    print(f"\n  {pre}#{trade.ticket} {trade.instrument} {trade.direction.upper()} "
          f"@ {trade.entry_price:.5f} → {trade.exit_reason} @ {trade.exit_price:.5f} "
          f"(£{trade.pl:>+.2f})")
    print(f"    Entry: {trade.entry_time.strftime('%m/%d %H:%M')}  "
          f"Exit: {trade.exit_time.strftime('%m/%d %H:%M')}")

    if trade.is_preexisting:
        print(f"    [Pre-existing: no entry in log, skipping simulation]")
        return

    baseline = results.get("A_baseline")
    if baseline:
        print(f"    MFE: {baseline.mfe_pips:>+.1f} pips  |  MAE: {baseline.mae_pips:>+.1f} pips")

    print(f"    {'Strategy':<35} {'Exit':>6} {'Pips':>8} {'Est P&L':>10} {'Trail?':>7}")
    print(f"    {'-'*70}")
    for key in sorted(results.keys()):
        r = results[key]
        trail_str = "YES" if r.trail_activated else ""
        print(f"    {r.strategy:<35} {r.exit_reason:>6} {r.exit_pips:>+8.1f} "
              f"£{r.pl_estimate:>+9.2f} {trail_str:>7}")


def print_summary(all_results: Dict[str, List[Tuple[Trade, SimResult]]]):
    """Print summary comparison across all strategies."""
    print("\n" + "=" * 120)
    print("STRATEGY COMPARISON SUMMARY")
    print("=" * 120)

    headers = ["Strategy", "Total P&L", "Trades", "Wins", "Losses", "WR%",
               "Avg Win", "Avg Loss", "PF", "TP", "SL", "BE", "Trail", "Open"]
    print(f"  {headers[0]:<35} {headers[1]:>10} {headers[2]:>6} {headers[3]:>5} {headers[4]:>6} "
          f"{headers[5]:>5} {headers[6]:>8} {headers[7]:>9} {headers[8]:>6} "
          f"{headers[9]:>4} {headers[10]:>4} {headers[11]:>4} {headers[12]:>5} {headers[13]:>4}")
    print("  " + "-" * 115)

    for key in sorted(all_results.keys()):
        items = all_results[key]
        total_pl = sum(r.pl_estimate for _, r in items)
        wins = sum(1 for _, r in items if r.pl_estimate > 0)
        losses = sum(1 for _, r in items if r.pl_estimate <= 0)
        wr = wins / max(wins + losses, 1) * 100
        avg_win = sum(r.pl_estimate for _, r in items if r.pl_estimate > 0) / max(wins, 1)
        avg_loss = sum(r.pl_estimate for _, r in items if r.pl_estimate <= 0) / max(losses, 1)
        win_total = sum(r.pl_estimate for _, r in items if r.pl_estimate > 0)
        loss_total = abs(sum(r.pl_estimate for _, r in items if r.pl_estimate <= 0))
        pf = win_total / loss_total if loss_total > 0 else float("inf")
        n_tp = sum(1 for _, r in items if r.exit_reason == "TP")
        n_sl = sum(1 for _, r in items if r.exit_reason == "SL")
        n_be = sum(1 for _, r in items if r.exit_reason == "BE")
        n_trail = sum(1 for _, r in items if r.exit_reason == "TRAIL")
        n_open = sum(1 for _, r in items if r.exit_reason == "OPEN")

        desc = items[0][1].strategy if items else key
        print(f"  {desc:<35} £{total_pl:>+9.2f} {len(items):>6} {wins:>5} {losses:>6} "
              f"{wr:>4.0f}% £{avg_win:>+7.2f} £{avg_loss:>+8.2f} {pf:>5.2f} "
              f"{n_tp:>4} {n_sl:>4} {n_be:>4} {n_trail:>5} {n_open:>4}")

    # Delta from baseline
    baseline_items = all_results.get("A_baseline", [])
    if baseline_items:
        baseline_pl = sum(r.pl_estimate for _, r in baseline_items)
        print(f"\n  {'Improvement vs baseline:':<35}")
        for key in sorted(all_results.keys()):
            if key == "A_baseline":
                continue
            items = all_results[key]
            total_pl = sum(r.pl_estimate for _, r in items)
            delta = total_pl - baseline_pl
            desc = items[0][1].strategy if items else key
            print(f"    {desc:<35} £{delta:>+9.2f} ({'better' if delta > 0 else 'worse'})")


def print_mfe_mae_analysis(all_results: Dict[str, List[Tuple[Trade, SimResult]]]):
    """Print MFE/MAE analysis — the most valuable insight from 5s data."""
    print("\n" + "=" * 120)
    print("MFE/MAE ANALYSIS (Maximum Favourable/Adverse Excursion)")
    print("This shows what ACTUALLY happened during each trade — the key data we couldn't see before.")
    print("=" * 120)

    baseline_items = all_results.get("A_baseline", [])
    if not baseline_items:
        return

    tp_trades = [(t, r) for t, r in baseline_items if t.exit_reason == "TP" and not t.is_preexisting]
    sl_trades = [(t, r) for t, r in baseline_items if t.exit_reason == "SL" and not t.is_preexisting]

    print(f"\n  TP TRADES ({len(tp_trades)} trades that hit +40 TP):")
    print(f"  {'#':<6} {'Pair':<10} {'Dir':<5} {'MFE':>8} {'MAE':>8} {'Path description'}")
    print(f"  {'-'*80}")
    for t, r in tp_trades:
        # MFE should be ~40+ since it hit TP. MAE tells us the deepest drawdown before TP
        path = ""
        if r.mae_pips > -5:
            path = "Clean run to TP (minimal drawdown)"
        elif r.mae_pips > -15:
            path = f"Small pullback ({r.mae_pips:+.0f}p) before TP"
        elif r.mae_pips > -30:
            path = f"Significant pullback ({r.mae_pips:+.0f}p) before TP — trail risk!"
        else:
            path = f"DEEP pullback ({r.mae_pips:+.0f}p) before TP — trail would likely exit early"

        went_beyond = " *** BEYOND TP" if r.mfe_pips > 45 else ""
        print(f"  {t.ticket:<6} {t.instrument:<10} {t.direction:<5} "
              f"{r.mfe_pips:>+7.1f}p {r.mae_pips:>+7.1f}p {path}{went_beyond}")

    avg_mfe_tp = sum(r.mfe_pips for _, r in tp_trades) / max(len(tp_trades), 1)
    avg_mae_tp = sum(r.mae_pips for _, r in tp_trades) / max(len(tp_trades), 1)
    beyond_tp = sum(1 for _, r in tp_trades if r.mfe_pips > 45)
    deep_pullback = sum(1 for _, r in tp_trades if r.mae_pips < -15)

    print(f"\n  TP Summary: avg MFE={avg_mfe_tp:+.1f}p, avg MAE={avg_mae_tp:+.1f}p")
    print(f"  Went beyond +45p (missed profit): {beyond_tp}/{len(tp_trades)}")
    print(f"  Had pullback > 15p before TP (trail risk): {deep_pullback}/{len(tp_trades)}")

    print(f"\n  SL TRADES ({len(sl_trades)} trades that hit -60 SL):")
    print(f"  {'#':<6} {'Pair':<10} {'Dir':<5} {'MFE':>8} {'MAE':>8} {'Could BE@20 save?':>20} {'Could BE@15 save?':>20}")
    print(f"  {'-'*95}")
    be20_saved = 0
    be15_saved = 0
    for t, r in sl_trades:
        save20 = "YES — was +{:.0f}p".format(r.mfe_pips) if r.mfe_pips >= 20 else "No (max +{:.0f}p)".format(r.mfe_pips)
        save15 = "YES — was +{:.0f}p".format(r.mfe_pips) if r.mfe_pips >= 15 else "No (max +{:.0f}p)".format(r.mfe_pips)
        if r.mfe_pips >= 20:
            be20_saved += 1
        if r.mfe_pips >= 15:
            be15_saved += 1

        print(f"  {t.ticket:<6} {t.instrument:<10} {t.direction:<5} "
              f"{r.mfe_pips:>+7.1f}p {r.mae_pips:>+7.1f}p {save20:>20} {save15:>20}")

    avg_mfe_sl = sum(r.mfe_pips for _, r in sl_trades) / max(len(sl_trades), 1)
    print(f"\n  SL Summary: avg MFE={avg_mfe_sl:+.1f}p")
    print(f"  Could have been saved by BE@20: {be20_saved}/{len(sl_trades)} ({be20_saved/max(len(sl_trades),1)*100:.0f}%)")
    print(f"  Could have been saved by BE@15: {be15_saved}/{len(sl_trades)} ({be15_saved/max(len(sl_trades),1)*100:.0f}%)")

    # Potential saving
    avg_sl_pl = sum(t.pl for t, _ in sl_trades) / max(len(sl_trades), 1)
    print(f"\n  If BE@20 saved those {be20_saved} trades: £{abs(avg_sl_pl) * be20_saved:>+.2f} saved")
    print(f"  If BE@15 saved those {be15_saved} trades: £{abs(avg_sl_pl) * be15_saved:>+.2f} saved")


def print_chain_results(chain_results: List[Tuple[str, List[Trade], Dict[str, SimResult]]]):
    """Print chain hold analysis results."""
    print("\n" + "=" * 120)
    print("CHAIN HOLD ANALYSIS")
    print("Instead of TP→re-enter→TP→re-enter→SL, what if we held the first trade with a trailing stop?")
    print("=" * 120)

    for chain_id, chain, results in chain_results:
        actual_pl = sum(t.pl for t in chain)
        print(f"\n  Chain: {chain[0].instrument} {chain[0].direction.upper()} "
              f"({len(chain)} trades, {chain[0].entry_time.strftime('%m/%d %H:%M')} - "
              f"{chain[-1].exit_time.strftime('%m/%d %H:%M')})")
        print(f"  Actual chain P&L: £{actual_pl:>+.2f} "
              f"({sum(1 for t in chain if t.exit_reason=='TP')} TPs, "
              f"{sum(1 for t in chain if t.exit_reason=='SL')} SLs)")

        if not results:
            print(f"  [Skipped: pre-existing trade]")
            continue

        print(f"    {'Hold with strategy':<35} {'Exit':>6} {'Pips':>8} {'Est P&L':>10} {'vs Actual':>10}")
        print(f"    {'-'*75}")
        for key in sorted(results.keys()):
            r = results[key]
            delta = r.pl_estimate - actual_pl
            print(f"    {r.strategy:<35} {r.exit_reason:>6} {r.exit_pips:>+8.1f} "
                  f"£{r.pl_estimate:>+9.2f} £{delta:>+9.2f}")


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="5s Trailing Stop Simulation for 40/60 Bot")
    parser.add_argument("--data-dir", required=True, help="Directory containing parquet files")
    parser.add_argument("--csv", default=TRANSACTION_CSV, help="Transaction CSV file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-trade details")
    args = parser.parse_args()

    print("=" * 120)
    print("40/60 BOT — 5-SECOND TRAILING STOP SIMULATION")
    print("Using real bid/ask tick data. No interpolation. No guessing.")
    print("=" * 120)

    # ─── Parse trades ───
    print(f"\nParsing trades from {args.csv}...")
    trades = parse_transactions(args.csv)
    sim_trades = [t for t in trades if not t.is_preexisting]
    print(f"  Total trades: {len(trades)} ({len(sim_trades)} with full entry/exit data, "
          f"{len(trades) - len(sim_trades)} pre-existing)")

    # ─── Find and load parquet files ───
    instruments = set(t.instrument_oanda for t in sim_trades)
    print(f"\nLoading 5s data for: {', '.join(sorted(instruments))}")

    price_cache: Dict[str, pd.DataFrame] = {}
    missing = []
    for inst in sorted(instruments):
        path = find_parquet_file(args.data_dir, inst)
        if path:
            price_cache[inst] = load_price_data(path, DATE_START, DATE_END)
        else:
            print(f"  WARNING: No parquet file found for {inst}")
            missing.append(inst)

    if missing:
        print(f"\n  Missing data for: {', '.join(missing)}. Those trades will be skipped.")

    # ─── Run simulation for each trade × each strategy ───
    print(f"\nSimulating {len(sim_trades)} trades × {len(STRATEGIES)} strategies...")

    all_results: Dict[str, List[Tuple[Trade, SimResult]]] = {k: [] for k in STRATEGIES}

    for i, trade in enumerate(sim_trades):
        if trade.instrument_oanda not in price_cache:
            continue

        prices = price_cache[trade.instrument_oanda]
        trade_results = {}

        for strat_key, strat in STRATEGIES.items():
            result = simulate_trade(trade, prices, strat)
            if result:
                trade_results[strat_key] = result
                all_results[strat_key].append((trade, result))

        if args.verbose:
            print_trade_detail(trade, trade_results)

        # Progress
        if (i + 1) % 10 == 0:
            print(f"  Processed {i+1}/{len(sim_trades)} trades...")

    print(f"  Done. Processed {len(sim_trades)} trades.")

    # ─── MFE/MAE Analysis ───
    print_mfe_mae_analysis(all_results)

    # ─── Strategy comparison ───
    print_summary(all_results)

    # ─── Chain analysis ───
    print("\nBuilding chains (consecutive same-pair same-direction trades)...")
    chains = []
    current_chain = []
    for t in sim_trades:
        if not current_chain:
            current_chain = [t]
            continue
        prev = current_chain[-1]
        same_pair = t.instrument == prev.instrument
        same_dir = t.direction == prev.direction
        gap_hours = (t.entry_time - prev.exit_time).total_seconds() / 3600
        if same_pair and same_dir and gap_hours < 8:
            current_chain.append(t)
        else:
            if len(current_chain) >= 2:
                chains.append(current_chain)
            current_chain = [t]
    if len(current_chain) >= 2:
        chains.append(current_chain)

    print(f"  Found {len(chains)} chains with 2+ trades")

    chain_results = []
    for i, chain in enumerate(chains):
        if chain[0].instrument_oanda not in price_cache:
            continue
        prices = price_cache[chain[0].instrument_oanda]
        results = {}
        for strat_key, strat in STRATEGIES.items():
            r = simulate_chain_hold(chain, prices, strat)
            if r:
                results[strat_key] = r
        chain_results.append((f"chain_{i}", chain, results))

    print_chain_results(chain_results)

    # ─── Final verdict ───
    print("\n" + "=" * 120)
    print("CONCLUSION")
    print("=" * 120)

    baseline_pl = sum(r.pl_estimate for _, r in all_results.get("A_baseline", []))
    best_key = max(all_results.keys(), key=lambda k: sum(r.pl_estimate for _, r in all_results[k]))
    best_pl = sum(r.pl_estimate for _, r in all_results[best_key])
    best_desc = all_results[best_key][0][1].strategy if all_results[best_key] else best_key

    print(f"\n  Baseline P&L (sim):  £{baseline_pl:>+.2f}")
    print(f"  Best strategy:       {best_desc}")
    print(f"  Best strategy P&L:   £{best_pl:>+.2f} (£{best_pl - baseline_pl:>+.2f} improvement)")

    # Check if any strategy consistently beats baseline
    print(f"\n  Per-trade comparison (how often each strategy beats baseline):")
    baseline_map = {t.ticket: r for t, r in all_results.get("A_baseline", [])}
    for key in sorted(all_results.keys()):
        if key == "A_baseline":
            continue
        items = all_results[key]
        better = 0
        worse = 0
        same = 0
        for t, r in items:
            bl = baseline_map.get(t.ticket)
            if not bl:
                continue
            if abs(r.pl_estimate - bl.pl_estimate) < 0.50:
                same += 1
            elif r.pl_estimate > bl.pl_estimate:
                better += 1
            else:
                worse += 1
        desc = items[0][1].strategy if items else key
        print(f"    {desc:<35} Better: {better:>3}  Same: {same:>3}  Worse: {worse:>3}")


if __name__ == "__main__":
    main()
