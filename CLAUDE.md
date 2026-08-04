# CLAUDE.md

Guidance for working in this repo. Read the invariants before changing anything
in `apex.py`, `playbook.py`, or `indicators.py`.

## What this is

An intraday Nasdaq futures copilot for an Apex $50K Performance Account. Two
layers with deliberately different epistemic status:

- **Signal layer** (`market.py`, `signals.py`) — pattern recognition with modest
  edge. Wrong regularly. Never describe a score as a win probability.
- **Risk layer** (`apex.py`) — arithmetic. Exact, and holds veto power over the
  signal layer.

Entry point: `playbook.evaluate` → one `Directive`.

## Invariants — do not break these

Each is enforced by tests. If a change makes one fail, the change is wrong.

1. **Risk vetoes signal.** A hard `Blocker` always produces `STAND_DOWN` (or
   `FLATTEN` when in a position), no matter the setup score. There must be no
   code path where a high score overrides a rule gate.
2. **No lookahead.** Value at index `i` must be computable from bars `0..i`
   only. Swing pivots publish at `i + right`, not at the pivot bar. Truncating
   future bars must never change a past value.
3. **Series alignment.** Indicators return input-length lists with `None`
   during warmup — never zero, never forward-filled.
4. **The threshold never decreases.** Monotonic under any sequence of marks,
   and capped at `threshold_lock`.
5. **Sizing cannot breach.** A fully-stopped trade must leave equity above the
   threshold, always.
6. **Prices land on legal ticks.** Stops round wider, targets round nearer.
   Never the reverse.

## Apex rules as verified

These were corrected mid-project against the user's official rulebook. Two were
wrong in the dangerous direction. **Do not "restore" the old values.**

| | Value | Note |
|---|---|---|
| Drawdown | **$2,000** | Was wrongly $2,500. Overstating it reports room the account lacks. |
| Threshold start | $48,000 | balance − drawdown |
| Threshold lock | $50,100 | start + $100; also the payout safety net |
| Profit target | $3,000 | evaluation |
| Consistency | **50%** | Was wrongly 30%. Measured **since last payout**, strict inequality ("50% or more" fails). |
| Qualifying days | 5 | each clearing `payout_min_daily_profit` (default $50, unconfirmed) |
| Min payout | $500 | against profit above the safety net |
| Lifetime payouts | 6 | per account |
| Daily loss limit | none firm | Evaluations enforce none. PA is tier-based — never guessed, supplied via `--tiers`. |

## Three traps this codebase exists to prevent

1. **The intraday ratchet.** The threshold follows peak *equity*, including
   unrealised profit. A winner that round-trips to breakeven costs drawdown
   room permanently without booking a cent. Marking must use the bar's
   favourable extreme, not its close.
2. **Tier timing.** Contract caps come from the **end-of-day** balance and apply
   to the *next* session. Being up intraday does not raise today's allowance;
   treating it as though it does is a rule breach. `tier_reference_balance`
   only advances in `end_session`.
3. **Payout windows.** Consistency and qualifying-day counts reset at each
   payout. `record_payout` handles this; a mid-day `end_session` must not erase
   the day from `daily_pnl` (regression already fixed once).

## Risk limits are derived, not hardcoded

Dollar limits are fractions of the **drawdown allowance**, chosen by
`RiskStyle`. A fixed $600 daily cap is prudent on a $10,000 buffer and reckless
on a $2,000 one; only the ratio makes that visible. `days_to_breach` is the
sanity check — conservative gives ~6.7 losing days, aggressive ~2.9.

Default is `CONSERVATIVE`, including on `RiskLimits` itself. The out-of-the-box
setting must be the safe one.

## Commands

```bash
python3 -m pytest -q                                  # 416 tests
nqcopilot --demo                                      # decision card
nqcopilot --csv bars.csv --backtest --trades          # replay
nqcopilot --scid ~/SierraChart/Data/NQZ26.scid --scid-info    # inspect a file
nqcopilot --scid NQZ26.scid --backtest                # replay real history
nqcopilot --serve --csv bars.csv --secret "..."       # TradingView webhook feed
nqcopilot --poll URL --poll-price-path data.last      # quote-to-bar feed
nqcopilot --state ~/.apex.json --record-trade -125.50 # book a fill
```

## Conventions

- **Zero runtime dependencies.** Stdlib only — fewer moving parts next to a
  funded account. Do not add a dependency without a strong reason.
- Everything is timezone-aware, anchored to `America/New_York`.
- Decisions are made on **closed bars only**.
- The Pine indicator in `pine/` mirrors the engine. Change both together, or
  the chart and the CLI will disagree.
- Commission is included in every risk figure. Gross R:R flatters tight stops.
- **A Sierra `.scid` record is usually a trade, not a bar.** Sierra overloads the
  OHLC fields for tick data: `Open` is a sentinel, `High` is the ask, `Low` the
  bid, `Close` the trade price. Reading those four floats as a bar invents a
  price series that still looks plottable. `sierra.py` detects and aggregates
  ticks; never bypass it. The sentinels are float32 and must be matched by
  magnitude — `==` against the documented literals silently fails.

## What is NOT verified

- **No real-data backtest has ever run.** Still true, but the reason has
  changed. Free intraday futures history was not reachable during development
  (Yahoo rate-limits, Stooq has no intraday); `sierra.py` now reads Sierra
  Chart's `.scid` files, so anyone with Sierra Chart installed has real
  tick-resolution history on disk to replay against. The path is *unblocked, not
  walked*: the reader has only ever been exercised on synthetic ticks, so every
  expectancy figure in this repo still says nothing about real markets. On
  synthetic data the strategy posts a small loss, which is the *correct* result
  — a random walk has no edge, so a profit there would mean a modelling error.
- **Indicator parameters are conventional, not optimised.** Tuning them to a
  historical window is the classic route to something that backtests superbly
  and is worthless forward.
- **The evaluation threshold lock** is deliberately unset — the offset varies by
  platform. Unset means "never stops trailing", which understates room rather
  than overstating it.
