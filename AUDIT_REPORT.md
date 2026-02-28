# Forensic Audit Report: forex_bot_40_60

**Date:** 2026-02-28
**Period Under Review:** 2026-02-06 to 2026-02-28 (22 days)
**Bot Version:** forex_bot_40_60.py (last modified 2026-02-18)
**Account Currency:** GBP
**Broker:** OANDA (Practice account, 50:1 leverage)

---

## 1. EXECUTIVE SUMMARY

The 40/60 bot has **lost -£4,137 (-24.35%)** over 22 days, falling from £16,993 to £12,856. A single catastrophic event on Feb 9th wiped £4,396 (25.7% of peak equity) in one snapshot interval, which accounts for essentially the entire loss. Excluding that event, the bot has been roughly flat with a slight negative drift.

The bot is generating **massive volumes of signals** (~12,400 in 22 days) but the signals log records **zero of them as "traded"** — a logging/tracking disconnect. Meanwhile, the performance log shows ~46 identifiable balance-change events (22 wins, 24 losses), confirming trades ARE being executed on OANDA.

**Key verdict: The bot has structural problems in risk management, a fatal logging gap, and a mathematically unfavourable core strategy design (negative R:R with low win rate).**

---

## 2. STRATEGY ARCHITECTURE

### 2.1 Core Concept — "40/60 TP/SL"
- **Take Profit:** 40 pips (static)
- **Stop Loss:** 60 pips (static)
- **Risk:Reward Ratio:** 0.67:1 (inverted — risking more than potential gain)
- **Stated expectation:** "70 trades, £66,362 profit, 28.6% win rate" from backtest

The bot's header claims this is a "90% win rate config" and simultaneously a "28.6% win rate" system. These are contradictory. The backtest-derived expectation of £66,362 profit from 70 trades at 28.6% win rate is mathematically impossible with a 40/60 TP/SL — at 28.6% WR with R:R 0.67, you lose money (expected value per trade is negative).

**Break-even win rate required:** With 40-pip TP and 60-pip SL: `60 / (40 + 60) = 60%`. The bot needs a **60% win rate** just to break even, before accounting for spread and slippage.

### 2.2 Active Strategies (3 of 5 original)
1. **Trend Following** — 88.8% of all signals (11,010 of 12,392)
2. **Momentum Pullback** — 9.8% (1,215 signals)
3. **Volatility Breakout** — 1.3% (167 signals)

Two strategies were intentionally disabled:
- `mean_reversion_signal` — "structurally broken (shorts into bullish MTF)"
- `order_flow_signal` — "fired 13 times in 3 months, not worth the risk"

### 2.3 Entry Filters (Layered)
The bot has a substantial filter stack that must ALL pass:
1. **Market tradeability:** ATR 5-100 pips, spread acceptable, session quality > 0.5
2. **Time window filter:** Currently DISABLED (`USE_TIME_WINDOWS = False`)
3. **Multi-timeframe alignment:** |MTF bias| >= 0.5 (weighted across M1/M5/M15/H1/H4)
4. **Hurst exponent filter:** H >= 0.52 (only trade trending markets)
5. **Strategy-specific conditions** (MACD cross, pullback patterns, BB breakout etc.)
6. **Confidence threshold:** >= 0.60
7. **R:R minimum:** >= 0.5 (effectively always passes since R:R is fixed at 0.67)
8. **Correlation check:** max 2 positions per correlated group
9. **Position limit:** max 8 positions
10. **Margin pre-check:** need 1.5x estimated margin available
11. **News blackout:** 30-min window around high-impact events

### 2.4 Position Sizing
- Base risk: 1% of account balance per trade
- Adjusted by: confidence (0.5-0.5 scaling), session quality (0.8-1.2 scaling)
- Margin cap: max 60% of balance as total margin, divided across MAX_POSITIONS
- Kelly criterion: DISABLED
- Trailing stops: DISABLED
- Partial profits: DISABLED

---

## 3. PERFORMANCE ANALYSIS

### 3.1 Balance Trajectory
| Metric | Value |
|--------|-------|
| Starting balance (Feb 6) | £16,993.00 |
| Peak balance | £17,105.67 (Feb 9 AM) |
| Trough balance | £12,545.79 (Feb 11 PM) |
| Ending balance (Feb 28) | £12,855.75 |
| Total P&L | **-£4,137.25 (-24.35%)** |
| Max drawdown (peak-to-trough) | **£4,559.88 (26.66%)** |

### 3.2 The Catastrophic Event — Feb 9, 2026
Between 09:06 and 12:11 on Feb 9th, the account dropped from £17,105.67 to £12,709.80 — a **£4,395.87 loss** in roughly 3 hours. This single event accounts for **106%** of total losses (the bot has been slightly profitable otherwise).

This was likely **multiple positions stopping out simultaneously**. The bot was running with 1 open position at start, hit a TP (+£112.67), then suffered this catastrophic multi-stop-out. This points to:
- **Correlated positions** being stopped out together
- **Gap/volatility event** blowing through multiple stop losses
- The correlation protection (max 2 per group) was insufficient

### 3.3 Trade-Level Statistics
From balance change analysis (reconstructed since the bot's own tracking shows 0 trades):

| Metric | Value |
|--------|-------|
| Win events | 22 |
| Loss events | 24 |
| Win rate | 47.8% (below 60% breakeven) |
| Average win | £116.06 |
| Average loss | £278.73 (excl. catastrophe: ~£118) |
| Profit factor (excl. catastrophe) | ~1.08 |
| Profit factor (incl. catastrophe) | 0.38 |
| Total won | £2,553.28 |
| Total lost | £6,689.61 |

### 3.4 Position Holding Pattern
The bot maintains 4-5 open positions 82% of the time:

| Positions | Time % |
|-----------|--------|
| 1 | 1.2% |
| 2 | 0.1% |
| 3 | 5.7% |
| 4 | 28.3% |
| 5 | 53.7% |
| 6 | 11.1% |

This persistent 4-6 position exposure amplifies correlated risk.

---

## 4. SIGNAL ANALYSIS

### 4.1 Signal Volume
- **12,392 signals** generated over ~19 trading days
- Average **~650 signals/day** (one every ~2 minutes during active hours)
- **Zero signals recorded as "traded"** — critical logging bug

### 4.2 Signal Distribution by Pair
| Pair | Signals | % |
|------|---------|---|
| USD_JPY | 3,623 | 29.2% |
| GBP_USD | 3,013 | 24.3% |
| EUR_USD | 1,528 | 12.3% |
| AUD_USD | 1,275 | 10.3% |
| AUD_JPY | 1,252 | 10.1% |
| EUR_JPY | 1,060 | 8.6% |
| USD_CAD | 510 | 4.1% |
| NZD_USD | 112 | 0.9% |
| EUR_GBP | 19 | 0.2% |

USD_JPY and GBP_USD dominate, which are both high-volatility pairs — this is expected given the ATR and trend filters.

### 4.3 Direction Bias
- **Sell: 6,712 (54.2%)**
- **Buy: 5,680 (45.8%)**

Slight sell bias, but broadly balanced.

### 4.4 Session Distribution
| Session | Signals | % |
|---------|---------|---|
| Tokyo | 3,892 | 31.4% |
| London/NY overlap | 3,641 | 29.4% |
| New York | 2,537 | 20.5% |
| London | 1,790 | 14.4% |
| Tokyo/London overlap | 532 | 4.3% |

Peak signal generation at 14:00-17:00 UTC (London/NY overlap) — roughly 4,000 signals, which makes sense as this is peak volatility.

### 4.5 Confidence Distribution
- All signals above 0.70 (threshold is 0.60)
- **69% between 0.80-0.90**
- **31% between 0.90-0.95** (capped at 0.95)
- Mean confidence: 0.886

The confidence scores are very tightly clustered, suggesting the confidence calculation doesn't differentiate well between opportunities.

---

## 5. CRITICAL BUGS AND ISSUES

### 5.1 CRITICAL: Performance Tracking Shows Zero Trades
The `performance_log.csv` shows `total_trades=0`, `winning_trades=0`, `losing_trades=0` for **ALL 4,563 snapshots**. The bot's internal trade counter is never properly incrementing after the daily reset. This means:
- Win rate calculation is always 0
- Sharpe ratio is always 0.0
- Expectancy is always 0.0
- Kelly fraction is always 0.0
- The bot is flying blind on its own performance

**Root cause:** The `_save_performance_snapshot()` method reads from `self.risk_manager.winning_trades` and `self.risk_manager.losing_trades`, but these are reset daily. Since trades may close after the daily reset boundary, and the bot also resets `daily_trades` at midnight, the counters appear to reset before accumulating meaningful values. Additionally, the `total_trades` in `performance_tracker` increments in `_trading_cycle()` but is read from `metrics['total_trades']` which equals `performance_tracker['total_trades']` — this IS incremented, yet the CSV shows 0. This suggests the performance snapshot saves happen before trades execute within the same cycle, or there's a race condition.

### 5.2 CRITICAL: Signal Log Shows Zero Trades
All 12,392 signals are logged as `traded=False` with no reason given. The signal record is created with `traded=False`, and the code does attempt to update it via `signal['_signal_record'].traded = True` on successful execution (line 3391), but the CSV is written at signal creation time (line 2378), before the trade executes. The CSV is append-only — it never goes back and updates the signal record after trade execution.

### 5.3 HIGH: Daily Analysis JSONs Are Empty
All 5 daily analysis JSONs (Feb 24-28) show:
- `trades_today: 0`
- `win_rate: 0`
- `sharpe_ratio: 0.0`
- `trades: []`, `signals: []`
- Recommendations always: "Win rate below 40%", "Profit factor below 1.5"

These are generated from in-memory lists that are never properly populated after restarts. Each time the bot restarts, the in-memory `trades_log` and `signals_log` lists are empty.

### 5.4 MEDIUM: Contradictory Strategy Description
The file header says "90% win rate" but the trade window analysis says "28.6% win rate". Neither matches the actual observed ~48% win rate. The backtest expectations are fundamentally unreliable.

### 5.5 MEDIUM: Insufficient Margin Protection Against Correlated Losses
The catastrophic -£4,396 loss on Feb 9 shows the correlation protection (max 2 per correlated group) is inadequate. When 3-5 positions are all in the same directional trade and a large market move hits, all stop losses trigger simultaneously, creating an effective 3-5x risk multiplier.

### 5.6 HIGH: 94.7% of Signals Use Stale/Cached Indicator Data
11,733 out of 12,392 signals (94.7%) share duplicate indicator readings with other signals for the same symbol. The bot caches market data for 30 seconds (`cache_timeout = 30`) but generates signals every ~70 seconds on average. Combined with the rapid-fire signal generation pattern, many signals are generated from the same cached indicator snapshot, producing redundant signals that add no new information.

### 5.7 HIGH: Contra-Indicator Signals Being Generated
- **893 buy signals** generated when RSI > 70 AND Stochastic > 80 (overbought)
- **1,305 sell signals** generated when RSI < 30 AND Stochastic < 20 (oversold)
- 18.3% of buy signals have overbought RSI; 21.4% of sell signals have oversold RSI

The trend-following strategy ignores overbought/oversold conditions, effectively buying into exhaustion.

### 5.8 MEDIUM: Market Regime Detection Non-Functional
12,390 of 12,392 signals report `regime: neutral`. Only 2 signals ever detected `risk_off`. The regime detection (based on USD_JPY 24-hour H1 change) has thresholds that are almost never triggered.

### 5.9 MEDIUM: Order Flow Always Zero
99.8% of signals show `order_flow: 0.0`. The order flow calculation depends on buying pressure diverging from 50% and VWAP deviation, but with M1 60-bar data, it almost always returns 0.

### 5.10 LOW: Hurst Threshold Potentially Too Low
The Hurst threshold of 0.52 is described as "conservative" but is only marginally above random walk (0.50). A threshold of 0.55-0.60 would provide stronger trending confirmation.

### 5.11 LOW: Session Quality is Hardcoded, Not Dynamic
Session quality values are fixed lookup values per session type (tokyo=0.7, london=0.9, overlap=1.0, etc.) — they don't reflect actual market conditions during that session.

---

## 6. DAILY ANALYSIS JSON REVIEW (Feb 24-28)

All 5 JSON files show identical structure with zero meaningful data:

| Date | Balance | Open Positions | Trades | Win Rate |
|------|---------|----------------|--------|----------|
| Feb 24 | £12,623.71 | 5 | 0 | 0 |
| Feb 25 | £12,827.19 | 3 | 0 | 0 |
| Feb 26 | £12,918.15 | 4 | 0 | 0 |
| Feb 27 | £12,860.64 | 6 | 0 | 0 |
| Feb 28 | £12,855.75 | 6 | 0 (13 snapshots) | 0 |

The balance is changing between these snapshots (e.g., +£203 from Feb 24 to 25, +£91 from Feb 25 to 26), confirming trades ARE happening, but the bot isn't tracking them internally. The Feb 28 file has 13 hourly performance snapshots all showing identical zero-trade data.

---

## 7. MATHEMATICAL ANALYSIS OF THE 40/60 STRATEGY

### 7.1 Expected Value Per Trade
With TP=40 pips and SL=60 pips:
- Each win: +40 pips (minus spread/slippage, call it ~38 pips net)
- Each loss: -60 pips (plus spread/slippage, call it ~62 pips net)
- Break-even win rate: 62 / (38 + 62) = **62%**

### 7.2 Observed Performance
- Observed win rate: ~48% (22 wins / 46 events)
- Expected value per trade: `0.48 * 38 - 0.52 * 62 = 18.24 - 32.24 = -14.0 pips`
- **The system has negative expected value at its current win rate**

### 7.3 What Would Be Needed
To be profitable with 40/60, you need a win rate of **at least 62%**, ideally 65%+ to have a meaningful edge. The bot's filters (Hurst, MTF alignment, trend following) are not producing a high enough win rate to overcome the inverted R:R.

---

## 8. RECOMMENDATIONS

### 8.1 Immediate (Stop the Bleeding)
1. **PAUSE the bot** until the logging/tracking bugs are fixed — you cannot manage what you cannot measure
2. **Fix the performance tracking** so total_trades, win_rate, and other metrics are persisted across restarts (use CSV/database, not in-memory counters)
3. **Fix signal logging** to record which signals were actually traded
4. **Reduce maximum simultaneous positions to 3-4** to limit correlated loss exposure

### 8.2 Strategic (Fix the Math)
5. **Reassess the 40/60 TP/SL** — either:
   - **Flip to 60/40** (60 TP, 40 SL) to make R:R work in your favour (only need 40% WR to break even)
   - **Use ATR-based dynamic TP/SL** with minimum 1:1 R:R
6. **Tighten entry filters** to increase win rate above 60% if keeping 40/60
7. **Implement maximum portfolio risk cap** — e.g., total risk across all open positions should never exceed 4-5% of account

### 8.3 Infrastructure
8. **Persist trade state to disk** — on restart, the bot loses all trade tracking
9. **Add real-time equity monitoring** with automatic shutdown if drawdown exceeds X%
10. **Log actual OANDA trade IDs** to the signals log for reconciliation
11. **Reduce signal generation frequency** — 650/day is excessive and likely creating unnecessary API load

---

## 9. FILES REVIEWED

| File | Size | Description |
|------|------|-------------|
| `forex_bot_40_60.py` | 4,134 lines | Main bot source code |
| `performance_log.csv` | 4,563 rows | Hourly performance snapshots (Feb 6-28) |
| `signals_log.csv` | 12,392 rows | All generated signals (Feb 9-27) |
| `daily_analysis_20260224.json` | 1,241 bytes | Daily summary (empty data) |
| `daily_analysis_20260225.json` | 1,241 bytes | Daily summary (empty data) |
| `daily_analysis_20260226.json` | 1,241 bytes | Daily summary (empty data) |
| `daily_analysis_20260227.json` | 1,241 bytes | Daily summary (empty data) |
| `daily_analysis_20260228.json` | 8,645 bytes | Daily summary with 13 snapshots (empty data) |

---

*Report generated by forensic code audit on 2026-02-28.*
