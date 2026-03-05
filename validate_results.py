#!/usr/bin/env python3
"""
V3 Result Validator for the 40/60 Bot Window Optimizer

This script validates the V2 optimizer results with additional rigour:

1. WIDER WINDOWS: Tests 1-hour and 2-hour blocks (fewer comparisons, more trades per window)
2. BONFERRONI CORRECTION: Adjusts p-values for multiple comparisons
3. REGIME STABILITY: Tests if edge is consistent across years (not just months)
4. ROLLING PERFORMANCE: Detects if edge is decaying over time
5. BOOTSTRAP CONFIDENCE INTERVALS: Non-parametric 95% CI on win rate

Reads checkpoint .pkl files from the V2 optimizer run and re-analyses them.

Usage:
    python validate_results.py --checkpoint-dir .optimizer_checkpoints/run_XXXXXX

    # With custom window sizes
    python validate_results.py --checkpoint-dir .optimizer_checkpoints/run_XXXXXX --window-sizes 30 60 120

    # Show all windows (not just validated)
    python validate_results.py --checkpoint-dir .optimizer_checkpoints/run_XXXXXX --show-all
"""

import argparse
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

# Bot constants
TP_PIPS = 40
SL_PIPS = 60
BREAKEVEN_WR = SL_PIPS / (TP_PIPS + SL_PIPS)  # 0.60


def load_all_checkpoints(checkpoint_dir: str) -> Dict[str, List[dict]]:
    """Load all pair checkpoint files from a V2 optimizer run."""
    pair_results = {}
    if not os.path.isdir(checkpoint_dir):
        print(f"ERROR: Checkpoint directory not found: {checkpoint_dir}")
        sys.exit(1)

    for fname in sorted(os.listdir(checkpoint_dir)):
        if not fname.endswith(".pkl"):
            continue
        pair = fname.replace(".pkl", "")
        path = os.path.join(checkpoint_dir, fname)
        with open(path, "rb") as f:
            data = pickle.load(f)
        pair_results[pair] = data["results"]
        print(f"  Loaded {pair}: {len(data['results']):,} trades")

    return pair_results


def assign_wider_window(trade: dict, window_minutes: int) -> str:
    """Assign a trade to a wider window based on its signal time."""
    t = trade["time"]
    total_minutes = t.hour * 60 + t.minute
    window_start = (total_minutes // window_minutes) * window_minutes
    h = window_start // 60
    m = window_start % 60
    end_minutes = window_start + window_minutes - 1
    eh = end_minutes // 60
    em = end_minutes % 60
    return f"{h:02d}:{m:02d}-{eh:02d}:{em:02d}"


def binomial_pvalue(k: int, n: int, p0: float = 0.60) -> float:
    """One-sided binomial test p-value: P(X >= k | n, p0)."""
    # Use normal approximation for large n, exact for small n
    if n == 0:
        return 1.0
    if n < 30:
        # Exact binomial
        from math import comb
        p_val = sum(comb(n, i) * (p0 ** i) * ((1 - p0) ** (n - i)) for i in range(k, n + 1))
        return p_val
    else:
        # Normal approximation with continuity correction
        mean = n * p0
        std = np.sqrt(n * p0 * (1 - p0))
        if std == 0:
            return 0.0 if k > mean else 1.0
        z = (k - 0.5 - mean) / std
        # One-sided: P(Z >= z)
        from scipy.stats import norm
        return float(1 - norm.cdf(z))


def bootstrap_wr_ci(results: list, n_bootstrap: int = 10000, ci: float = 0.95) -> Tuple[float, float]:
    """Bootstrap 95% confidence interval for win rate."""
    if len(results) < 5:
        return (0.0, 1.0)

    outcomes = np.array([1 if r["result"] == "TP" else 0 for r in results])
    n = len(outcomes)

    rng = np.random.default_rng(42)
    boot_wrs = np.empty(n_bootstrap)
    for b in range(n_bootstrap):
        sample = rng.choice(outcomes, size=n, replace=True)
        boot_wrs[b] = sample.mean()

    alpha = (1 - ci) / 2
    lo = float(np.percentile(boot_wrs, alpha * 100))
    hi = float(np.percentile(boot_wrs, (1 - alpha) * 100))
    return (lo, hi)


def yearly_breakdown(trades: list) -> Dict[int, dict]:
    """Break trades into yearly stats."""
    by_year = defaultdict(lambda: {"tp": 0, "sl": 0})
    for t in trades:
        year = t["date"].year
        if t["result"] == "TP":
            by_year[year]["tp"] += 1
        else:
            by_year[year]["sl"] += 1
    result = {}
    for year in sorted(by_year.keys()):
        d = by_year[year]
        n = d["tp"] + d["sl"]
        wr = d["tp"] / n if n > 0 else 0
        ev = wr * TP_PIPS - (1 - wr) * SL_PIPS
        result[year] = {"n": n, "tp": d["tp"], "sl": d["sl"], "wr": wr, "ev": ev}
    return result


def rolling_wr(trades: list, window_trades: int = 100) -> List[Tuple[str, float]]:
    """Compute rolling WR over last N trades."""
    if len(trades) < window_trades:
        return []

    sorted_trades = sorted(trades, key=lambda t: t["time"])
    points = []
    for i in range(window_trades, len(sorted_trades) + 1, max(1, window_trades // 4)):
        batch = sorted_trades[max(0, i - window_trades):i]
        tp = sum(1 for t in batch if t["result"] == "TP")
        wr = tp / len(batch)
        last_date = str(batch[-1]["date"])
        points.append((last_date, wr))
    return points


def walk_forward_validate(trades: list, train_months: int = 9,
                          test_months: int = 3, min_fold_trades: int = 15) -> dict:
    """Walk-forward validation returning summary stats."""
    if not trades:
        return {"counted": 0, "passed": 0, "test_wr": 0, "test_n": 0}

    all_months = sorted(set((t['date'].year, t['date'].month) for t in trades))
    if len(all_months) < train_months + test_months:
        return {"counted": 0, "passed": 0, "test_wr": 0, "test_n": 0}

    trades_by_month = defaultdict(list)
    for t in trades:
        trades_by_month[(t['date'].year, t['date'].month)].append(t)

    counted = 0
    passed = 0
    total_test_tp = 0
    total_test_n = 0

    test_start_idx = train_months
    while test_start_idx + test_months <= len(all_months):
        train_keys = set(all_months[:test_start_idx])
        test_keys = set(all_months[test_start_idx:test_start_idx + test_months])

        train_trades = [t for mk in train_keys for t in trades_by_month.get(mk, [])]
        test_trades = [t for mk in test_keys for t in trades_by_month.get(mk, [])]

        train_n = len(train_trades)
        test_n = len(test_trades)

        if test_n >= min_fold_trades:
            counted += 1
            train_tp = sum(1 for t in train_trades if t['result'] == 'TP')
            test_tp = sum(1 for t in test_trades if t['result'] == 'TP')
            train_wr = train_tp / train_n if train_n > 0 else 0
            test_wr = test_tp / test_n if test_n > 0 else 0

            if train_wr > BREAKEVEN_WR and test_wr > BREAKEVEN_WR:
                passed += 1
            total_test_tp += test_tp
            total_test_n += test_n

        test_start_idx += test_months

    # Handle remaining months
    if test_start_idx < len(all_months):
        remaining = all_months[test_start_idx:]
        if len(remaining) >= 1:
            train_keys = set(all_months[:test_start_idx])
            test_keys = set(remaining)
            train_trades = [t for mk in train_keys for t in trades_by_month.get(mk, [])]
            test_trades = [t for mk in test_keys for t in trades_by_month.get(mk, [])]
            test_n = len(test_trades)
            if test_n >= min_fold_trades:
                counted += 1
                train_tp = sum(1 for t in train_trades if t['result'] == 'TP')
                test_tp = sum(1 for t in test_trades if t['result'] == 'TP')
                train_wr = train_tp / len(train_trades) if train_trades else 0
                test_wr = test_tp / test_n
                if train_wr > BREAKEVEN_WR and test_wr > BREAKEVEN_WR:
                    passed += 1
                total_test_tp += test_tp
                total_test_n += test_n

    overall_test_wr = total_test_tp / total_test_n if total_test_n > 0 else 0

    return {
        "counted": counted,
        "passed": passed,
        "test_wr": overall_test_wr,
        "test_n": total_test_n,
    }


def analyse_window_group(pair: str, window_label: str, trades: list,
                         n_total_comparisons: int, min_folds: int = 3) -> Optional[dict]:
    """Full analysis of one pair-window combination."""
    n = len(trades)
    if n < 30:
        return None

    tp = sum(1 for t in trades if t["result"] == "TP")
    sl = n - tp
    wr = tp / n
    ev = wr * TP_PIPS - (1 - wr) * SL_PIPS
    pf = (tp * TP_PIPS) / (sl * SL_PIPS) if sl > 0 else float('inf')

    # Statistical significance
    p_raw = binomial_pvalue(tp, n, BREAKEVEN_WR)
    p_bonf = min(p_raw * n_total_comparisons, 1.0)

    # Bootstrap CI
    ci_lo, ci_hi = bootstrap_wr_ci(trades)

    # Walk-forward validation
    wf = walk_forward_validate(trades)

    # Yearly breakdown
    years = yearly_breakdown(trades)

    # Regime stability: count years with WR > breakeven
    years_above = sum(1 for y in years.values() if y["wr"] > BREAKEVEN_WR and y["n"] >= 10)
    years_total = sum(1 for y in years.values() if y["n"] >= 10)
    regime_stability = years_above / years_total if years_total > 0 else 0

    # Rolling WR (detect decay)
    rolling = rolling_wr(trades, window_trades=min(100, n // 3))
    if len(rolling) >= 4:
        # Compare first half vs second half of rolling points
        mid = len(rolling) // 2
        first_half_wr = np.mean([r[1] for r in rolling[:mid]])
        second_half_wr = np.mean([r[1] for r in rolling[mid:]])
        wr_trend = second_half_wr - first_half_wr
    else:
        wr_trend = 0.0
        first_half_wr = wr
        second_half_wr = wr

    return {
        "pair": pair,
        "window": window_label,
        "n": n, "tp": tp, "sl": sl,
        "wr": wr, "ev": ev, "pf": pf,
        "p_raw": p_raw,
        "p_bonf": p_bonf,
        "ci_lo": ci_lo, "ci_hi": ci_hi,
        "ci_lo_above_be": ci_lo > BREAKEVEN_WR,
        "wf_counted": wf["counted"],
        "wf_passed": wf["passed"],
        "wf_test_wr": wf["test_wr"],
        "wf_test_n": wf["test_n"],
        "wf_pass_rate": wf["passed"] / wf["counted"] if wf["counted"] > 0 else 0,
        "regime_stability": regime_stability,
        "years_above": years_above,
        "years_total": years_total,
        "years": years,
        "wr_trend": wr_trend,
        "first_half_wr": first_half_wr,
        "second_half_wr": second_half_wr,
    }


def print_analysis(results: list, window_minutes: int, show_all: bool = False):
    """Print the full analysis for a given window size."""
    print(f"\n{'='*140}")
    print(f"  WINDOW SIZE: {window_minutes}-MINUTE BLOCKS")
    print(f"{'='*140}")

    n_windows_per_day = (24 * 60) // window_minutes

    # Filter to significant results
    significant = [r for r in results if r["p_bonf"] < 0.05 and r["wr"] > BREAKEVEN_WR]
    marginal = [r for r in results if 0.05 <= r["p_bonf"] < 0.20 and r["wr"] > BREAKEVEN_WR]
    validated = [r for r in results if r["wf_counted"] >= 3 and r["wf_pass_rate"] >= 0.60
                 and r["wf_test_wr"] > BREAKEVEN_WR]

    print(f"\n  Total pair-window combinations tested: {len(results)}")
    print(f"  Bonferroni-significant (p<0.05): {len(significant)}")
    print(f"  Marginally significant (p<0.20): {len(marginal)}")
    print(f"  Walk-forward validated (>=60% folds pass, >=3 folds): {len(validated)}")
    print(f"  BOTH significant AND validated: {len([r for r in results if r['p_bonf'] < 0.05 and r in validated])}")

    # Print header
    print(f"\n  {'Pair':<10} {'Window':<16} {'N':>5} {'WR%':>6} {'EV':>6} {'PF':>5} | "
          f"{'p_raw':>8} {'p_bonf':>8} | {'95% CI':>13} {'CI>BE':>5} | "
          f"{'WF Folds':>8} {'WF Rate':>7} {'WF OOS':>6} | "
          f"{'Regime':>6} {'Trend':>6} | {'Verdict'}")
    print(f"  {'-'*155}")

    # Determine which results to show
    if show_all:
        to_show = sorted(results, key=lambda r: r["ev"], reverse=True)
    else:
        # Show validated + significant + marginal
        to_show_set = set()
        for r in significant + marginal + validated:
            to_show_set.add((r["pair"], r["window"]))
        to_show = sorted(
            [r for r in results if (r["pair"], r["window"]) in to_show_set],
            key=lambda r: r["ev"], reverse=True
        )

    for r in to_show:
        # Determine verdict
        verdicts = []
        if r["p_bonf"] < 0.05 and r["ci_lo_above_be"] and r["wf_counted"] >= 3 and r["wf_pass_rate"] >= 0.60 and r["regime_stability"] >= 0.6:
            verdict = "STRONG"
        elif r["p_bonf"] < 0.05 and r["wf_counted"] >= 3 and r["wf_pass_rate"] >= 0.60:
            verdict = "GOOD"
        elif r["p_bonf"] < 0.20 and r["wf_counted"] >= 2 and r["wf_pass_rate"] >= 0.50:
            verdict = "WEAK"
        elif r["wr"] > BREAKEVEN_WR and r["n"] >= 100:
            verdict = "unvalidated"
        else:
            verdict = "noise"

        if r["wr_trend"] < -0.05:
            verdict += " DECAY"

        sig_str = f"{r['p_raw']:.1e}" if r['p_raw'] < 0.001 else f"{r['p_raw']:.4f}"
        bonf_str = f"{r['p_bonf']:.4f}" if r['p_bonf'] < 1.0 else "1.0000"

        wf_folds = f"{r['wf_passed']}/{r['wf_counted']}" if r['wf_counted'] > 0 else "n/a"
        wf_rate = f"{r['wf_pass_rate']*100:.0f}%" if r['wf_counted'] > 0 else "n/a"
        wf_oos = f"{r['wf_test_wr']*100:.0f}%" if r['wf_test_n'] > 0 else "n/a"

        regime = f"{r['years_above']}/{r['years_total']}"
        trend = f"{r['wr_trend']:+.1%}"

        ci_str = f"{r['ci_lo']*100:.1f}-{r['ci_hi']*100:.1f}%"

        print(f"  {r['pair']:<10} {r['window']:<16} {r['n']:>5} {r['wr']*100:>5.1f}% "
              f"{r['ev']:>+5.1f}p {r['pf']:>5.2f} | "
              f"{sig_str:>8} {bonf_str:>8} | {ci_str:>13} {'YES' if r['ci_lo_above_be'] else 'no':>5} | "
              f"{wf_folds:>8} {wf_rate:>7} {wf_oos:>6} | "
              f"{regime:>6} {trend:>6} | {verdict}")

    return validated, significant


def print_yearly_detail(results: list):
    """Print yearly breakdown for the best validated windows."""
    validated = [r for r in results if r["wf_counted"] >= 3 and r["wf_pass_rate"] >= 0.60
                 and r["wf_test_wr"] > BREAKEVEN_WR and r["p_bonf"] < 0.20]

    if not validated:
        print("\n  No windows passed both statistical and walk-forward validation.")
        return

    print(f"\n{'='*140}")
    print(f"  YEARLY REGIME STABILITY (validated windows only)")
    print(f"{'='*140}")

    for r in sorted(validated, key=lambda x: x["ev"], reverse=True):
        print(f"\n  {r['pair']} {r['window']}  (N={r['n']}, WR={r['wr']*100:.1f}%, EV={r['ev']:+.1f}p)")
        print(f"  {'Year':<8} {'N':>5} {'TP':>5} {'SL':>5} {'WR%':>7} {'EV(p)':>8} {'Status'}")
        print(f"  {'-'*50}")

        for year in sorted(r["years"].keys()):
            y = r["years"][year]
            status = "ABOVE" if y["wr"] > BREAKEVEN_WR else "BELOW" if y["n"] >= 10 else "low-n"
            print(f"  {year:<8} {y['n']:>5} {y['tp']:>5} {y['sl']:>5} {y['wr']*100:>6.1f}% {y['ev']:>+7.1f}p  {status}")


def print_global_vb_test(all_trades: list):
    """Test the global VB strategy edge (no window selection)."""
    if not all_trades:
        return

    print(f"\n{'='*140}")
    print(f"  GLOBAL STRATEGY TEST (no window selection — is VB's edge real?)")
    print(f"{'='*140}")

    n = len(all_trades)
    tp = sum(1 for t in all_trades if t["result"] == "TP")
    wr = tp / n
    ev = wr * TP_PIPS - (1 - wr) * SL_PIPS

    p_val = binomial_pvalue(tp, n, BREAKEVEN_WR)
    ci_lo, ci_hi = bootstrap_wr_ci(all_trades, n_bootstrap=50000)

    z = (wr - BREAKEVEN_WR) / np.sqrt(BREAKEVEN_WR * (1 - BREAKEVEN_WR) / n)

    print(f"\n  Total trades: {n:,}")
    print(f"  Win Rate: {wr*100:.2f}%")
    print(f"  EV/trade: {ev:+.2f} pips")
    print(f"  Z-score vs breakeven: {z:.1f}")
    print(f"  P-value: {p_val:.2e}")
    print(f"  95% CI: [{ci_lo*100:.2f}%, {ci_hi*100:.2f}%]")
    print(f"  CI lower bound above breakeven (60%): {'YES' if ci_lo > BREAKEVEN_WR else 'NO'}")

    # Per-year
    years = yearly_breakdown(all_trades)
    print(f"\n  {'Year':<8} {'N':>6} {'WR%':>7} {'EV(p)':>8}")
    print(f"  {'-'*35}")
    for year in sorted(years.keys()):
        y = years[year]
        print(f"  {year:<8} {y['n']:>6} {y['wr']*100:>6.1f}% {y['ev']:>+7.1f}p")

    years_above = sum(1 for y in years.values() if y["wr"] > BREAKEVEN_WR and y["n"] >= 50)
    years_total = sum(1 for y in years.values() if y["n"] >= 50)
    print(f"\n  Years above breakeven (n>=50): {years_above}/{years_total}")

    # Per-pair
    by_pair = defaultdict(lambda: {"tp": 0, "sl": 0})
    for t in all_trades:
        if t["result"] == "TP":
            by_pair[t["pair"]]["tp"] += 1
        else:
            by_pair[t["pair"]]["sl"] += 1

    print(f"\n  {'Pair':<10} {'N':>6} {'WR%':>7} {'EV(p)':>8} {'p-value':>10}")
    print(f"  {'-'*48}")
    for pair in sorted(by_pair.keys()):
        d = by_pair[pair]
        pn = d["tp"] + d["sl"]
        pw = d["tp"] / pn if pn > 0 else 0
        pe = pw * TP_PIPS - (1 - pw) * SL_PIPS
        pp = binomial_pvalue(d["tp"], pn, BREAKEVEN_WR)
        print(f"  {pair:<10} {pn:>6} {pw*100:>6.1f}% {pe:>+7.1f}p {pp:>9.2e}")


def print_window_stability_question(all_trades: list, window_minutes_list: list):
    """Answer: should windows be periodically re-optimized?"""
    print(f"\n{'='*140}")
    print(f"  SHOULD WINDOWS BE PERIODICALLY RE-OPTIMIZED?")
    print(f"{'='*140}")

    print(f"""
  Analysis of temporal stability:

  1. STRUCTURAL EDGES (session overlaps, volatility patterns) are persistent.
     London-NY overlap has been the highest-volume period for decades.
     Asian session has consistently different volatility characteristics.
     These don't change quarter to quarter.

  2. REGIME SHIFTS happen on multi-year timescales:
     - Central bank policy changes (e.g., BoJ yield curve control end)
     - Market microstructure changes (faster algos, new venues)
     - Geopolitical regime shifts

  3. RECOMMENDATION:
     - Re-run the optimizer every 6-12 months with the latest data
     - Use it as a HEALTH CHECK, not as a reason to constantly re-fit
     - If a validated window's rolling WR drops below 58% over 3+ months,
       consider disabling it
     - NEVER re-optimize more frequently than quarterly — that's curve fitting

  4. WARNING SIGNS that a window edge is dying:
     - 3 consecutive months below breakeven
     - Rolling 100-trade WR below 55%
     - Regime stability (years above BE) dropping below 50%
""")

    # Show rolling WR trend for each pair globally
    by_pair = defaultdict(list)
    for t in all_trades:
        by_pair[t["pair"]].append(t)

    print(f"  ROLLING WR TREND BY PAIR (last 200 trades vs first 200 trades):")
    print(f"  {'Pair':<10} {'First 200 WR':>12} {'Last 200 WR':>12} {'Change':>8} {'Signal'}")
    print(f"  {'-'*55}")

    for pair in sorted(by_pair.keys()):
        trades = sorted(by_pair[pair], key=lambda t: t["time"])
        if len(trades) < 400:
            continue
        first_200 = trades[:200]
        last_200 = trades[-200:]
        wr_first = sum(1 for t in first_200 if t["result"] == "TP") / 200
        wr_last = sum(1 for t in last_200 if t["result"] == "TP") / 200
        change = wr_last - wr_first
        signal = "STABLE" if abs(change) < 0.05 else ("IMPROVING" if change > 0 else "DECLINING")
        print(f"  {pair:<10} {wr_first*100:>11.1f}% {wr_last*100:>11.1f}% {change:>+7.1%}  {signal}")


def main():
    parser = argparse.ArgumentParser(description="V3 Result Validator — Bonferroni, wider windows, regime stability")
    parser.add_argument("--checkpoint-dir", required=True,
                        help="Path to V2 checkpoint directory (e.g., .optimizer_checkpoints/run_874596)")
    parser.add_argument("--window-sizes", nargs="+", type=int, default=[30, 60, 120],
                        help="Window sizes in minutes to test (default: 30 60 120)")
    parser.add_argument("--min-trades", type=int, default=50,
                        help="Min trades for a pair-window to be analysed (default: 50)")
    parser.add_argument("--show-all", action="store_true",
                        help="Show all pair-windows, not just significant/validated ones")
    parser.add_argument("--wf-train", type=int, default=9,
                        help="Walk-forward training months (default: 9)")
    parser.add_argument("--wf-test", type=int, default=3,
                        help="Walk-forward test months (default: 3)")
    parser.add_argument("--min-fold-trades", type=int, default=15,
                        help="Min trades per WF test fold (default: 15)")
    parser.add_argument("--tp", type=int, default=None,
                        help="Take-profit in pips (overrides default 40)")
    parser.add_argument("--sl", type=int, default=None,
                        help="Stop-loss in pips (overrides default 60)")
    args = parser.parse_args()

    # Allow TP/SL override from CLI
    global TP_PIPS, SL_PIPS, BREAKEVEN_WR
    if args.tp is not None:
        TP_PIPS = args.tp
    if args.sl is not None:
        SL_PIPS = args.sl
    BREAKEVEN_WR = SL_PIPS / (TP_PIPS + SL_PIPS)

    print("=" * 140)
    print(f"  V3 RESULT VALIDATOR — Statistical rigour check ({TP_PIPS}/{SL_PIPS} TP/SL)")
    print("=" * 140)

    # Load checkpoints
    print(f"\n  Loading checkpoints from: {args.checkpoint_dir}")
    pair_results = load_all_checkpoints(args.checkpoint_dir)

    if not pair_results:
        print("ERROR: No checkpoint files found.")
        sys.exit(1)

    # Combine all trades
    all_trades = []
    for pair, trades in pair_results.items():
        all_trades.extend(trades)

    print(f"\n  Total trades loaded: {len(all_trades):,} across {len(pair_results)} pairs")

    # Global strategy test (no window selection)
    print_global_vb_test(all_trades)

    # Analyse each window size
    for window_minutes in args.window_sizes:
        n_windows_per_day = (24 * 60) // window_minutes
        n_pairs = len(pair_results)
        n_comparisons = n_windows_per_day * n_pairs

        print(f"\n  Analysing {window_minutes}-minute windows: "
              f"{n_windows_per_day} windows/day x {n_pairs} pairs = {n_comparisons} comparisons")

        all_window_results = []

        for pair, trades in sorted(pair_results.items()):
            # Group trades by wider window
            by_window = defaultdict(list)
            for t in trades:
                wl = assign_wider_window(t, window_minutes)
                by_window[wl].append(t)

            for window_label, window_trades in sorted(by_window.items()):
                if len(window_trades) < args.min_trades:
                    continue

                result = analyse_window_group(
                    pair, window_label, window_trades, n_comparisons
                )
                if result:
                    all_window_results.append(result)

        print_analysis(all_window_results, window_minutes, show_all=args.show_all)
        print_yearly_detail(all_window_results)

    # Window stability analysis
    print_window_stability_question(all_trades, args.window_sizes)

    print(f"\n{'='*140}")
    print(f"  DONE")
    print(f"{'='*140}")


if __name__ == "__main__":
    main()
