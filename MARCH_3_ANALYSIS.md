# March 3, 2026 — Trading Day Analysis

**Date:** 2026-03-03
**Market Context:** Strong USD rally driven by geopolitical/war developments. Dollar pushed hard against all majors.
**Bots Compared:** 40/60 (account ...003) vs 130/60 (account ...001)

---

## 1. THE DAY AT A GLANCE

| Metric | 40/60 Bot | 130/60 Bot |
|--------|-----------|------------|
| **Starting balance** | £12,834 | £64,743 |
| **Peak balance** | £13,846 (+£1,012) | £68,194 (+£3,451) |
| **Ending balance** | £13,073 | £66,881 |
| **Net P&L** | **+£239 (+1.9%)** | **+£2,138 (+3.3%)** |
| **Peak drawdown** | -£773 (-5.6%) | -£1,313 (-1.9%) |
| **Wins / Losses** | 13W / 6L (68%) | 3W / 3L (50%) |
| **Avg win** | £~103 | £~1,371 |
| **Avg loss** | £~152 | £~658 |
| **Profit factor** | 1.51 | 2.08 |

**Both bots were profitable.** But the story is very different.

---

## 2. THE 40/60 BOT'S DAY — TRADE BY TRADE

### 2.1 Equity Curve Narrative

```
£12,834 ─┐
          │ 01:23  AUD/USD SL hit (-£125)
£12,710 ──┤ .... flat for 4 hours ....
          │ 06:04  GBP/USD TP (+£86)     ← Dollar rally begins
£12,796 ──┤ 07:42  EUR/USD TP (+£93)
£12,889 ──┤ 08:12  GBP/USD TP (+£86)
£12,975 ──┤
          │ 08:45  AUD/USD SL (-£123)    ← Wrong side of AUD
£12,852 ──┤ 09:24  EUR/USD TP (+£86)
£12,938 ──┤ 10:19  NZD/USD TP (+£104)
          │ 10:38  AUD/USD TP (+£96)     ← Now SHORT AUD, riding the wave
£13,138 ──┤ 11:15  GBP/USD TP (+£104)
£13,242 ──┤
          │ ... 3 hour quiet period (London/NY overlap) ...
          │
£13,338 ──┤ 14:26  EUR/USD TP (+£96)
£13,440 ──┤ 14:42  AUD/USD TP (+£102)
          │
          │ 15:13  *** TRIPLE TP *** AUD, NZD, USD/CAD all hit TP simultaneously
£13,846 ══╪══════════════════════════════════════ PEAK (+£1,012 on the day)
          │
          │ === THE REVERSAL ===
          │
          │ 16:05  AUD/USD SL (-£157)    ← Re-entered SHORT after TP, market snapped back
£13,689 ──┤ 16:44  USD/CAD SL (-£160)
          │ 16:47  GBP/USD SL (-£149)
£13,380 ──┤ 18:33  EUR/USD SL (-£160)
          │ 18:41  NZD/USD SL (-£147)
£13,073 ──┘                               ← Day end. Gave back £773 from peak.
```

### 2.2 The Pattern: TP → Instant Re-entry → TP → Instant Re-entry → ... → SL

The bot's behaviour today was textbook "going to the well one too many times":

| Pair | TP#1 | Re-entry | TP#2 | Re-entry | TP#3 | Re-entry | Result |
|------|------|----------|------|----------|------|----------|--------|
| GBP/USD | 06:04 (+£86) | 06:28 | 08:12 (+£86) | 08:14 | 11:15 (+£104) | 11:16 | **16:47 SL (-£149)** |
| EUR/USD | 07:42 (+£93) | 07:47 | 09:24 (+£86) | 09:24 | 14:26 (+£96) | 14:31¹ | **18:33 SL (-£160)** |
| AUD/USD | — | — | 10:38 (+£96) | 10:48 | 14:42 (+£102) | 14:43 | 15:14 (+£107) → 15:17 → **16:05 SL (-£157)** |
| NZD/USD | 10:19 (+£104) | 10:21 | 15:14 (+£97) | 15:23 | — | — | **18:41 SL (-£147)** |

¹ EUR/USD actually hit TP a 4th time at 15:18 (+£103) before the final SL

**Every single pair followed the same arc:** Multiple TPs riding the USD wave, then a final re-entry that got stopped out when the dollar reversed.

### 2.3 The Critical Problem: Re-entry After Peak Momentum

The bot re-entered positions within **seconds** of hitting TP:
- GBP/USD TP at 11:14:37, re-entered at 11:15:58 (81 seconds later)
- EUR/USD TP at 14:26:01, re-entered at 14:31:14 (5 minutes)
- AUD/USD TP at 15:13:50, re-entered at 15:16:50 (3 minutes)

By the time the bot made its last round of entries (~15:15-15:30), it had been on the same directional trade for **9-12 hours**. The USD move was exhausted. The re-entries at 15:15+ were the "one too many" — but the bot had no concept of momentum exhaustion.

---

## 3. THE 130/60 BOT'S DAY — SAME MARKET, DIFFERENT STORY

The 130/60 bot traded the **exact same pairs** in the **same direction** (short everything vs USD), but its wider TP/SL created a completely different experience:

| Time | Event | P&L |
|------|-------|-----|
| 10:38 | EUR/USD TP hit (130 pips) | **+£1,264** |
| 10:39 | Re-entry EUR/USD short | — |
| 11:16 | GBP/USD TP hit (130 pips) | **+£1,227** |
| 11:18 | Re-entry GBP/USD short | — |
| 12:21 | GBP/USD SL hit (60 pips) | **-£661** |
| 13:46 | Re-entry GBP/USD short | — |
| 15:04 | AUD/USD TP hit (130 pips) | **+£1,622** |
| 15:06 | Re-entry AUD/USD short | — |
| 16:45 | AUD/USD SL hit | **-£666** |
| 18:41 | GBP/USD SL hit | **-£647** |

**Key differences:**
- Only **6 trades** vs the 40/60 bot's **19 trades**
- Each TP captured **£1,200-1,600** vs the 40/60 bot's **£86-107**
- Both bots had the same 3 SL hits in the afternoon, but the 130/60 bot had much more profit to absorb them
- **Profit factor 2.08** vs 1.51 — the wider TP captured the trend better

### 3.1 Why 130/60 Outperformed Today

The USD rally today moved roughly **200-400 pips** on the major pairs (GBP/USD dropped from ~1.340 to ~1.327 intraday). The 130/60 bot captured **130 pips** per trade in a single swing. The 40/60 bot had to enter and exit **3-4 times** on the same pair to capture a similar total — and each re-entry was a new risk event.

**The 40/60 bot turned one good trade into four mediocre trades**, with the last one being a loser that partially wiped out the previous three.

---

## 4. THE MISSED OPPORTUNITY — TRADES DEEP IN THE GREEN

This is what you were watching. Here are the trades that were **significantly in the green** but either:
- Waited for the full 40-pip TP when they could have banked profit earlier
- Hit TP, re-entered, and then gave it all back on the SL

### 4.1 GBP/USD — The Most Painful Example

The bot entered short GBP/USD at 03:23 at 1.33982. GBP/USD eventually dropped to ~1.327. That's a **130-pip move** in the bot's favour.

What the 40/60 bot did with this move:
```
Entry at 1.33982
  → TP at 1.33571 (+41 pips, +£86)     captured
  → Re-entry 1.33624
  → TP at 1.33213 (+41 pips, +£86)     captured
  → Re-entry 1.33162
  → TP at 1.32750 (+41 pips, +£104)    captured
  → Re-entry 1.32709
  → SL at 1.33304 (-60 pips, -£149)    LOST

  Net from 130-pip move: +£127 (captured ~25% of available profit)
```

What the 130/60 bot got from the same move:
```
Entry (pre-existing)
  → TP (+130 pips, +£1,227)            captured in one shot

  Net: +£1,227
```

Even accounting for account size difference (130/60 bot has 5x capital), on a per-unit basis the 130/60 bot captured **far more** of the move. The 40/60 bot's repeated TP/re-entry cycle skimmed the surface of a deep trend, then lost a chunk on the reversal.

### 4.2 AUD/USD — Similar Story

AUD/USD dropped from ~0.712 to ~0.694 (180+ pips). The 40/60 bot:
- Took 3 x 40-pip TPs = ~£303 profit
- Then got stopped out for -£157
- **Net: +£146** from a 180-pip move

### 4.3 EUR/USD — Four TPs Then a Loss

EUR/USD dropped from ~1.165 to ~1.154 (~110 pips). The 40/60 bot:
- Took 4 x 40-pip TPs = ~£379 profit
- Then got stopped out for -£160
- **Net: +£219** from a 110-pip move

---

## 5. WHAT CAN BE DONE — THE SOLUTIONS

The fundamental problem is clear: **fixed TP in a trending market leaves money on the table, and automatic re-entry after TP compounds the risk of catching the reversal.**

Here are the concrete solutions, ranked by impact:

### 5.1 TRAILING STOP (Highest Impact)

**The idea:** Once a trade is in profit by X pips, start trailing the stop loss behind the price. If the trend continues, you ride it. If it reverses, you exit with profit.

**Implementation options:**

#### Option A: Simple Trailing Stop
- After +20 pips profit, move SL to breakeven
- After +30 pips, trail SL 20 pips behind price
- After +40 pips, trail SL 15 pips behind price (tighter as profit grows)

**Today's example (GBP/USD first entry at 1.33982):**
- Price drops to 1.337 → SL moved to breakeven (1.33982)
- Price drops to 1.335 → SL moved to 1.337
- Price drops to 1.330 → SL moved to 1.3315
- Price drops to 1.327 → SL moved to 1.3285
- Price bounces back to 1.329 → **SL triggered at 1.3285** → +£300 profit (vs +£86 with fixed TP)
- **No re-entry needed** — one trade captured the whole move

#### Option B: ATR-Based Trailing Stop
- Trail SL at 2x ATR behind the best price
- More adaptive to market conditions
- On a volatile day like today, ATR would be wider, allowing more room to breathe

#### Option C: Stepped Trailing (Chandelier Exit)
- Move SL to breakeven at +25 pips
- Move SL to +20 pips at +35 pips
- Move SL to +30 pips at +45 pips
- Never worse than +30 pips profit after the trade has moved 45+

### 5.2 RE-ENTRY COOLDOWN

**The idea:** After hitting TP on a pair, don't re-enter the same pair in the same direction for at least N minutes/hours.

- The bot currently re-enters within **seconds** of TP
- A 30-60 minute cooldown would have prevented the final losing entries at 15:15-15:30
- Could also scale the cooldown based on how many consecutive TPs have been hit on the same pair

### 5.3 MOMENTUM EXHAUSTION DETECTOR

**The idea:** Track how far price has already moved in the current session. If the move is extended (e.g., >100 pips on the day), reduce confidence or skip new entries.

- At 15:15, GBP/USD had already dropped 130+ pips — the move was likely exhausted
- The bot had no concept of "this trend has been running all day, it might reverse"
- Could use: distance from session open, RSI on higher timeframe, or number of consecutive TPs as a proxy

### 5.4 PARTIAL PROFIT-TAKING

**The idea:** Close half the position at +25 pips, move SL to breakeven, let the rest run with a trailing stop.

- Locks in guaranteed profit on every trade that gets to +25
- The remaining half can capture the bigger move
- Worst case: you bank +25 on half and breakeven on the other half
- Best case: you bank +25 on half and ride a 100+ pip trend on the other half

### 5.5 CONSECUTIVE TP LIMIT PER PAIR

**The idea:** After 2-3 consecutive TPs on the same pair in the same direction, stop trading that pair for the session.

- Today: GBP/USD hit TP 3 times, then re-entered a 4th time and got stopped
- If the bot had stopped after 3 TPs, it would have banked £276 instead of net £127
- Simple to implement, immediately prevents "going to the well too many times"

---

## 6. RECOMMENDATION PRIORITY

| Priority | Solution | Impact | Complexity |
|----------|----------|--------|------------|
| **1** | **Trailing stop** (Option A or C) | Very high — captures trends, eliminates "left money on table" | Medium |
| **2** | **Re-entry cooldown** (30-60 min) | High — prevents chasing exhausted moves | Low |
| **3** | **Consecutive TP limit** (max 3 per pair) | Medium — simple guard rail | Very low |
| **4** | **Partial profit-taking** (50% at +25 pips) | Medium — locks in profit | Medium |
| **5** | **Momentum exhaustion detector** | Medium — prevents late entries | Medium-High |

### What I'd Implement First

**Trailing stop + re-entry cooldown** together would have transformed today from:
- **Actual:** +£239 net, gave back £773 from peak
- **Estimated with trailing stops:** ~+£800-1,200 net, much less drawdown
- **Estimated with trailing + cooldown:** ~+£1,000-1,500 net, minimal late-day losses

The trailing stop alone would have:
1. Captured 80-100+ pips on GBP/USD instead of 3x40 then -60
2. Captured 100+ pips on EUR/USD instead of 4x40 then -60
3. Eliminated most of the late-day stop-outs (because trades would already be closed with profit)

---

## 7. TODAY'S LESSON

The bot's strategy **works** — it correctly identified the USD strength and traded in the right direction all day. The signal generation is sound. What failed is **profit management**:

- Fixed 40-pip TP in a 200-pip trend is like catching rain in a shot glass
- Instant re-entry treats every TP as a new opportunity, but it's actually the same move
- No concept of "enough" — the bot will keep re-entering until it gets stopped

**The bot needs to learn when to let winners run and when to stop pressing.**

---

*Analysis based on OANDA transaction logs, performance_log CSVs, and bot_state files for March 3, 2026.*
