# grok-trader

A beginner-friendly Solana new-token scanner. It watches newly listed tokens,
drops anything that fails hard safety rules, asks Grok (xAI) for a risk read on
the survivors, and **paper trades** with fake money while logging every single
decision and the reason behind it.

Built from the "How to Build a Grok AI Trading Bot" guide by @seb.ai.

> **Educational only. Not financial advice.** New Solana tokens are extremely
> risky and most go to zero. This program cannot trade real money: there is no
> wallet, no private-key handling and no transaction-signing code anywhere in
> it. `MODE` must be `paper`; anything else makes it exit.

---

## 1. Install

You need Python 3.11 or newer (`python3 --version`).

```bash
cd grok-trader
pip install -r requirements.txt
```

That installs three small libraries: `requests` (HTTP), `python-dotenv` (reads
`.env`) and `flask` (the local dashboard).

## 2. Configure `.env`

```bash
cp .env.example .env
```

Open `.env` in a text editor and fill it in:

| Variable | What it is | Where to get it |
|---|---|---|
| `XAI_API_KEY` | Your Grok API key. Without it the bot still runs, but every token stops at the Grok step and nothing is paper-traded. | console.x.ai (pay-as-you-go credits) |
| `SOLANA_RPC_URL` | The Solana node the bot reads holders and mint data from. The public default works but is shared and limited (see section 8). | Free public: `https://api.mainnet.solana.com` (the older `api.mainnet-beta.solana.com` name serves the same node). Dedicated providers (Helius, QuickNode, Triton, etc.) give a URL with a key in it. |
| `MODE` | Must be `paper`. | - |

`.env` and every `.env.*` variant (`.env.local`, `.env.bak`, ...) are listed in
`.gitignore`, so git will never pick them up. Never paste a key into a prompt,
screenshot or chat. If your RPC URL carries a provider key, note that the bot
logs only the host name on errors, never the full URL.

## 3. Run it

**Offline first.** This runs the whole pipeline on three fictional tokens with
no network and no keys, so you can see what a buy, a skip and a stop-loss look
like:

```bash
python src/main.py --demo
```

Expected: `BUY SAFEPUP`, `SKIP RUGLORD`, `SKIP THINPOOL`, then
`SELL SAFEPUP stop_loss`, and `DEMO PASSED`. The demo writes to separate files
(`logs/decisions.demo.jsonl`, `data/paper_state.demo.json`,
`data/seen_tokens.demo.json`) so it never mixes with your real paper record.

**Paper mode** (live market data, fake money):

```bash
python src/main.py --mode paper
```

Leave it running. At least every 60 seconds (a cycle can take longer while it
waits politely on the data providers) it pulls the newest Solana tokens, scores
up to 10 of them, checks the stop-loss and take-profit of anything it holds,
and prints one line per decision. Stop it with `Ctrl+C`; the paper account is
saved after every trade, so you can restart any time.

Useful flags:

| Flag | Effect |
|---|---|
| `--once` | One scan cycle, then exit (good for a quick check) |
| `--no-dashboard` | Do not start the web page |
| `--config path` | Use a different `config.json` |

## 4. The dashboard

While the bot runs, open **http://localhost:8000**. It shows the paper balance,
equity, realised P&L, today's P&L (and whether the daily loss limit tripped),
open positions with their stop and target, average win and loss, PASS/FAIL/UNKNOWN
counts per rule (all time), and the last 50 decisions. It refreshes itself every 30 seconds and only
listens on your own machine (127.0.0.1). `/api/state` gives the same data as
JSON.

## 5. Read the logs

`logs/decisions.jsonl` has one JSON object per line, one line per event:

| Field | Meaning |
|---|---|
| `ts` | Time in UTC |
| `action` | `buy`, `skip`, `sell` or `error` |
| `token`, `symbol`, `pair`, `price_usd` | Which token, which pool, at what price |
| `checks` | Every hard rule with `status` (PASS / FAIL / UNKNOWN), the value seen and the rule |
| `grok` | Grok's reply (`risk`, `reasons`, `missing_data`) or `null` if the token never reached Grok |
| `score`, `reason` | The 0-100 convenience score and the one-line reason for the action |
| `market`, `holders`, `authorities`, `lp`, `rugcheck` | The raw numbers the decision used. `holders.holder_source` and `authorities.authority_source` say where each came from: `rpc`, `rugcheck`, or `null` when nothing could read it |
| `data_errors` | Anything a data source refused to tell us |
| `trade` (sells only) | Entry, exit, P&L, and whether it was `stop_loss` or `take_profit` |

Quick look at the last decisions:

```bash
python -c "import json;[print(r['ts'],r['action'],r.get('symbol'),r['reason']) for r in map(json.loads,open('logs/decisions.jsonl'))]" | tail -n 20
```

What to review after a few days (guide section 8): win rate, average win vs
average loss, biggest loss and how often the stop fired (all on the dashboard,
also in `/api/state` under `paper`), and the tokens that passed every rule and
then collapsed. Change one rule at a time.

## 6. Change the rules: `config.json`

No code changes needed. Tighten freely; loosen only after the logs say so.

| Key | Default | Meaning |
|---|---|---|
| `poll_interval_seconds` | 60 | How often to pull the new-token feed |
| `max_new_tokens_per_cycle` | 10 | Cap on tokens scored per cycle (keeps API usage polite) |
| `min_liquidity_usd` | 25000 | Main pool must hold at least this much |
| `min_pool_age_minutes` | 10 | Pool must be at least this old |
| `max_single_holder` | 0.10 | Largest wallet (pools excluded) may hold at most 10%; token accounts are summed per owner wallet |
| `max_top10_share` | 0.40 | Top-10 wallets (pools excluded) at most 40% |
| `require_mint_disabled` | true | Mint authority must be null (supply cannot be inflated) |
| `require_freeze_disabled` | true | Freeze authority must be null (your tokens cannot be frozen) |
| `require_lp_locked` | true | LP must be locked or burned |
| `min_lp_locked_pct` | 90 | At least this much of the LP locked/burned |
| `lp_provider` | "rugcheck" | Where LP and pool data come from (`"none"` disables it: LP becomes UNKNOWN and every token is skipped) |
| `grok_model` | see section 9 | xAI model name sent to the API |
| `grok_max_risk` | 4 | Grok risk must be at or below this (0-10) |
| `grok_reasoning_effort` | "low" | grok-4.x models think before answering and bill those tokens as output; low keeps a short classification cheap and fast. Set to `null` to omit the parameter |
| `grok_timeout_seconds` | 120 | HTTP timeout for the Grok call (reasoning models can take a while) |
| `paper_starting_balance_usd` | 1000 | Fake starting balance |
| `max_position_pct` | 0.02 | Each paper buy is 2% of the current balance |
| `stop_loss_pct` | 0.25 | Sell at -25% |
| `take_profit_pct` | 0.50 | Sell at +50% |
| `max_daily_loss_pct` | 0.05 | Realised loss of 5% of the day's starting equity stops new buys until the next UTC day |
| `max_open_positions` | 5 | Never hold more than this many tokens |
| `paper_fee_pct` | 0.01 | Haircut on every paper buy and sell, standing in for fees and slippage |
| `dashboard_host`, `dashboard_port` | 127.0.0.1, 8000 | Where the dashboard listens |

## 7. How a decision is made

1. **New token appears**: DEX Screener's latest token profiles, Solana only, each token scored once.
2. **Pool data**: DEX Screener token pairs; the pool with the most USD liquidity is "the" pool.
3. **On-chain reads** (Solana RPC): mint authority, freeze authority and supply from the parsed mint account (`getTokenSupply` as the fallback when the mint cannot be parsed), the 20 largest token accounts and who owns them, summed per owner wallet.
4. **LP / rug signals** (RugCheck): LP locked %, which large accounts are pools, RugCheck's own risk list.
5. **Hard rules**: the table below. **Any FAIL = skip. UNKNOWN never counts as PASS.**
6. **Grok**: only an all-PASS token is sent, with the raw numbers, and asked for `{"risk": 0-10, "reasons": [...], "missing_data": [...]}`. The reply is treated as data; nothing in it is ever executed.
7. **Decision**: buy only if every rule PASSED and `risk <= grok_max_risk`. The 0-100 score is for the log and dashboard; it can never override a failed rule.
8. **Paper trade**: 2% of balance, stop and target set at entry, checked every cycle.
9. **Log**: every token gets a line, including skips, errors, and feed entries that are not even a valid Solana address.

| Rule | PASS | FAIL |
|---|---|---|
| Liquidity | >= $25,000 in the main pool | Below. No figure at all (a bonding curve, or a bad number) is UNKNOWN, which is also a skip |
| Pool age | >= 10 minutes | Younger |
| Largest holder | <= 10% of supply (pools and burn address excluded) | Any single wallet above |
| Concentration | Top-10 <= 40% | Above |
| Mint authority | Disabled (`null`) | Still set |
| Freeze authority | Disabled (`null`) | Still set |
| LP status | Locked or burned, >= 90% | Unlocked, below 90%, or UNKNOWN |
| Grok | risk <= 4 with reasons | risk >= 5, no reasons, or no usable reply |

## 8. Honest limits you should know about

- **The free public Solana RPC does not serve `getTokenLargestAccounts`.** It answers a JSON-RPC 429 with the header `x-ratelimit-method-limit: 0` on the free tier (checked live, repeatedly, on both hostnames). The bot detects this, logs it in `data_errors`, and uses RugCheck's top-holder list instead, marked `holder_source: "rugcheck"` in the log. A dedicated RPC URL in `.env` gives you RPC-sourced holder data.
- **RugCheck does a lot of work here**: LP lock percentage, identifying which big accounts are AMM pools (so the Raydium or pump.fun vault does not count as a "whale"), fallback holder shares, and fallback mint data. If RugCheck is down, LP becomes UNKNOWN and every token is skipped. That is the intended fail-safe direction. Its documented anonymous limit is 1 request per second (the response header advertises a bucket of 15), so the bot spaces calls at that rate and backs off on 429.
- **pump.fun bonding-curve tokens** have no liquidity figure on DEX Screener (the `liquidity` key is absent), so the liquidity rule is UNKNOWN for them and they are skipped until they graduate to a real pool. Expect many skips for that reason.
- **Paper fills are optimistic.** A 1% haircut per side is applied, but a real thin pool moves against you when you trade it, and fees vary. Treat paper results as "the code runs", not "the strategy works".
- **Each token is scored once**, including tokens that errored. The feed shows the same tokens for a long time, so this keeps API usage small; it also means a transient outage can make the bot skip a token for good.
- **"Day" is the UTC calendar day** for the daily loss limit.
- **Without `XAI_API_KEY`** every token stops at the Grok step with `grok unavailable: XAI_API_KEY not set`. No key, no paper trades: the Grok read is a required gate, not a bonus. A wrong key shows up as `xAI rejected the API key (400)`: xAI answers 400, not 401, for an incorrect key.
- **Grok costs real money per call.** Each reply's exact cost (from xAI's `usage.cost_in_usd_ticks`) is logged as `grok.cost_usd` and totalled on the dashboard. Only all-PASS tokens are sent, and the request asks for low reasoning effort; expect well under a cent per call at the published grok-4.7 prices, but watch the tile.

## 9. What was verified, and what was not

Checked on 2026-11-09, by live probes from this environment and by reading the
official documentation pages, while writing the code:

| Source | Verified |
|---|---|
| DEX Screener `GET /token-profiles/latest/v1` | Returns a list; `chainId`, `tokenAddress`, `url`, `icon`, `description`, `links`; documented 60 req/min; `cache-control: max-age=60`; no key |
| DEX Screener `GET /token-pairs/v1/solana/{token}` | Returns a list of up to 30 pairs (no pagination); the token can be the base OR the quote side, so the bot keeps base-side pools; `pairAddress`, `dexId`, `priceUsd` (string), `liquidity.usd` (absent for bonding-curve pairs), `pairCreatedAt` (ms since epoch), `fdv`, `marketCap`, `volume.h24`, `txns.h1.buys/sells`; documented 300 req/min; unknown token returns `[]` |
| Solana RPC | `https://api.mainnet.solana.com` is the documented public endpoint (`api.mainnet-beta.solana.com` resolves to the same node); `getTokenSupply`, `getAccountInfo` (jsonParsed: `data.parsed.info.mintAuthority / freezeAuthority / supply / decimals`, disabled authority is JSON `null`) for both the classic Token program and Token-2022; `getMultipleAccounts` for owners; `getTokenLargestAccounts` refused on the free tier (`x-ratelimit-method-limit: 0`, JSON-RPC error 429 inside HTTP 200); documented limits 100 req/10 s per IP, 40 per method |
| RugCheck `GET /v1/tokens/{mint}/report` | No key needed; documented anonymous limit 1 request/second (headers advertise 15 per window); fields the bot reads: `markets[].pubkey` (equals the DEX Screener `pairAddress` for the same pool), `markets[].lp.lpLockedPct`, `markets[].lp.baseUSD/quoteUSD` (to pick the biggest market when no pubkey matches), `markets[].marketType`, `knownAccounts` (typed `AMM` / `CREATOR` / `LOCKER`), `topHolders[].owner/pct` (list can be `null`), `token.mintAuthority/freezeAuthority/supply`, `tokenProgram`, `totalHolders`, `risks[].name/level`, `score_normalised`; `rugged` is deprecated and not used; garbage mint returns 400 |
| xAI | `POST https://api.x.ai/v1/chat/completions` with `Authorization: Bearer` (xAI now labels this endpoint legacy/deprecated in favour of `/v1/responses`, with no retirement date); model `grok-4.7` exists at $2 / 1M input and $6 / 1M output tokens, reasoning tokens billed as output (grok-4.3 is cheaper); `reasoning_effort` low/medium/high/xhigh; structured output via `response_format` `json_schema` (primary) or `json_object`; 401 with no credentials, **400 `invalid-argument` for a wrong key**, 429 for rate limits; reply at `choices[0].message.content`, a refusal at `choices[0].message.refusal`, exact cost at `usage.cost_in_usd_ticks` |

Not verified here:

- No paid Grok call was made, so the exact 429 headers and whether every model accepts `json_schema` were not observed. If a model rejects it (400), the bot retries once as plain text. If the model name is rejected, the error appears in the log as `grok unavailable: HTTP 4xx ...`; the fix is a one-line change in `config.json`.
- RugCheck's rate-limit window behind the `x-rate-limit-limit: 15` header, and whether `lpLockedPct` counts burned LP (there is no explicit "burned" field; locked and burned are both treated as locked here). On pump.fun bonding-curve markets there is no LP token at all (RugCheck still reports 100% "locked" because the curve is program-controlled); the figure is not comparable with a real pool, so the bot reports LP as UNKNOWN for them, which is the conservative direction.
- Any real-money result. No backtest exists, and no edge is claimed. On the demo the bot loses a few dollars to its own stop-loss, which is the honest result for a test with fictional data.

## 10. Tests

```bash
cd grok-trader
pip install pytest
python -m pytest
```

The tests use fake HTTP sessions and never touch the network. They cover the
rules (including "UNKNOWN never passes"), the decision gate ("a low Grok risk
cannot override a failed rule"), the paper broker (sizing, stop, target, daily
halt, persistence), the Grok reply parser, feed parsing, RPC error handling, the
RugCheck fallbacks, the offline demo and the dashboard (including HTML escaping
of token names).

## 11. Files

```
grok-trader/
  src/main.py        Entry point, scan loop, --demo
  src/feed.py        DEX Screener feed, pool snapshot, rate limiting, seen-token cache
  src/onchain.py     Solana RPC reads, RugCheck report, gather() that assembles the numbers
  src/checks.py      The hard PASS/FAIL/UNKNOWN rules (pure functions)
  src/grok.py        Grok prompt, call, strict reply parsing
  src/scoring.py     Score + the strict buy/skip gate
  src/paper.py       Paper broker: balance, positions, stop/target, daily loss halt
  src/journal.py     decisions.jsonl writer and reader
  src/dashboard.py   One-page Flask dashboard (read-only)
  tests/             pytest suite, offline
  config.json        Every threshold and risk limit
  .env.example       Placeholder keys
  data/              Paper state and seen-token cache (created on first run, git-ignored)
  logs/              decisions.jsonl (created on first run, git-ignored)
```

Roughly 1,000 code lines in `src/` (about 1,350 with comments, docstrings and
the dashboard template) against the guide's "~600" target. The extra is retries
and backoff, the RugCheck fallbacks, input validation, the security guards and
the demo fixtures. Two helper modules (`onchain.py`, `journal.py`) exist beyond
the guide's file list so that each file stays single-purpose.
