# NQ Apex Copilot

An intraday Nasdaq futures trading assistant built around one honest premise:
**the signal layer will be wrong regularly, and the risk layer must never be.**

It ships as two things that share identical logic:

- a **Python engine + CLI** that reads bars and prints one instruction, and
- a **TradingView Pine indicator** that draws the same read on your chart.

---

## Read this before you use it

You asked for something that makes no mistakes. Half of that is achievable and
half is not, and it matters enormously which half is which.

**Cannot be made mistake-free.** Anything that forecasts price. Every setup in
here is a conditional pattern with a modest historical edge. A 90/100 score is
not a 90% win rate — the score ranks candidates against each other, nothing
more. Losing trades are not malfunctions; they are the cost of doing business.
Any tool marketed to you as never wrong is lying, and the lie is expensive.

**Can be made mistake-free, and is.** The arithmetic that actually ends funded
accounts:

- trailing-threshold tracking, including the unrealised-profit ratchet
- position sizing that cannot exceed your remaining drawdown room
- daily loss limits, trade counts, loss streaks
- session cut-offs, the CME halt, the Apex flatten deadline
- tick-exact prices, and commission included in every risk number

These are deterministic and covered by 153 tests. A hard rule gate always beats
a good-looking setup, and there is no code path that lets a signal override one.

This is decision support. It does not place orders, and you remain responsible
for every trade.

---

## The mechanic that ends most Apex intraday accounts

On an intraday trailing-threshold account, the threshold follows your peak
**equity** — including profit you have not booked yet.

```
Start:              balance $50,000    threshold $47,500    room $2,500
Trade runs +$1,500: equity  $51,500    threshold $49,000    room $2,500
Gives it all back:  balance $50,000    threshold $49,000    room $1,000
```

You booked nothing. You lost 60% of your buffer anyway, permanently.

Most traders discover this the second time it happens. The copilot models it
explicitly, warns you before entry how much room a runner will consume, and
tells you the exact profit that locks the threshold for good:

```
· $2,600.00 more peak equity locks the threshold at $50,100.00 permanently.
· If this runs to target 2 (+$658.00 open) your threshold rises $658.00.
  That room is permanent — do not let a winner round-trip to breakeven.
```

---

## Install

Requires Python 3.11+. No third-party dependencies — deliberately, so there is
less to break next to a funded account.

```bash
git clone <this repo> && cd Mac
pip install -e .
```

Or run it without installing:

```bash
PYTHONPATH=src python3 -m nqcopilot.cli --demo
```

---

## Quick start

```bash
# See it work on deterministic synthetic bars
nqcopilot --demo

# Your real account, bars exported from your platform
nqcopilot --csv nq_5m.csv --symbol MNQ --balance 51200 --threshold 48700

# Live-ish delayed data, refreshing every minute
nqcopilot --live --symbol MNQ --state ~/.nqcopilot.json --watch 60
```

Output:

```
══════════════════════════════════════════════════════════════════════════════
 NQ COPILOT │ MNQ │ Thu 23 Jul 10:40 ET │ morning trend
══════════════════════════════════════════════════════════════════════════════

  GO LONG
    GO LONG 7 MNQ @ 20,418.75 — Trend Pullback (66/100)

  ORDER
    Entry         20,418.75   7 × MNQ
    Stop          20,403.00   −$220.50  (15.75 pts)
    Target 1      20,442.25   +$329.00  (1.49R)
    Target 2      20,465.75   +$658.00  (2.98R)

  MARKET
    Price 20,418.75   Regime TREND UP

  WHY
    ✓ Pullback to the 21 EMA held, momentum resuming
    ✓ 15m trend agrees
    ✓ EMAs stacked in trade direction
    ✓ ADX 35: directional
    ✓ Morning Trend: prime hours

  CAUTION
    ! On the wrong side of session VWAP

  ACCOUNT
    · Equity $50,000.00 | threshold $47,500.00 | room $2,500.00
    · Risk $229.88 all-in (stop loss plus commission) of your $2,500.00 room — 9.2%
    · Scale: take 3 off at 20,442.25 (+$141.00), trail the remaining 4 toward 20,465.75
    · Move the stop to breakeven once price reaches 20,434.50 (1.0R)
```

Every entry states its rationale *and* its counter-argument. A tool that hides
why is training you to click without thinking.

---

## Data sources

| Source | Use | Notes |
|---|---|---|
| `--csv` | **Live trading** | Export 5-minute bars from Tradovate / NinjaTrader / TradingView. The reliable path. |
| `--live` | Learning, after-hours review | Delayed, rate-limits, revises bars. Not for funded decisions. |
| `--demo` | Testing | Deterministic synthetic bars. Means nothing about real performance. |

To wire your broker feed in, produce a `list[Bar]` — that is the entire
interface, and everything downstream works unchanged.

### Economic calendar (optional)

`--news-auto` fetches the day's high-impact releases and blacks out entries
around them, replacing hand-typed `--news 08:30` flags. Backed by the RapidAPI
[Trading View API](https://rapidapi.com/apidojo/api/trading-view) (apidojo):

```bash
export RAPIDAPI_KEY=your-key
nqcopilot --csv nq_5m.csv --news-auto --news-countries US
```

**That API cannot supply price bars.** Its 14 endpoints are auto-complete,
calendars, ideas, news, movers and financials — there is no OHLC/candlestick
endpoint, so it can't replace `--csv` or `--live`. Its economic calendar is
genuinely useful though, which is the part wired up here.

If the fetch fails, the copilot warns **loudly on stderr** and still produces a
read. It never drops a safety gate silently:

```
! news calendar unavailable (...). Blackout windows are NOT active —
  check the schedule yourself before trading.
```

## Daily workflow

```bash
# 1. Start the session
nqcopilot --state ~/.apex.json --reset-day

# 2. Run through the day
nqcopilot --state ~/.apex.json --live --symbol MNQ --watch 60

# 3. Book each trade as it closes — this is what keeps the threshold honest
nqcopilot --state ~/.apex.json --record-trade -125.50
nqcopilot --state ~/.apex.json --record-trade 340.00
```

The state file tracks balance, threshold, peak equity, trade count, loss streak
and per-day P&L for the consistency rule. If you skip step 3 the risk layer is
working from stale numbers and its guarantees no longer hold.

---

## What it refuses to let you do

| Gate | Default | Why |
|---|---|---|
| Account breached | equity ≤ threshold | Nothing else matters |
| Insufficient room | < $400 | Not enough buffer to survive a normal loss |
| Daily loss limit | −$600 | Apex imposes none, which is why you must |
| Daily profit lock | +$900 | Giving back a green day is how accounts stall |
| Trade count | 4/day | Overtrading is the most common blow-up path |
| Loss streak | 2 in a row | You are misreading the session; stop |
| Stop too tight | < 8 pts | Inside NQ's noise band — a coin flip |
| Stop too wide | > 60 pts | Not an intraday setup |
| Flatten deadline | 16:59 ET | Apex requires flat |
| CME halt | 17:00–18:00 ET | Market closed |
| Opening auction | first 5 min | Spreads wide, range undefined |
| Lunch | 11:30–13:30 ET | Poor follow-through (soft warning) |
| News blackout | ±15 min | `--news 08:30`, or `--news-auto` to fetch the schedule |

Position size is the **minimum** of every applicable constraint, and the card
names which one bound. Commission is inside every risk figure — on micros with a
tight stop it is a meaningful slice of the loss.

---

## The setups

Each is gated to the regime where it has a rationale. Trading a fade in a trend,
or a breakout in chop, is how a reasonable idea becomes a losing one.

| Setup | Regime | Idea |
|---|---|---|
| **Trend Pullback** | Trend | Enter with dominant flow where the stop is close. Structurally the best intraday pattern on NQ. |
| **Opening Range Breakout** | Trend / expansion | First accepted close outside a completed 09:30–10:00 range. |
| **VWAP Reclaim** | Any but expansion | The session's balance point changes hands. |
| **Failed Breakout** | Any | A poke through the prior-day high/low that does not hold, trapping breakout traders. |
| **VWAP Fade** | Chop / quiet only | 2σ extension with visible rejection. Target 1 only — never held for a runner. |

The regime filter is the highest-value component in the system. On demo data
roughly 3% of bars produce an entry — about two per day, which suits a four-trade
daily cap.

---

## TradingView

`pine/NQApexCopilot.pine` mirrors the engine: same EMAs, same Wilder ATR/ADX,
same session VWAP with volume-weighted bands, same regime gate, same five
setups, same sizing.

1. Chart **MNQ1!** or **NQ1!** on **5-minute** candles.
2. Set the chart timezone to **New York**.
3. Paste the script into Pine Editor → *Add to chart*.
4. **Open Settings and enter your real balance and threshold.** The defaults are
   placeholders; sizing is wrong until you do this.

Filtered setups are marked with a grey ✕ rather than hidden — seeing what was
declined, and why, is how you come to trust the filter.

---

## Configuring for your account

Prop-firm rules change without notice and differ between variants (evaluation
vs PA, intraday vs end-of-day, Rithmic vs Tradovate). **Every parameter is
configurable, and the bundled presets are a starting point, not an authority.**

The `apex50k-intraday` preset assumes: $2,500 drawdown, threshold starting at
$47,500 and freezing at $50,100, 10 minis / 100 micros, 30% consistency rule.
Published third-party summaries disagree on the lock level in particular. **Open
your Apex dashboard and confirm these before trading**; the CLI prints them on
every run for exactly that reason.

To change them:

```python
from dataclasses import replace
from nqcopilot.apex import APEX_50K_INTRADAY

my_account = replace(APEX_50K_INTRADAY, drawdown_amount=2_000.0, threshold_lock=52_100.0)
```

---

## Replay / backtest

```bash
nqcopilot --csv nq_5m_2024.csv --symbol MNQ --backtest --trades
```

Replays the copilot bar by bar, evolving the account exactly as it would live —
the threshold trails, daily counters roll, and entries are gated by the state as
it stood at that moment. It tests the whole system, not just the signal layer.

**Every modelling choice is the pessimistic one**, because a harness that
flatters the strategy is worse than none:

| Choice | Why |
|---|---|
| Entry fills on the **next bar's open**, plus slippage | The signal bar's close has already happened and can't be traded. Filling at it is the most common way a backtest invents profit. |
| Ambiguous bars resolve **as losses** | When one bar's range holds both the stop and a target, there's no honest way to know which came first without tick data. |
| Gaps fill **at the open** | Not at the stop price. |
| Threshold ratchets on each bar's **favourable extreme** | The most punishing reading of the intraday rule. |
| Commission on **every** exit | Including partial scale-outs. |
| Time stop at 24 bars | A momentum thesis that hasn't resolved in two hours has expired. |

A worked example of why this matters — a run that finished **up $66 net**:

```
 Account   balance $50,066.62  threshold $47,891.64  room $2,174.98
```

Up sixty-six dollars, and $325 of drawdown room gone. That is the ratchet, and
it is why the risk layer exists.

**On `--demo` data the strategy loses slightly.** That is the correct result and
a good sign: synthetic bars are a random walk with no edge to find, so anything
showing a profit there would mean the harness is cheating. Real expectancy needs
real bars.

What it still cannot model: true slippage on a fast tape, partial fills, order
rejects, or whether your platform was connected. Treat any output as an upper
bound on quality, never as an expectation.

## Testing

```bash
python3 -m pytest -q     # 174 tests
```

The suite covers the threshold ratchet and its monotonicity under random mark
sequences, sizing under every binding constraint, all rule gates, indicator
correctness against Wilder/TradingView conventions, and two properties that are
easy to get quietly wrong:

- **No lookahead.** A pivot is published only once confirmed; truncating future
  bars never changes a past value.
- **Risk veto.** Swept across the whole series, no hard blocker ever coexists
  with an actionable entry, and a fully-stopped trade never breaches the account.

---

## Limitations

- **No performance claim.** The replay harness exists, but I have not run it on
  real market data, so there is no expectancy figure here worth trusting. Any
  equity curve from `--demo` is synthetic and means nothing.
- **Parameters are conventional, not optimised.** Tuning them to a historical
  window is the classic route to something that looks superb in backtest and is
  worthless forward.
- **`--live` is delayed data**, can rate-limit, and revises bars. Fine for
  learning the tool and reviewing after hours; for live decisions on a funded
  account, export from your broker or wire your Rithmic/Tradovate feed into the
  same `list[Bar]` interface.
- **Decisions are made on closed bars.** Acting on a forming bar means acting on
  a number that can still change.
- **No order placement.** By design.
- **Rule presets need verifying** against your dashboard, as above.

---

## Layout

```
src/nqcopilot/
├── contracts.py   Tick-exact price and dollar math
├── bars.py        Bars, sessions, the CME/Apex clock
├── indicators.py  EMA, Wilder ATR/RSI/ADX, session VWAP, pivots, resampling
├── market.py      Indicator stack + regime classification
├── signals.py     The five setup detectors
├── apex.py        Trailing threshold, sizing, rule gates, consistency rule
├── playbook.py    Fuses signal + risk into one Directive (risk has veto)
├── backtest.py    Pessimistic bar-by-bar replay and metrics
├── calendar.py    Economic-calendar fetch for automatic news blackouts
├── data.py        CSV / live / demo loaders
└── cli.py         The decision card
pine/NQApexCopilot.pine
tests/
```

Start at `playbook.evaluate` — it returns a single `Directive`.

---

## Licence

MIT. Trading futures involves substantial risk of loss and is not suitable for
every investor. This software is provided as-is, with no warranty and no
guarantee of profitability.
