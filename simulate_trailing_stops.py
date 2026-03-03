#!/usr/bin/env python3
"""
Replay March 3 trades through different exit strategies using actual OANDA price data.

We reconstruct the price path for each pair from the transaction log entries
(every fill gives us a real market price at a known time), then simulate
what would have happened with trailing stops vs fixed TP/SL.

KEY CONSTRAINT: We only know prices at transaction timestamps. Between transactions,
we must interpolate. For trades that hit SL/TP, we know the price reached that level
at that exact time. This gives us a conservative but grounded simulation.
"""

import csv
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
import json

@dataclass
class Trade:
    ticket: str
    time: datetime
    instrument: str
    entry_price: float
    units: float
    direction: str  # 'buy' or 'sell'
    stop_loss: float
    take_profit: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pl: Optional[float] = None
    conversion_rate: Optional[float] = None

@dataclass
class PricePoint:
    """A known price at a known time for a given instrument"""
    time: datetime
    price: float
    source: str  # e.g. 'fill', 'tp_hit', 'sl_hit'

def parse_transactions(filename: str) -> Tuple[List[Trade], Dict[str, List[PricePoint]]]:
    """Parse OANDA transaction CSV into trades and price observations"""
    trades = {}  # ticket -> Trade (open trades being built)
    closed_trades = []
    price_observations = {}  # instrument -> [PricePoint]

    pending_orders = {}  # ticket -> order details
    trade_to_ticket = {}  # maps fill ticket to trade for TP/SL matching

    with open(filename, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        # Handle BOM and quoted headers
        for row in reader:
            # Find the TICKET field (may have BOM or quotes)
            ticket = None
            for k, v in row.items():
                if 'TICKET' in k:
                    ticket = v.strip().strip('"')
                    break
            if not ticket:
                continue

            tx_type = row['TRANSACTION TYPE'].strip()
            details = row['DETAILS'].strip()
            instrument = row['INSTRUMENT'].strip()
            time_str = row['TRANSACTION DATE'].strip()

            if not time_str:
                continue

            try:
                tx_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S GMT')
            except:
                continue

            price_str = row['PRICE'].strip()
            price = float(price_str) if price_str else None
            units_str = row['UNITS'].strip()
            units = float(units_str) if units_str else None
            direction = row['DIRECTION'].strip()
            sl_str = row['STOP LOSS'].strip()
            tp_str = row['TAKE PROFIT'].strip()
            pl_str = row['PL'].strip()
            conv_str = row['CONVERSION RATE'].strip()

            # Record price observation for any fill
            if tx_type == 'ORDER_FILL' and instrument and price:
                if instrument not in price_observations:
                    price_observations[instrument] = []

                source = 'fill'
                if 'TAKE_PROFIT' in details:
                    source = 'tp_hit'
                elif 'STOP_LOSS' in details:
                    source = 'sl_hit'

                price_observations[instrument].append(PricePoint(
                    time=tx_time, price=price, source=source
                ))

            # Trade opened (LIMIT_ORDER fill or MARKET_ORDER fill)
            if tx_type == 'ORDER_FILL' and details in ('LIMIT_ORDER', 'MARKET_ORDER') and instrument:
                trade = Trade(
                    ticket=ticket,
                    time=tx_time,
                    instrument=instrument,
                    entry_price=price,
                    units=units,
                    direction=direction.lower() if direction else ('buy' if units > 0 else 'sell'),
                    stop_loss=0.0,
                    take_profit=0.0,
                )
                trades[ticket] = trade

            # TP/SL orders set on fill
            if tx_type in ('TAKE_PROFIT_ORDER', 'STOP_LOSS_ORDER') and details == 'ON_FILL':
                # Find the most recently opened trade
                if trades:
                    last_ticket = list(trades.keys())[-1]
                    if tx_type == 'TAKE_PROFIT_ORDER' and price:
                        trades[last_ticket].take_profit = price
                    elif tx_type == 'STOP_LOSS_ORDER' and price:
                        trades[last_ticket].stop_loss = price

            # Trade closed (TP or SL hit)
            if tx_type == 'ORDER_FILL' and details in ('TAKE_PROFIT_ORDER', 'STOP_LOSS_ORDER') and instrument:
                # Find the matching open trade for this instrument
                matching_ticket = None
                for t_ticket, t in trades.items():
                    if t.instrument == instrument:
                        matching_ticket = t_ticket
                        break

                if matching_ticket:
                    trade = trades.pop(matching_ticket)
                    trade.exit_time = tx_time
                    trade.exit_price = price
                    trade.exit_reason = 'TP' if 'TAKE_PROFIT' in details else 'SL'
                    trade.pl = float(pl_str) if pl_str else 0.0
                    trade.conversion_rate = float(conv_str) if conv_str else 1.0
                    closed_trades.append(trade)

    # Sort price observations by time
    for inst in price_observations:
        price_observations[inst].sort(key=lambda p: p.time)

    return closed_trades, price_observations


def interpolate_price_path(instrument: str, entry_time: datetime, exit_time: datetime,
                           entry_price: float, exit_price: float,
                           all_observations: Dict[str, List[PricePoint]],
                           direction: str) -> List[PricePoint]:
    """
    Build a price path for a trade's lifetime using all known price observations.

    We know prices at:
    - Entry time (fill price)
    - Exit time (TP/SL price)
    - Any other fills on the same instrument between entry and exit
    - Fills on correlated instruments can hint at direction but we won't use those

    Between known points, we linearly interpolate (conservative assumption).
    """
    obs = all_observations.get(instrument, [])

    path = [PricePoint(entry_time, entry_price, 'entry')]

    for o in obs:
        if entry_time < o.time < exit_time:
            path.append(o)

    path.append(PricePoint(exit_time, exit_price, 'exit'))
    path.sort(key=lambda p: p.time)

    # Now interpolate to ~1 minute resolution
    detailed_path = []
    for i in range(len(path) - 1):
        p1 = path[i]
        p2 = path[i + 1]

        total_seconds = (p2.time - p1.time).total_seconds()
        if total_seconds <= 0:
            detailed_path.append(p1)
            continue

        # Add point every 60 seconds
        steps = max(1, int(total_seconds / 60))
        for step in range(steps):
            frac = step / steps
            t = p1.time + timedelta(seconds=total_seconds * frac)
            price = p1.price + (p2.price - p1.price) * frac
            detailed_path.append(PricePoint(t, price, 'interpolated'))

    detailed_path.append(path[-1])
    return detailed_path


def pip_value(instrument: str) -> float:
    """Returns the pip size for a given instrument"""
    if 'JPY' in instrument:
        return 0.01
    return 0.0001


def pips_from_entry(price: float, entry_price: float, direction: str, instrument: str) -> float:
    """Calculate pips of profit/loss from entry"""
    pip = pip_value(instrument)
    if direction in ('sell', 'Sell'):
        return (entry_price - price) / pip
    else:
        return (price - entry_price) / pip


def simulate_trailing_stop(trade: Trade, price_path: List[PricePoint],
                           config: dict) -> dict:
    """
    Simulate a trailing stop strategy on a trade given its price path.

    Config options:
    - 'breakeven_trigger': pips in profit to move SL to breakeven
    - 'trail_trigger': pips in profit to start trailing
    - 'trail_distance': pips behind best price to trail SL
    - 'tighten_trigger': pips in profit to tighten trail
    - 'tighten_distance': tighter trail distance after tighten_trigger
    - 'keep_fixed_tp': if True, also exit at the original fixed TP

    Returns dict with exit_price, exit_time, exit_reason, pips_profit, pl_estimate
    """
    pip = pip_value(trade.instrument)
    direction = trade.direction.lower()
    entry = trade.entry_price
    original_sl = trade.stop_loss
    original_tp = trade.take_profit

    breakeven_trigger = config.get('breakeven_trigger', 20)
    trail_trigger = config.get('trail_trigger', 25)
    trail_distance = config.get('trail_distance', 20)
    tighten_trigger = config.get('tighten_trigger', 40)
    tighten_distance = config.get('tighten_distance', 15)
    keep_fixed_tp = config.get('keep_fixed_tp', False)

    current_sl = original_sl
    best_price = entry
    sl_moved_to_breakeven = False
    trailing_active = False

    for point in price_path:
        price = point.price
        pips_profit = pips_from_entry(price, entry, direction, trade.instrument)

        # Update best price (most favourable)
        if direction in ('sell',):
            if price < best_price:
                best_price = price
        else:
            if price > best_price:
                best_price = price

        best_pips = pips_from_entry(best_price, entry, direction, trade.instrument)

        # Check if fixed TP hit (if keeping it)
        if keep_fixed_tp:
            if direction in ('sell',):
                if price <= original_tp:
                    return {
                        'exit_price': original_tp,
                        'exit_time': point.time,
                        'exit_reason': 'FIXED_TP',
                        'pips_profit': pips_from_entry(original_tp, entry, direction, trade.instrument),
                        'best_pips': best_pips,
                    }
            else:
                if price >= original_tp:
                    return {
                        'exit_price': original_tp,
                        'exit_time': point.time,
                        'exit_reason': 'FIXED_TP',
                        'pips_profit': pips_from_entry(original_tp, entry, direction, trade.instrument),
                        'best_pips': best_pips,
                    }

        # Move SL to breakeven
        if not sl_moved_to_breakeven and pips_profit >= breakeven_trigger:
            if direction in ('sell',):
                current_sl = entry - (2 * pip)  # Slightly better than breakeven
            else:
                current_sl = entry + (2 * pip)
            sl_moved_to_breakeven = True

        # Start trailing
        if best_pips >= trail_trigger:
            trailing_active = True

        if trailing_active:
            dist = tighten_distance if best_pips >= tighten_trigger else trail_distance

            if direction in ('sell',):
                new_sl = best_price + (dist * pip)
                if new_sl < current_sl:  # Only move SL in favorable direction
                    current_sl = new_sl
            else:
                new_sl = best_price - (dist * pip)
                if new_sl > current_sl:
                    current_sl = new_sl

        # Check if current SL hit
        if direction in ('sell',):
            if price >= current_sl and sl_moved_to_breakeven:
                return {
                    'exit_price': current_sl,
                    'exit_time': point.time,
                    'exit_reason': 'TRAILING_SL' if trailing_active else 'BREAKEVEN_SL',
                    'pips_profit': pips_from_entry(current_sl, entry, direction, trade.instrument),
                    'best_pips': best_pips,
                }
            # Original SL still applies if we haven't moved to breakeven
            if not sl_moved_to_breakeven and price >= original_sl:
                return {
                    'exit_price': original_sl,
                    'exit_time': point.time,
                    'exit_reason': 'ORIGINAL_SL',
                    'pips_profit': pips_from_entry(original_sl, entry, direction, trade.instrument),
                    'best_pips': best_pips,
                }
        else:
            if price <= current_sl and sl_moved_to_breakeven:
                return {
                    'exit_price': current_sl,
                    'exit_time': point.time,
                    'exit_reason': 'TRAILING_SL' if trailing_active else 'BREAKEVEN_SL',
                    'pips_profit': pips_from_entry(current_sl, entry, direction, trade.instrument),
                    'best_pips': best_pips,
                }
            if not sl_moved_to_breakeven and price <= original_sl:
                return {
                    'exit_price': original_sl,
                    'exit_time': point.time,
                    'exit_reason': 'ORIGINAL_SL',
                    'pips_profit': pips_from_entry(original_sl, entry, direction, trade.instrument),
                    'best_pips': best_pips,
                }

    # If we reach end of price path without exit, use last known price
    last = price_path[-1]
    return {
        'exit_price': last.price,
        'exit_time': last.time,
        'exit_reason': 'END_OF_DATA',
        'pips_profit': pips_from_entry(last.price, entry, direction, trade.instrument),
        'best_pips': pips_from_entry(best_price, entry, direction, trade.instrument),
    }


def estimate_pl(trade: Trade, pips_profit: float) -> float:
    """Estimate P&L in GBP from pips profit"""
    pip = pip_value(trade.instrument)
    units = abs(trade.units)
    pl_in_instrument_currency = pips_profit * pip * units

    # Use the actual conversion rate from the trade if available
    conv = trade.conversion_rate if trade.conversion_rate else 0.75
    return pl_in_instrument_currency * conv


def main():
    print("=" * 100)
    print("MARCH 3, 2026 — TRAILING STOP SIMULATION")
    print("Replaying actual trades with actual price data from OANDA transaction logs")
    print("=" * 100)

    # Parse both accounts
    trades_40_60, prices_40_60 = parse_transactions('transactions_101-004-31618463-003.csv')
    trades_130_60, prices_130_60 = parse_transactions('transactions_101-004-31618463-001.csv')

    print(f"\n40/60 Bot: {len(trades_40_60)} closed trades on March 3")
    print(f"130/60 Bot: {len(trades_130_60)} closed trades on March 3")

    # === ACTUAL RESULTS ===
    print("\n" + "=" * 100)
    print("SECTION 1: ACTUAL RESULTS (What really happened)")
    print("=" * 100)

    total_pl_actual = 0
    print(f"\n{'#':>2} {'Time':>8} {'Pair':<8} {'Dir':<5} {'Entry':>10} {'Exit':>10} "
          f"{'Pips':>7} {'P&L':>10} {'Reason':<5} {'Duration':>10}")
    print("-" * 95)

    for i, t in enumerate(trades_40_60):
        pips = pips_from_entry(t.exit_price, t.entry_price, t.direction, t.instrument)
        duration = t.exit_time - t.time if t.exit_time else timedelta(0)
        dur_str = f"{int(duration.total_seconds() / 60)}m"
        total_pl_actual += t.pl

        print(f"{i+1:>2} {t.time.strftime('%H:%M'):>8} {t.instrument:<8} {t.direction:<5} "
              f"{t.entry_price:>10.5f} {t.exit_price:>10.5f} {pips:>+7.1f} "
              f"£{t.pl:>+9.2f} {t.exit_reason:<5} {dur_str:>10}")

    print("-" * 95)
    print(f"{'TOTAL':>70} £{total_pl_actual:>+9.2f}")

    # Count wins/losses
    wins_actual = sum(1 for t in trades_40_60 if t.pl > 0)
    losses_actual = sum(1 for t in trades_40_60 if t.pl <= 0)
    print(f"\nWins: {wins_actual}, Losses: {losses_actual}, Win Rate: {wins_actual/(wins_actual+losses_actual)*100:.0f}%")

    # === TRAILING STOP SCENARIOS ===
    configs = {
        'A: Conservative Trail (BE@20, Trail@25/20pip, Tight@40/15pip)': {
            'breakeven_trigger': 20,
            'trail_trigger': 25,
            'trail_distance': 20,
            'tighten_trigger': 40,
            'tighten_distance': 15,
            'keep_fixed_tp': False,
        },
        'B: Aggressive Trail (BE@15, Trail@20/15pip, Tight@35/10pip)': {
            'breakeven_trigger': 15,
            'trail_trigger': 20,
            'trail_distance': 15,
            'tighten_trigger': 35,
            'tighten_distance': 10,
            'keep_fixed_tp': False,
        },
        'C: Fixed TP + Trailing (Keep 40-pip TP, BE@20, Trail@25/20pip)': {
            'breakeven_trigger': 20,
            'trail_trigger': 25,
            'trail_distance': 20,
            'tighten_trigger': 40,
            'tighten_distance': 15,
            'keep_fixed_tp': True,
        },
        'D: Wide Trail (BE@25, Trail@30/25pip, Tight@50/20pip)': {
            'breakeven_trigger': 25,
            'trail_trigger': 30,
            'trail_distance': 25,
            'tighten_trigger': 50,
            'tighten_distance': 20,
            'keep_fixed_tp': False,
        },
        'E: Tight Trail (BE@12, Trail@15/12pip, Tight@25/8pip)': {
            'breakeven_trigger': 12,
            'trail_trigger': 15,
            'trail_distance': 12,
            'tighten_trigger': 25,
            'tighten_distance': 8,
            'keep_fixed_tp': False,
        },
    }

    # Build extended price paths using ALL observations
    # For each trade, we need price data not just to its exit but potentially beyond
    # (since a trailing stop might keep a trade open longer than the original TP)
    # We'll use ALL price observations for the instrument across the whole day

    print("\n" + "=" * 100)
    print("SECTION 2: TRAILING STOP SIMULATIONS")
    print("=" * 100)

    print("\n⚠️  IMPORTANT CAVEATS:")
    print("  1. Price path between known points is LINEAR INTERPOLATION — real prices zigzag more")
    print("  2. Trailing stops in volatile markets get whipsawed — interpolation UNDERSTATES this")
    print("  3. Trades that stayed open longer (no TP exit) may have been affected by spreads,")
    print("     slippage, and price action we can't see in this data")
    print("  4. RE-ENTRY BEHAVIOUR: In scenarios where trailing stop keeps trade open longer,")
    print("     the bot would NOT have re-entered (no double-up). This affects trade count.")
    print("  5. We only simulate the FIRST entry per pair-direction chain, not the re-entries,")
    print("     since re-entries wouldn't exist if the original trade was still open.")

    # Group trades into "chains" — consecutive trades on same pair in same direction
    # The first trade in a chain is the original entry. Subsequent ones are re-entries.
    chains = {}  # instrument -> list of trades in sequence
    for t in trades_40_60:
        key = t.instrument
        if key not in chains:
            chains[key] = []
        chains[key].append(t)

    print(f"\n\nTrade chains identified:")
    for inst, chain_trades in chains.items():
        entries = [f"{t.time.strftime('%H:%M')}({t.exit_reason})" for t in chain_trades]
        print(f"  {inst}: {' → '.join(entries)}")

    # For the simulation, we simulate the FIRST trade in each chain but with the
    # price path extended through the LAST trade's exit time.
    # This shows what would have happened if the bot held the original position.

    for config_name, config in configs.items():
        print(f"\n{'─' * 100}")
        print(f"SCENARIO: {config_name}")
        print(f"{'─' * 100}")

        total_pl_sim = 0
        trade_results = []

        print(f"\n{'#':>2} {'Pair':<8} {'Entry@':>8} {'Dir':<5} "
              f"{'ActualExit':>10} {'ActPips':>7} {'Act£':>9} │ "
              f"{'SimExit':>10} {'SimPips':>7} {'Sim£':>9} {'SimReason':<15} {'BestPips':>8}")
        print("-" * 130)

        trade_num = 0
        for inst, chain_trades in chains.items():
            first_trade = chain_trades[0]
            last_trade = chain_trades[-1]

            # Sum actual P&L across the chain
            chain_actual_pl = sum(t.pl for t in chain_trades)
            chain_actual_pips = sum(pips_from_entry(t.exit_price, t.entry_price, t.direction, t.instrument)
                                   for t in chain_trades)

            # Build price path from first entry through last exit
            # Use all observations for this instrument
            extended_path = interpolate_price_path(
                inst,
                first_trade.time,
                last_trade.exit_time,
                first_trade.entry_price,
                last_trade.exit_price,
                prices_40_60,
                first_trade.direction
            )

            # Simulate trailing stop on first trade with extended path
            result = simulate_trailing_stop(first_trade, extended_path, config)

            # Estimate P&L
            sim_pl = estimate_pl(first_trade, result['pips_profit'])
            total_pl_sim += sim_pl

            trade_num += 1
            actual_pips_str = f"{chain_actual_pips:>+7.1f}"

            print(f"{trade_num:>2} {inst:<8} {first_trade.time.strftime('%H:%M'):>8} "
                  f"{first_trade.direction:<5} "
                  f"  ({len(chain_trades)} trades) {actual_pips_str} £{chain_actual_pl:>+8.2f} │ "
                  f"{result['exit_price']:>10.5f} {result['pips_profit']:>+7.1f} "
                  f"£{sim_pl:>+8.2f} {result['exit_reason']:<15} {result['best_pips']:>+7.1f}")

            # Show individual trades in the chain for reference
            for j, t in enumerate(chain_trades):
                t_pips = pips_from_entry(t.exit_price, t.entry_price, t.direction, t.instrument)
                print(f"   └─ {j+1}. {t.time.strftime('%H:%M')}-{t.exit_time.strftime('%H:%M')} "
                      f"entry={t.entry_price:.5f} exit={t.exit_price:.5f} "
                      f"{t_pips:>+6.1f}pip £{t.pl:>+8.2f} ({t.exit_reason})")

            trade_results.append({
                'instrument': inst,
                'chain_trades': len(chain_trades),
                'actual_pl': chain_actual_pl,
                'actual_pips': chain_actual_pips,
                'sim_pl': sim_pl,
                'sim_pips': result['pips_profit'],
                'sim_reason': result['exit_reason'],
                'best_pips': result['best_pips'],
            })

        print("-" * 130)
        total_actual = sum(r['actual_pl'] for r in trade_results)
        print(f"{'ACTUAL TOTAL:':>70} £{total_actual:>+9.2f}")
        print(f"{'SIMULATED TOTAL:':>70} £{total_pl_sim:>+9.2f}")
        diff = total_pl_sim - total_actual
        print(f"{'DIFFERENCE:':>70} £{diff:>+9.2f} ({'better' if diff > 0 else 'worse'})")

        wins_sim = sum(1 for r in trade_results if r['sim_pl'] > 0)
        losses_sim = sum(1 for r in trade_results if r['sim_pl'] <= 0)
        total_sim_trades = len(trade_results)
        print(f"\nSimulated: {total_sim_trades} chain-trades ({wins_sim}W/{losses_sim}L)")
        print(f"Actual: {len(trades_40_60)} individual trades ({wins_actual}W/{losses_actual}L)")

    # === SUMMARY COMPARISON TABLE ===
    print("\n\n" + "=" * 100)
    print("SECTION 3: SUMMARY COMPARISON")
    print("=" * 100)

    print(f"\n{'Strategy':<60} {'P&L':>10} {'vs Actual':>12} {'Trades':>8}")
    print("-" * 92)
    print(f"{'ACTUAL (40-pip fixed TP, 60-pip SL)':.<60} £{total_pl_actual:>+9.2f} {'baseline':>12} {len(trades_40_60):>8}")

    for config_name, config in configs.items():
        total_sim = 0
        n_trades = 0
        for inst, chain_trades in chains.items():
            first_trade = chain_trades[0]
            last_trade = chain_trades[-1]
            chain_actual_pl = sum(t.pl for t in chain_trades)

            extended_path = interpolate_price_path(
                inst, first_trade.time, last_trade.exit_time,
                first_trade.entry_price, last_trade.exit_price,
                prices_40_60, first_trade.direction
            )
            result = simulate_trailing_stop(first_trade, extended_path, config)
            sim_pl = estimate_pl(first_trade, result['pips_profit'])
            total_sim += sim_pl
            n_trades += 1

        diff = total_sim - total_pl_actual
        print(f"{config_name:.<60} £{total_sim:>+9.2f} £{diff:>+10.2f} {n_trades:>8}")

    # === 130/60 comparison ===
    print(f"\n\n130/60 Bot Comparison:")
    total_130 = 0
    for t in trades_130_60:
        pips = pips_from_entry(t.exit_price, t.entry_price, t.direction, t.instrument)
        print(f"  {t.instrument:<8} {t.direction:<5} {t.time.strftime('%H:%M')}-{t.exit_time.strftime('%H:%M')} "
              f"entry={t.entry_price:.5f} exit={t.exit_price:.5f} "
              f"{pips:>+7.1f}pip £{t.pl:>+9.2f} ({t.exit_reason})")
        total_130 += t.pl
    print(f"  130/60 Total P&L: £{total_130:>+9.2f}")

    # NOTE about limitations
    print("\n\n" + "=" * 100)
    print("SECTION 4: CRITICAL LIMITATIONS OF THIS SIMULATION")
    print("=" * 100)
    print("""
1. LINEAR INTERPOLATION BIAS: Between known price points, we assume straight-line
   movement. Real prices zigzag. This means:
   - Trailing stops may LOOK better than they'd actually perform (no whipsaws in interpolation)
   - Trades that "would have" held through to a better exit might actually have been
     stopped out by an intraday spike we can't see

2. SPREAD NOT MODELLED: Trailing stop exit prices assume mid-price. In reality,
   you'd exit at bid (for sells) or ask (for buys), typically 1-2 pips worse.

3. NO RE-ENTRY MODEL: The simulation holds one position per pair. In reality, the
   bot might re-enter differently with a trailing stop (e.g., if stopped out at
   breakeven, it might re-enter and catch the next leg).

4. SINGLE DAY SAMPLE: This is ONE trading day. Trailing stops may perform very
   differently on range-bound days, choppy days, or news events. A proper backtest
   across weeks/months is needed before changing the live strategy.

5. CONVERSION RATES: P&L estimates use the conversion rate from the original trade.
   Actual rates would vary slightly for different exit times.

6. THE KEY QUESTION: Would trailing stops have ALSO prevented some of the winning
   TP hits? (e.g., if price pulled back 20 pips before eventually hitting TP,
   a trailing stop might have exited early). We CAN'T see this with interpolated data.
""")


if __name__ == '__main__':
    main()
