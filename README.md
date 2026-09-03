# ATRx Screener — Support-Level Bounce Strategy

[![GitHub repo](https://img.shields.io/badge/GitHub-mjmasalawala%2FATRx-blue?logo=github)](https://github.com/mjmasalawala/ATRx)

This documents the screener in `screener.py` and the strategy it implements:
finding volatile stocks trading near a price level they've historically
respected, on the premise that they tend to bounce from there within the
next 3–5 trading sessions.

**This tool only screens and ranks. It never places an order.** The output
is a CSV + console table meant for you to manually review before trading.

---

## 1. The idea in one sentence

Volatile stocks develop price levels that repeatedly act as a floor —
because stop-losses, algo orders, and buyers cluster there — and when price
returns to a level that has held before without breaking it, that's a
higher-probability spot for a bounce than a random point on the chart.

## 2. Pipeline overview

```
universe.json (your tickers)
   -> fetch daily history (Kite historical API)
   -> compute ATR7 (volatility)
   -> keep only the more volatile names in your universe
   -> find pivot lows -> cluster into support levels -> score by touches/breaches
   -> keep levels price is currently near (ATRx)
   -> backtest: how has THIS stock behaved every other time it touched THIS level?
   -> composite score -> ranked CSV
```

The rest of this document walks through every one of those steps in the
order they run, using one running worked example throughout, so you can
see exactly what number produces what decision at each stage.

---

## 3. Worked example, start to finish

### 3.1 Fetch daily history

For every symbol in `universe.json`, the screener asks Kite's historical
API for daily OHLCV candles:

```
timestamp    open    high    low    close   volume
2026-06-01   99.50   100.80  99.10  100.40  42,000
```

It fetches more than just `lookback_days` (60) worth. Confirming a pivot
low on, say, day 55 of a 60-day window needs 5 more days of data *after*
it (section 3.3 explains why) — data that falls outside a bare 60-day
fetch. The historical backtest in section 3.6 has the same problem in
reverse: measuring "what happened 5 days after a touch" requires those 5
days to exist in the data. So the script actually pulls
`lookback_days + pivot_window + atr_period + forward_days_long + 15`
calendar days (scaled up further to cover weekends/holidays) — that extra
range is pure scaffolding for the edges, not part of the "60 days" you
should think of as the analysis window itself.

### 3.2 Compute ATR7, and keep only the volatile half of your universe

**True Range** on any single day is the largest of three gaps:
```
TR = max(high − low, |high − prev_close|, |low − prev_close|)
```
The last two terms catch overnight gaps — a stock that gaps up 3% and
then trades in a tight range that day still "moved" 3%, and `high − low`
alone would miss that.

**ATR7** is a 7-day rolling average of True Range. Say the last 7 True
Range values for our example stock, STOCK_A, were:
`1.2, 1.8, 1.1, 2.0, 1.4, 1.6, 1.4`
```
ATR7 = (1.2+1.8+1.1+2.0+1.4+1.6+1.4) / 7 = 1.50
```
We use 7 days (not the more common 14) because it's more reactive to the
stock's *current* volatility regime — the tradeoff is more day-to-day
jumpiness, since one unusual range day swings a 7-day average more than a
14-day one. This is configurable via `atr_period` in `config.py`.

Divide by price to get a volatility measure that's comparable across
stocks of different price levels: `atr_pct = ATR7 / current_price`.
With STOCK_A's current price at 101.50:
```
atr_pct = 1.50 / 101.50 = 1.48%
```
Do this for every stock in your universe. Say your list has five tickers:
```
Symbol      ATR%
STOCK_A     1.48%
STOCK_B     0.60%
STOCK_C     2.90%
STOCK_D     1.10%
STOCK_E     3.50%
```
`min_atr_percentile = 50` sets the cutoff at the median of these five
values. Sorted: `0.60, 1.10, 1.48, 2.90, 3.50` → the median is **1.48%**.
The rule keeps anything **≥** the threshold:
```
STOCK_B  0.60%  → below 1.48%  → DROPPED
STOCK_D  1.10%  → below 1.48%  → DROPPED
STOCK_A  1.48%  → equals threshold → PASSES
STOCK_C  2.90%  → above threshold → PASSES
STOCK_E  3.50%  → above threshold → PASSES
```
STOCK_B and STOCK_D aren't bad stocks — they're just the calmer half of
*this specific list, today*. The threshold is relative to your own
universe rather than a fixed number like "ATR% > 2%" precisely so it
keeps working whether the overall market is quiet or choppy that month:
you always keep the more-volatile half of whatever you're watching. Only
STOCK_A, STOCK_C, and STOCK_E move on to level detection.

### 3.3 Find pivot lows (the `pivot_window` rule)

A day counts as a **pivot low** only if its `low` is the smallest low
among the 5 trading days *before* it and the 5 trading days *after* it —
an 11-day window centered on that day. `pivot_window = 5` sets that 5.

Take a small illustrative stretch of daily lows:
```
Day:   1    2    3    4    5    6    7    8    9   10   11   12   13   14   15
Low: 102  100   98   96   94   95   97   99  101   93   95   97  100   99  101
```
Check **Day 10** (low = 93), which has a full 5 days on both sides:
- 5 days before (days 5–9): lows 94, 95, 97, 99, 101
- 5 days after (days 11–15): lows 95, 97, 100, 99, 101
- Day 10's own low, 93, is smaller than all ten neighbors → **confirmed pivot low**.

Now check **Day 5** (low = 94) the same way, using days 0–4 before and
6–10 after: its neighbors include Day 10's low of 93, which is *smaller*
than 94. Day 5 fails the test — it looked like a dip at the time, but the
market later made a lower low nearby, so Day 5 wasn't really the bottom of
that stretch. It's discarded, which is exactly the filter's job: only
keep days that were the genuine low point of a broader window, not every
minor wiggle.

**The unavoidable catch**: confirming Day 10 needed days 11–15 to already
exist. That means the most recent 5 trading days in any dataset can never
register as a pivot yet — you can't know a low "held for 5 days after" until
5 days have actually passed. A sharp bounce from yesterday won't show up
as a validated pivot for a few more sessions, no matter what.

### 3.4 Cluster pivots into a level, and apply the two validation gates

Running this test across the full ~60-day lookback for STOCK_A might turn
up these confirmed pivot lows on different dates:
```
Date        Pivot low
Jan 5       100.20
Jan 18       99.80
Feb 2       100.50
Feb 20       94.00
```
The first three are clearly "the same" level to the eye; 94.00 is a
separate, much lower one. The screener automates that judgment using
ATR instead of eyeballing it: two pivots merge into one level if they're
within `cluster_atr_multiple × current ATR7` of each other. With
`cluster_atr_multiple = 1.0` and ATR7 = 1.50, the merge threshold is 1.50.

Sorted by price: `99.80, 100.20, 100.50, 94.00`
```
100.20 − 99.80  = 0.40  → within 1.50 → same group
100.50 − 100.20 = 0.30  → within 1.50 → same group
 94.00 − 100.50 = 6.50  → outside 1.50 → new group
```
Result: two candidate levels.
- **Level A**: `{99.80, 100.20, 100.50}` → level price = median = **100.20**, touches = 3
- **Level B**: `{94.00}` → touches = 1

Scaling the merge distance by ATR (instead of a fixed rupee band) matters
because a ₹1.50 spread means something different for a calm stock than a
volatile one — ATR answers "are these close *relative to how much this
stock normally moves in a day*," which is the real question being asked.

**Gate 1 — `min_touches = 3`**: Level B has only 1 touch and is discarded
immediately — a single dip proves nothing about a level. Level A has 3
touches and survives.

**Gate 2 — `max_breaches = 1`**: now scan every close (not just the pivot
days) in the lookback window for any close that dropped meaningfully
below Level A's price. A breach only counts if the close is at least
`breach_buffer_atr × ATR7` below the level — `0.25 × 1.50 = 0.375` — so a
close needs to fall below `100.20 − 0.375 = 99.825` to count. Say on Jan
25 the stock closed at 98.90: that's below 99.825, so it's **one real
breach**. If that's the only such close in the window, Level A ends up
with `breaches = 1` — right at the limit, so it survives, but only just.
A second breach anywhere in the window would have disqualified it
entirely: a level that's already failed twice isn't one worth leaning on
for a bounce trade a third time.

Level A passes both gates and moves forward as: **level = 100.20,
touches = 3, breaches = 1**.

### 3.5 Keep levels price is currently near (the ATRx filter)

With current price 101.50 and ATR7 1.50:
```
ATRx = (current_price − level) / ATR7 = (101.50 − 100.20) / 1.50 = 0.867
```
`atrx_lower = -0.3` and `atrx_upper = 1.0` define the allowed range.
0.867 falls inside it → Level A **passes** and moves to the backtest.

To see why the bounds matter, run three alternative scenarios with the
same level and ATR:

**Price runs up to 103.50** (already well past the level):
```
ATRx = (103.50 − 100.20) / 1.50 = 2.20   → outside [-0.3, 1.0] → DROPPED
```
Price is already more than two full average-days'-move above the level —
this would mean chasing a move that's already happened, not catching a
fresh return to the floor.

**Price dips slightly to 99.90** (a small poke below, not yet a breach):
```
ATRx = (99.90 − 100.20) / 1.50 = −0.20   → inside [-0.3, 1.0] → STILL PASSES
```
This is the deliberate small negative allowance. A shallow dip below the
level that hasn't closed there is often a *stronger* setup, not a weaker
one — a failed breakdown that reclaims the level can trap short sellers
and produce a sharper bounce.

**Price drops hard to 97.00**:
```
ATRx = (97.00 − 100.20) / 1.50 = −2.13   → outside [-0.3, 1.0] → DROPPED
```
This is more than two full daily ranges below the floor — no longer a
shakeout, but a level that has clearly been broken through. The filter
can't tell from price alone whether this is a one-day panic that mean-
reverts anyway or a real breakdown into a new downtrend, so rather than
guess, it simply declines to call this "near the level" and leaves the
judgment to you.

There's a second consequence here worth noting: a close at 97.00 is also
well below the breach threshold (99.825 from section 3.4), so it would
itself register as a **second breach** the next time the screener scans
history. Level A already had `breaches = 1`; a second breach pushes it to
2, which exceeds `max_breaches = 1`. On the *next* run, Level A wouldn't
just fail the proximity filter — it would fail Gate 2 in section 3.4 and
stop qualifying as a valid level entirely, until enough time passes for
those older pivots to age out of the 60-day lookback. The two gates work
together this way: ATRx keeps you from acting on a level that's currently
too far away, while the breach count is what permanently retires a level
once the market has shown twice that the floor doesn't hold.

### 3.6 Backtest every historical touch of the level

Separately from the 3 pivots that *defined* Level A, the screener scans
every close in the lookback window for days price came near 100.20 —
using a wider band than the strict pivot definition, since this should
also catch tests of the level that didn't form a textbook swing low.
`touch_band_atr = 0.5` sets that band:
```
band = 0.5 × 1.50 = 0.75
touch zone = 100.20 ± 0.75  →  99.45 to 100.95
```
Say four historical closes fall inside that zone:
```
Date        Close    Close 3 days later   Close 5 days later
Jan 5       100.20   101.80               102.40
Jan 18       99.90   100.10               100.50
Feb 2       100.50    99.80               101.20
Mar 3       100.00   102.10               103.00
```
For each, compute the forward return: `fwd_ret = (future_close −
touch_close) / touch_close`. For the 5-day column:
```
Jan 5:  (102.40 − 100.20) / 100.20 = +2.20%
Jan 18: (100.50 −  99.90) /  99.90 = +0.60%
Feb 2:  (101.20 − 100.50) / 100.50 = +0.70%
Mar 3:  (103.00 − 100.00) / 100.00 = +3.00%
```
(The 3-day column is computed the same way, independently.) All four are
positive:
```
hit_rate_5d    = 4 positive / 4 total × 100 = 100%
avg_fwd_ret_5d = (2.20+0.60+0.70+3.00) / 4  = 1.625%
backtest_touches = 4
```
`backtest_touches` matters as much as the hit rate itself — 4 historical
touches is a real signal, but still a small sample. The same 100% hit
rate on 10+ touches would be a much stronger claim.

### 3.7 Score and rank against the rest of the day's candidates

Say after every symbol in your universe has been through sections
3.2–3.6, three levels survived every filter:
```
Symbol    ATRx    Breaches   Recency_wt   Hit_5d%   AvgRet_5d%
STOCK_A   0.87    1          1.4          100       1.625
STOCK_C   0.10    0          2.1           80       0.900
STOCK_E   0.95    0          0.5           60       0.400
```
(`Recency_wt` — used but not yet defined above — is
`sum(exp(-age_in_days / recency_decay_days))` across a level's touches;
a touch from today contributes ~1.0, one from 60 days ago contributes
~0.37. It rewards levels validated more recently over otherwise-identical
ones validated long ago. `recency_decay_days = 60` by default.)

The score combines three normalized components plus a breach penalty,
weighted by `score_w_return=0.5, score_w_proximity=0.3,
score_w_recency=0.2, score_breach_penalty=0.15`:

**Return component** — z-score `avg_fwd_ret_5d` across the three
candidates (mean ≈ 0.975, std ≈ 0.51), then scale by hit rate so a single
lucky outlier touch can't dominate:
```
STOCK_A: z=(1.625−0.975)/0.51= 1.27 → ×1.00 =  1.27
STOCK_C: z=(0.900−0.975)/0.51=−0.15 → ×0.80 = −0.12
STOCK_E: z=(0.400−0.975)/0.51=−1.13 → ×0.60 = −0.68
```
**Proximity component** — `1 − |ATRx| / atrx_upper`:
```
STOCK_A: 1 − 0.87 = 0.13
STOCK_C: 1 − 0.10 = 0.90
STOCK_E: 1 − 0.95 = 0.05
```
**Recency component** — normalized against the max in this batch (2.1):
```
STOCK_A: 1.4 / 2.1 = 0.67
STOCK_C: 2.1 / 2.1 = 1.00
STOCK_E: 0.5 / 2.1 = 0.24
```
**Final score** = `0.5×return + 0.3×proximity + 0.2×recency − 0.15×breaches`:
```
STOCK_A: 0.5(1.27)+0.3(0.13)+0.2(0.67)−0.15(1) = 0.635+0.039+0.134−0.150 =  0.658
STOCK_C: 0.5(−0.12)+0.3(0.90)+0.2(1.00)−0.15(0)= −0.060+0.270+0.200−0     =  0.410
STOCK_E: 0.5(−0.68)+0.3(0.05)+0.2(0.24)−0.15(0)= −0.340+0.015+0.048−0     = −0.277
```
**Ranked output: STOCK_A (0.658) → STOCK_C (0.410) → STOCK_E (−0.277).**

Notice STOCK_A wins despite a breach and worse proximity than STOCK_C —
its historical edge (100% hit rate, +1.6% average return) was strong
enough to outweigh those penalties, because the return term carries the
heaviest weight. That's intentional: a real, well-sampled track record
should be able to override a slightly imperfect setup, while proximity
and recency still act as meaningful tie-breakers between otherwise
similar candidates.

`score` is a relative ranking number, not a return estimate or a
percentage — it's only meaningful compared against other candidates from
the same day's run, not across different days.

---

## 4. Parameter quick reference

| Parameter | Default | What it controls |
|---|---|---|
| `atr_period` | 7 | days averaged for ATR (volatility) |
| `pivot_window` | 5 | days on each side required to confirm a swing low |
| `lookback_days` | 60 | how far back pivots/breaches/touches are drawn from |
| `cluster_atr_multiple` | 1.0 | max ATR-distance between pivots to merge into one level |
| `breach_buffer_atr` | 0.25 | how far below a level a close must be to count as a breach |
| `min_touches` | 3 | minimum pivots required to validate a level |
| `max_breaches` | 1 | maximum tolerated real closing breaches |
| `recency_decay_days` | 60.0 | decay constant weighting recent touches more heavily |
| `atrx_lower` / `atrx_upper` | -0.3 / 1.0 | allowed "distance from level" range, in ATRs |
| `touch_band_atr` | 0.5 | band (in ATRs) used to find historical touches for the backtest |
| `forward_days_short` / `forward_days_long` | 3 / 5 | holding periods measured in the backtest |
| `min_atr_percentile` | 50.0 | volatility percentile cutoff, relative to your own universe |
| `score_w_return / proximity / recency` | 0.5 / 0.3 / 0.2 | ranking score weights |
| `score_breach_penalty` | 0.15 | score subtracted per tolerated breach |

There's no mathematically "correct" setting for any of these — treat them
as dials. If results feel too jumpy, lengthen `atr_period`. If levels look
too coarse or too fragmented on the chart, adjust `cluster_atr_multiple`.
If the top of the list keeps favoring stocks far from the level today,
raise `score_w_proximity`.

## 5. Output columns reference

| Column | Formula | What it tells you |
|---|---|---|
| `current_price` | latest close | — |
| `level` | median of the clustered pivot lows | the support price itself |
| `atrx` | `(current_price − level) / ATR7` | distance from the level, in typical daily moves |
| `atr_pct` | `ATR7 / current_price × 100` | how volatile this stock is, as a % of price |
| `touches` | confirmed pivot lows in the cluster | how many times this was a swing low |
| `breaches` | closes beyond the breach buffer below the level | how many times the level has meaningfully failed |
| `recency_weight` | sum of `exp(-age/decay)` across touches | how fresh the evidence for this level is |
| `backtest_touches` | historical closes within the touch band | sample size behind the two columns below |
| `hit_rate_3d_pct` / `hit_rate_5d_pct` | % of touches with a positive forward return | how often a bounce actually followed a touch |
| `avg_fwd_ret_3d_pct` / `avg_fwd_ret_5d_pct` | mean forward return after a touch | average size of the move, when it happened |
| `score` | composite ranking (section 3.7) | overall rank for the day — higher is stronger by this model's logic |

## 6. When and how often to run it

Every number this screener produces — ATR7, the levels themselves,
touches, breaches, the backtest — is computed from **daily candles**. A
level built from yesterday's data doesn't change again until tomorrow's
daily candle closes and either confirms a new pivot or doesn't. That makes
this fundamentally a **once-a-day batch job**, not something to run
continuously:

- **Run it once, after market close** (any time after ~3:30 PM IST). At
  that point the day's candle is final, so `touches`, `breaches`, and the
  backtest are computed on complete data that won't revise itself later
  that evening.
- Running it mid-session would score against an incomplete, still-moving
  "today" candle — a level could look touched or breached based on a
  candle that finishes differently by the close.
- **Nothing here ticks intraday**, and deliberately so: the level and
  ATR7 are fixed for the day regardless of what price does during market
  hours. The only thing that's genuinely live intraday is where the
  *current* price sits relative to yesterday's frozen level — this
  version doesn't track that live; a future version could, by polling
  quotes separately from the daily batch.

## 7. The HTML report

Every run writes two files into `output/`:

- `atrx_<timestamp>.csv` — the top `top_n` (default 25) candidates, for a
  quick shortlist.
- `atrx_report_<timestamp>.html` **and** `atrx_report_latest.html` — every
  candidate that passed all filters, in a single self-contained page you
  open in a browser. No server, no network dependency beyond the two
  Google Fonts links (which degrade to system fonts offline).

The report gives you:
- A **filter bar**: symbol search, min score, ATRx range, min touches, max
  breaches, min backtest sample size — all applied live as you type.
- A **sortable table**: click any column header to sort by it; click again
  to reverse.
- A small **ATRx gauge** per row — a visual bar showing where price
  currently sits within the allowed `[atrx_lower, atrx_upper]` range, so
  you can see proximity to the level at a glance instead of just reading
  the number.
- Color coding: positive score/returns in green, negative in coral (muted
  colors, not attention-grabbing, since this is meant for extended
  reading) and any row with a breach flagged.

It always shows **every** candidate, not just `top_n` — the shortlist
trim only applies to the CSV — since the whole point of a filterable page
is that you do your own narrowing rather than trusting a fixed cutoff.

## 8. How to actually use the output

1. Run `python screener.py` after market close.
2. Open `output/atrx_report_latest.html` in a browser.
3. Check `touches`/`breaches` first — the level's raw track record. Then
   `hit_rate_5d_pct` and `avg_fwd_ret_5d_pct` — whether that track record
   has actually translated into forward edge, not just "the line held."
4. Weigh those last two numbers against `backtest_touches`: 3 historical
   touches is a much weaker sample than 8. Use the "Min backtest n" filter
   to hide thin-sample rows if you'd rather not see them at all.
5. Pull up the actual chart for anything near the top before acting — the
   screener understands price and volume geometry only, not news, sector
   context, or earnings dates. A validated level a company reports
   results in front of tomorrow is a different risk than the same level
   with no catalyst nearby.
6. Since this is a 3–5 session hold, it needs a CNC (delivery) or NRML
   position, not MIS — different capital lock-up than the intraday
   scalping strategy in the other project.

## 9. Known limitations, honestly

- **In-sample backtest.** The touch backtest in section 3.6 runs on the
  same data used to define the level. A level that "worked" 4/4 times
  historically could still fail the 5th — small sample sizes are common
  here, which is exactly why `backtest_touches` is surfaced instead of
  hidden.
- **Well-known levels are double-edged.** A level with many touches is,
  by construction, one the broader market has also noticed — partly *why*
  it tends to hold (clustered stop orders defend it), but also why it can
  fail fast and hard the time it doesn't, as those same stops cascade.
  Always trade this with a real stop below the level, not a mental one.
- **No fundamental or news awareness.** Pure price/volume geometry.
- **Bounded by your universe.** The screener only ever looks at what's in
  `universe.json` — it can't surface a great setup on a stock that isn't
  on your list.

I'm not a financial advisor and this isn't investment advice — this tool
surfaces candidates for your own judgment, and whether the historical edge
shown here holds up going forward is something only continued, honest
tracking of your own results can tell you.
