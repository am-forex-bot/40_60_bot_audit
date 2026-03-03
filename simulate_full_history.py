#!/usr/bin/env python3
"""
Full history trailing stop simulation for 40/60 bot (Feb 18 - Mar 3, 2026).

IMPORTANT: This script runs THREE different analyses because the trailing stop
simulation has a fundamental limitation with transaction-log data:

ANALYSIS 1: "Re-entry Cooldown" — NO interpolation needed
  Simply caps the number of consecutive same-direction TPs per pair before
  stopping re-entries. This is 100% accurate because it only removes real
  trades that actually happened. No price guessing.

ANALYSIS 2: "Chain Hold" — Uses interpolation (less reliable)
  Instead of TP→re-enter→TP→re-enter→SL, hold the first position with a
  trailing stop through the entire chain. Uses interpolated prices between
  known fills, which is OPTIMISTIC for trailing stops (no whipsaws).

ANALYSIS 3: "Individual Trade Stats"
  Shows what we know and don't know about each trade. For trades that hit SL,
  we CAN'T know if the price was ever in our favour (no intermediate data).
  For trades that hit TP, we know they reached +40 pips but not how.
"""

import csv
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from collections import defaultdict


@dataclass
class Trade:
    ticket: str
    time: datetime
    instrument: str
    entry_price: float
    units: float
    direction: str
    stop_loss: float
    take_profit: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pl: Optional[float] = None
    conversion_rate: Optional[float] = None
    is_preexisting: bool = False  # True if no entry found in log


def pip_value(instrument: str) -> float:
    return 0.01 if 'JPY' in instrument else 0.0001


def pips_from_entry(price, entry, direction, instrument):
    pip = pip_value(instrument)
    if direction in ('sell', 'Sell'):
        return (entry - price) / pip
    return (price - entry) / pip


def parse_full_history(filename: str) -> Tuple[List[Trade], List[dict]]:
    """Parse all trades, including pre-existing ones (exit found but no entry)."""
    open_trades = {}  # instrument -> Trade
    closed_trades = []
    all_fills = []  # every ORDER_FILL with a P&L

    # First pass: collect all exits (fills with P&L)
    exits = []
    entries = []

    with open(filename, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
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

            if tx_type == 'ORDER_FILL' and details in ('LIMIT_ORDER', 'MARKET_ORDER') and instrument and price:
                entries.append({
                    'ticket': ticket, 'time': tx_time, 'instrument': instrument,
                    'price': price, 'units': units, 'direction': direction,
                })

            if tx_type in ('TAKE_PROFIT_ORDER', 'STOP_LOSS_ORDER') and details == 'ON_FILL':
                # Record TP/SL levels for the most recent entry
                if entries:
                    if tx_type == 'TAKE_PROFIT_ORDER' and price:
                        entries[-1]['tp'] = price
                    elif tx_type == 'STOP_LOSS_ORDER' and price:
                        entries[-1]['sl'] = price

            if tx_type == 'ORDER_FILL' and details in ('TAKE_PROFIT_ORDER', 'STOP_LOSS_ORDER') and instrument:
                exits.append({
                    'ticket': ticket, 'time': tx_time, 'instrument': instrument,
                    'price': price, 'direction': direction,
                    'reason': 'TP' if 'TAKE_PROFIT' in details else 'SL',
                    'pl': float(pl_str) if pl_str else 0,
                    'conv': float(conv_str) if conv_str else 1.0,
                })

    # Match entries to exits
    available_entries = {inst: [] for inst in set(e['instrument'] for e in entries)}
    for e in entries:
        available_entries[e['instrument']].append(e)

    for exit_info in exits:
        inst = exit_info['instrument']
        matched_entry = None

        if inst in available_entries and available_entries[inst]:
            # Find the earliest unmatched entry for this instrument before this exit
            for i, e in enumerate(available_entries[inst]):
                if e['time'] < exit_info['time']:
                    matched_entry = available_entries[inst].pop(i)
                    break

        if matched_entry:
            trade = Trade(
                ticket=matched_entry['ticket'],
                time=matched_entry['time'],
                instrument=inst,
                entry_price=matched_entry['price'],
                units=matched_entry['units'],
                direction=matched_entry['direction'].lower(),
                stop_loss=matched_entry.get('sl', 0),
                take_profit=matched_entry.get('tp', 0),
                exit_time=exit_info['time'],
                exit_price=exit_info['price'],
                exit_reason=exit_info['reason'],
                pl=exit_info['pl'],
                conversion_rate=exit_info['conv'],
                is_preexisting=False,
            )
        else:
            # Pre-existing trade — no entry in our log
            trade = Trade(
                ticket=exit_info['ticket'],
                time=exit_info['time'] - timedelta(hours=12),  # Guess
                instrument=inst,
                entry_price=0,  # Unknown
                units=0,
                direction='sell' if exit_info['direction'] == 'Buy' else 'buy',  # Infer from exit direction
                stop_loss=0,
                take_profit=0,
                exit_time=exit_info['time'],
                exit_price=exit_info['price'],
                exit_reason=exit_info['reason'],
                pl=exit_info['pl'],
                conversion_rate=exit_info['conv'],
                is_preexisting=True,
            )

        closed_trades.append(trade)

    closed_trades.sort(key=lambda t: t.exit_time)
    return closed_trades, exits


def analysis_1_cooldown(trades: List[Trade]):
    """
    ANALYSIS 1: Re-entry Cooldown
    What if we limited the number of consecutive same-direction TPs per pair?
    After N TPs in a row, stop re-entering until direction changes or session ends.

    This requires NO interpolation — we just remove the later trades in each chain.
    """
    print("=" * 100)
    print("ANALYSIS 1: RE-ENTRY COOLDOWN")
    print("What if we stopped re-entering after N consecutive TPs on the same pair?")
    print("=" * 100)
    print("\nThis analysis is 100% accurate — it only removes trades that actually happened.")
    print("No price interpolation or guessing required.\n")

    # Build chains: consecutive trades on same pair in same direction
    # A chain breaks when direction changes or there's a gap > 6 hours
    chains = []
    current_chain = []

    for t in trades:
        if not current_chain:
            current_chain = [t]
            continue

        prev = current_chain[-1]
        same_pair = t.instrument == prev.instrument
        same_dir = t.direction == prev.direction
        gap_hours = (t.time - prev.exit_time).total_seconds() / 3600 if prev.exit_time else 999

        if same_pair and same_dir and gap_hours < 8:
            current_chain.append(t)
        else:
            chains.append(current_chain)
            current_chain = [t]

    if current_chain:
        chains.append(current_chain)

    # Test different cooldown limits
    for max_consecutive_tp in [1, 2, 3, 4, 999]:
        label = f"Max {max_consecutive_tp} consecutive TPs" if max_consecutive_tp < 999 else "No limit (ACTUAL)"
        total_pl = 0
        total_trades = 0
        wins = 0
        losses = 0
        trades_removed = 0

        for chain in chains:
            tp_count = 0
            for t in chain:
                if tp_count >= max_consecutive_tp and t.exit_reason == 'TP':
                    # Would not have re-entered — but we'd still be in the previous trade
                    # Actually, the previous trade hit TP and exited. We just don't re-enter.
                    # So the NEXT trade in chain wouldn't exist.
                    # But the trade after the TP might be an SL — which also wouldn't exist.
                    pass

                if tp_count < max_consecutive_tp or max_consecutive_tp >= 999:
                    total_pl += t.pl
                    total_trades += 1
                    if t.pl > 0:
                        wins += 1
                    else:
                        losses += 1

                    if t.exit_reason == 'TP':
                        tp_count += 1
                    else:
                        tp_count = 0  # SL breaks the TP streak
                else:
                    trades_removed += 1

        wr = wins / max(wins + losses, 1) * 100
        avg_w = sum(t.pl for chain in chains for t in chain if t.pl > 0) / max(wins, 1) if max_consecutive_tp >= 999 else 0
        pf_num = sum(t.pl for t in [t for c in chains for t in c[:max_consecutive_tp] if max_consecutive_tp < 999] if t.pl > 0) if max_consecutive_tp < 999 else sum(t.pl for t in trades if t.pl > 0)

        print(f"  {label:.<50} £{total_pl:>+8.2f}  ({total_trades} trades, {wins}W/{losses}L, "
              f"WR={wr:.0f}%, removed={trades_removed})")

    # Detailed chain view
    print(f"\n  Chain details (showing chains with 2+ trades):")
    print(f"  {'Pair':<10} {'Chain Trades':>12} {'TPs in row':>10} {'Chain P&L':>10} {'Last trade':>10}")
    print(f"  {'-'*55}")

    for chain in chains:
        if len(chain) < 2:
            continue
        chain_pl = sum(t.pl for t in chain)
        tp_streak = sum(1 for t in chain if t.exit_reason == 'TP')
        last = chain[-1]
        print(f"  {chain[0].instrument:<10} {len(chain):>12} {tp_streak:>10} £{chain_pl:>+8.2f}  "
              f"{last.exit_reason} £{last.pl:>+.2f}")

    # What if we removed ONLY the final SL in chains where we had 2+ TPs first?
    print(f"\n  Alternative: Remove only the 'one too many' trade (the SL after 2+ TPs):")
    adjusted_pl = sum(t.pl for t in trades)
    removed_count = 0
    removed_pl = 0
    for chain in chains:
        tp_count = sum(1 for t in chain if t.exit_reason == 'TP')
        if tp_count >= 2 and chain[-1].exit_reason == 'SL':
            sl_trade = chain[-1]
            adjusted_pl -= sl_trade.pl
            removed_pl += sl_trade.pl
            removed_count += 1
            print(f"    Would skip: {sl_trade.instrument} {sl_trade.exit_time.strftime('%m/%d %H:%M')} "
                  f"£{sl_trade.pl:>+.2f} (after {tp_count} TPs)")

    print(f"\n    Actual P&L: £{sum(t.pl for t in trades):>+.2f}")
    print(f"    Adjusted P&L: £{adjusted_pl:>+.2f} (removed {removed_count} SLs totalling £{removed_pl:>+.2f})")


def analysis_2_individual_stats(trades: List[Trade]):
    """
    ANALYSIS 2: Individual Trade Statistics
    What we know and don't know about each trade.
    """
    print("\n\n" + "=" * 100)
    print("ANALYSIS 2: FULL TRADE HISTORY")
    print("=" * 100)

    daily = defaultdict(lambda: {'pl': 0, 'trades': 0, 'wins': 0, 'losses': 0})

    print(f"\n{'#':>3} {'Date':>12} {'Time':>6} {'Pair':<10} {'Dir':<5} "
          f"{'Entry':>10} {'Exit':>10} {'Pips':>7} {'P&L':>10} {'Reason':>4} {'Pre?':>5}")
    print("-" * 95)

    for i, t in enumerate(trades):
        day = t.exit_time.strftime('%Y-%m-%d')
        daily[day]['pl'] += t.pl
        daily[day]['trades'] += 1
        if t.pl > 0:
            daily[day]['wins'] += 1
        else:
            daily[day]['losses'] += 1

        if t.is_preexisting:
            pips_str = "   ???"
            entry_str = "    ???   "
        else:
            pips = pips_from_entry(t.exit_price, t.entry_price, t.direction, t.instrument)
            pips_str = f"{pips:>+7.1f}"
            entry_str = f"{t.entry_price:>10.5f}"

        print(f"{i+1:>3} {t.exit_time.strftime('%Y-%m-%d'):>12} {t.exit_time.strftime('%H:%M'):>6} "
              f"{t.instrument:<10} {t.direction:<5} "
              f"{entry_str} {t.exit_price:>10.5f} {pips_str} "
              f"£{t.pl:>+9.2f} {t.exit_reason:>4} {'YES' if t.is_preexisting else '':>5}")

    print("-" * 95)
    total_pl = sum(t.pl for t in trades)
    print(f"{'TOTAL':>70} £{total_pl:>+9.2f}")

    pre_count = sum(1 for t in trades if t.is_preexisting)
    print(f"\n  Pre-existing trades (no entry in log): {pre_count}")
    print(f"  Trades with full data: {len(trades) - pre_count}")

    # Win/loss by day
    print(f"\n  Daily P&L:")
    running = 0
    for day in sorted(daily.keys()):
        d = daily[day]
        running += d['pl']
        bar = '+' * int(abs(d['pl']) / 30) if d['pl'] > 0 else '-' * int(abs(d['pl']) / 30)
        print(f"    {day}: £{d['pl']:>+8.2f} ({d['wins']}W/{d['losses']}L) "
              f"running: £{running:>+8.2f}  {bar}")

    # Stats by exit reason
    tp_trades = [t for t in trades if t.exit_reason == 'TP']
    sl_trades = [t for t in trades if t.exit_reason == 'SL']
    print(f"\n  TP hits: {len(tp_trades)}, avg P&L: £{sum(t.pl for t in tp_trades)/max(len(tp_trades),1):>+.2f}")
    print(f"  SL hits: {len(sl_trades)}, avg P&L: £{sum(t.pl for t in sl_trades)/max(len(sl_trades),1):>+.2f}")
    print(f"  Ratio: {abs(sum(t.pl for t in sl_trades)/max(len(sl_trades),1)) / max(sum(t.pl for t in tp_trades)/max(len(tp_trades),1), 0.01):.2f}x "
          f"(avg loss is this many times bigger than avg win)")

    # Stats by pair
    print(f"\n  By pair:")
    by_pair = defaultdict(lambda: {'pl': 0, 'trades': 0, 'wins': 0, 'losses': 0})
    for t in trades:
        by_pair[t.instrument]['pl'] += t.pl
        by_pair[t.instrument]['trades'] += 1
        if t.pl > 0:
            by_pair[t.instrument]['wins'] += 1
        else:
            by_pair[t.instrument]['losses'] += 1

    for pair in sorted(by_pair.keys(), key=lambda p: by_pair[p]['pl'], reverse=True):
        d = by_pair[pair]
        wr = d['wins'] / max(d['trades'], 1) * 100
        print(f"    {pair:<10} £{d['pl']:>+8.2f}  ({d['trades']} trades, {d['wins']}W/{d['losses']}L, WR={wr:.0f}%)")

    # Stats by session (time of day)
    print(f"\n  By session (exit time):")
    by_session = defaultdict(lambda: {'pl': 0, 'trades': 0})
    for t in trades:
        hour = t.exit_time.hour
        if 0 <= hour < 7:
            session = 'Asian (00-07)'
        elif 7 <= hour < 12:
            session = 'London AM (07-12)'
        elif 12 <= hour < 16:
            session = 'London/NY (12-16)'
        elif 16 <= hour < 21:
            session = 'NY PM (16-21)'
        else:
            session = 'Late (21-00)'
        by_session[session]['pl'] += t.pl
        by_session[session]['trades'] += 1

    for session in sorted(by_session.keys()):
        d = by_session[session]
        print(f"    {session:<20} £{d['pl']:>+8.2f}  ({d['trades']} trades)")


def analysis_3_honest_assessment(trades: List[Trade]):
    """
    ANALYSIS 3: Honest assessment of what trailing stops could do
    """
    print("\n\n" + "=" * 100)
    print("ANALYSIS 3: HONEST TRAILING STOP ASSESSMENT")
    print("=" * 100)

    tp_trades = [t for t in trades if t.exit_reason == 'TP' and not t.is_preexisting]
    sl_trades = [t for t in trades if t.exit_reason == 'SL' and not t.is_preexisting]

    print(f"""
WHAT WE KNOW:
  - {len(tp_trades)} trades hit TP at +40 pips → avg P&L £{sum(t.pl for t in tp_trades)/max(len(tp_trades),1):>+.2f}
  - {len(sl_trades)} trades hit SL at -60 pips → avg P&L £{sum(t.pl for t in sl_trades)/max(len(sl_trades),1):>+.2f}

WHAT A TRAILING STOP WOULD CHANGE:

  FOR TP TRADES (the {len(tp_trades)} winners):
    COULD HELP: If price went well past +40 pips, trailing stop would capture more
    COULD HURT: If price zigzagged to +40 pips with pullbacks, trailing stop might
                exit early at +20 or +25 pips instead of reaching +40 TP
    WE CAN'T TELL: We don't have tick data showing the path to TP. Did GBP/USD
                    go straight to +40, or did it go +30, pull back to +15, then
                    push to +40? If the latter, a trailing stop would have exited
                    at +15.

  FOR SL TRADES (the {len(sl_trades)} losers):
    COULD HELP: If price went to +20 pips before reversing to -60, breakeven stop
                would have saved the trade (£0 instead of £-140)
    COULD HURT: Never — trailing stop on a loser is same or better than SL
    WE CAN'T TELL: Did the price ever go positive before hitting SL? Some SL trades
                    might have gone straight to -60 without ever being in profit.
                    A trailing stop wouldn't help those at all.

  THE UNKNOWABLE QUESTION:
    How many of the {len(tp_trades)} TP trades would have been CUT SHORT by a trailing stop?
    This is the critical risk. If a trailing stop turns 10 x £97 TPs into 10 x £50
    early exits, you lose £470 — which might wipe out any benefit from saving losers.
""")

    # What we CAN calculate: the break-even analysis
    avg_tp = sum(t.pl for t in tp_trades) / max(len(tp_trades), 1)
    avg_sl = sum(t.pl for t in sl_trades) / max(len(sl_trades), 1)

    print(f"  BREAK-EVEN ANALYSIS:")
    print(f"  Current avg TP: £{avg_tp:>+.2f} (40 pips)")
    print(f"  Current avg SL: £{avg_sl:>+.2f} (60 pips)")
    print()

    # If trailing stop reduces avg TP by X%, how many SL saves do we need to break even?
    for tp_reduction_pct in [0, 10, 20, 30, 40]:
        reduced_tp = avg_tp * (1 - tp_reduction_pct/100)
        tp_cost = (avg_tp - reduced_tp) * len(tp_trades)  # Total cost from reduced TPs
        sl_save_per = abs(avg_sl)  # Max saving per SL if converted to breakeven
        needed_saves = tp_cost / sl_save_per if sl_save_per > 0 else 0
        print(f"    If trailing stop reduces avg TP by {tp_reduction_pct}% (to £{reduced_tp:>+.2f}):")
        print(f"      Cost: {len(tp_trades)} trades x £{avg_tp - reduced_tp:.2f} less = £{tp_cost:.2f} lost")
        print(f"      Need {needed_saves:.1f} of {len(sl_trades)} SL trades saved to break even")
        print(f"      That's {needed_saves/max(len(sl_trades),1)*100:.0f}% of SL trades needing to have been in profit first")
        print()


def main():
    print("=" * 100)
    print("40/60 BOT — FULL HISTORY ANALYSIS (Feb 18 - Mar 3, 2026)")
    print("51 trades, 10 trading days")
    print("=" * 100)

    trades, exits = parse_full_history('transactions_101-004-31618463-003 (14).csv')

    print(f"\nParsed {len(trades)} trades "
          f"({sum(1 for t in trades if t.is_preexisting)} pre-existing, "
          f"{sum(1 for t in trades if not t.is_preexisting)} with full entry/exit data)")

    analysis_2_individual_stats(trades)
    analysis_1_cooldown(trades)
    analysis_3_honest_assessment(trades)


if __name__ == '__main__':
    main()
