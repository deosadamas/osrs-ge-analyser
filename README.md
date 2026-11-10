# osrs-ge-analyser

Grand Exchange analyser for Old School RuneScape. Pulls live prices and a local history
of 5-minute market windows from the
[OSRS Wiki real-time prices API](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices)
and tells you what to buy and sell.

Five commands:

| Command | What it does |
|---------|--------------|
| `flips` | Ranks items by instant-sell → instant-buy margin **after the 2% GE tax**, bounded by buy limit, your budget and last-hour volume. |
| `item`  | Full detail for one item: current spread, hourly volume, 24h and ~3-month trend with a buy/sell/neutral verdict. |
| `dips`  | Scans the most liquid items and flags ones trading ≥ N standard deviations below their 24h mean (mean-reversion buys). |
| `bought` | Records what you've bought so remaining 4-hour buy limits are honoured by `flips` and `item`. |
| `simulate` | Paper-trading agent: runs the ranking on a schedule, places simulated offers, fills them against real later prices. Live loop or 24h+ backtest. |
| `sync` | Imports your real trades from RuneLite's Flipping Utilities: fills the buy-limit ledger, reports FIFO P&L and fill times, calibrates the simulator's fill model. |

No third-party dependencies; Python 3.10+.

## Install

```sh
git clone https://github.com/deosadamas/osrs-ge-analyser
cd osrs-ge-analyser
pip install -e .
```

## Usage

```sh
# Best flips for a 10m cash stack, F2P items only
ge-analyser flips --budget 10m --f2p

# Stricter: 2%+ ROI, at least 500 traded/hour, prices under 15 min old
ge-analyser flips --budget 50m --min-roi 2 --min-volume 500 --max-age 15 --top 30

# One item
ge-analyser item "abyssal whip"
ge-analyser item 4151

# Items currently dumped below their 24h average
ge-analyser dips --budget 20m --scan 100 --z 1.5
```

Example `flips` output:

```
Item               Buy     Sell   Margin   ROI  Limit  Vol/h  Qty   Profit  Age
---------------  -----  -------  -------  ----  -----  -----  ---  -------  ---
Dragon bones     2,650    2,789       83  3.1%  11000  3,120  780   64.7k   2m
```

- **Buy**: place your buy offer at this price (the current instant-sell price).
- **Sell**: place your sell offer at this price (the current instant-buy price).
- **Margin**: `Sell − tax − Buy`. Tax is 2% of the sell price, rounded down (so < 50 gp is untaxed), capped at 5m per item; bonds, energy potions, bronze/iron/steel arrows & darts, mind runes, low-level food, basic teleport tablets/jewellery and skilling tools are [exempt](https://oldschool.runescape.wiki/w/Grand_Exchange#Exempt_from_tax).
- **Qty**: `min(buy limit, budget / Buy, Vol/h ÷ 4)`; assumes you capture at most a quarter of an hour's volume.
- **Profit**: `Margin × Qty` for one 4-hour buy-limit cycle.
- **Age**: time since the older of the two prices; stale prices are scored down.

## Buy-limit tracking

GE buy limits are per item per rolling 4-hour window. The analyser is stateless about
the game, so tell it what you bought and it will cap `Qty` at what you have left and
drop items whose limit is exhausted:

```sh
ge-analyser bought "ruby necklace" 11000   # Recorded ... 11,000/18,000 used, 7,000 left; resets in 4h00
ge-analyser bought                         # table of in-window purchases and when each resets
ge-analyser bought --clear [item]          # forget purchases (all, or one item)
ge-analyser --ignore-ledger flips          # one-off: pretend limits are fresh
```

Purchases live in `~/.osrs-ge-analyser/ledger.json` (override with `GE_ANALYSER_LEDGER`)
and expire 4 hours after they were recorded. The `Limit` column shows `left/limit` when
part of a limit is used. Connected limits (e.g. Prayer potion doses sharing one limit)
aren't in the API data and are not modelled.

## Paper-trading agent

`simulate` runs the flip ranking on a schedule, places simulated GE offers, and fills
them against the *next* real price prints — with tax, buy limits and GE slot caps.

```sh
# Replay the last 24h of 5-minute history with a 10k start (results in seconds)
ge-analyser simulate --budget 10k --backtest 24

# Trade live: one tick per 5 minutes, forever (Ctrl-C to stop; state persists)
ge-analyser simulate --budget 10k --ticks 0
ge-analyser simulate --status     # portfolio, open offers, recent events
ge-analyser simulate --reset      # start over
```

Strategy: for each free slot, take the best-scoring flip the ranking produces with
`budget = cash / free_slots`, bid at the current instant-sell price and, once filled,
ask at the instant-buy price seen at fill time. Buys are cancelled after 30 min unfilled;
sells are re-priced to the current instant-buy after 30 min and dumped at instant-sell
after 90 min so nothing sits forever.

Fill model (conservative by default, tunable):

| Rule | Flag |
|------|------|
| A buy fills only when a later print's instant-sell goes **below** our bid; a sell only when instant-buy goes **above** our ask (price-time priority: at a touched price you're queued behind everyone already there). | `--loose` to fill on touch (very optimistic) |
| Each print fills at most 1/N of that window's one-sided volume. | `--fill-share N` (default 10) |
| Fills happen at our offer price, never the better print. | — |
| Only enter flips with ≥ 2 gp and ≥ 2% margin after tax; 1-gp spreads on bot items aren't real money for a solo flipper. | `--min-margin`, `--min-roi` |

Reference: on a 24h replay of the 40 most liquid items, 10k start → ~12.6k (+26%)
under the default model, +57% F2P-only with 3 slots, −13% if 1-gp spreads are allowed,
and a meaningless +31,000% with `--loose`. Treat the default as a floor-ish estimate
and `--loose` as a ceiling nobody reaches.

State lives in `~/.osrs-ge-analyser/sim.json` (override with `GE_ANALYSER_SIM`).

## Syncing real trades from RuneLite

Install **Flipping Utilities** from the RuneLite Plugin Hub. It records every GE offer on
your account (no automation; it only reads game state) to
`~/.runelite/flipping/<DisplayName>.json`. Then:

```sh
ge-analyser sync                 # all accounts in ~/.runelite/flipping
ge-analyser sync path/to/Acc.json --days 30
ge-analyser sync --calibrate     # also compare each fill with the wiki print at that time
```

What it does:

1. **Buy limits** — every real purchase in the last 4 h goes into the ledger (tagged per
   account, idempotent; manual `bought` entries are kept), so `flips`/`item` show your
   true remaining limits with zero bookkeeping.
2. **P&L report** — FIFO-matched realised profit per item after the 2% tax, units
   bought/sold, unsold inventory, median time-to-fill for buys and sells, and how full
   cancelled offers were when you pulled them.
3. **Calibration** (`--calibrate`) — for each completed fill, fetches the wiki price at
   that moment and asks: did you pay *below* the instant-sell / receive *above* the
   instant-buy (the market had to cross you — a "strict" fill), or were you at/above the
   print (a "loose" fill)? The share of strict fills tells you which `simulate` fill model
   matches your account, and the median edge tells you how much better than the instant
   price your offers actually did.

The schema is taken from the plugin's source (Gson field names `b`, `id`, `cQIT`, `p`,
`t`, `st`, `tSFO` …). It was verified against a synthetic file, not a real export — if a
real file fails to parse, open an issue with the file attached.

## Local market history and how ranking works

Every command that ranks items keeps a local SQLite store of **5-minute windows for all
items**, pulled from the wiki's bulk `/5m?timestamp=` endpoint (one request per window,
all ~2,000 traded items at once). First run fetches the last 3 days (~860 requests, ~1
min); afterwards each run tops up only the windows it is missing.

```sh
ge-analyser history --days 7     # backfill / inspect coverage
ge-analyser --history-days 7 flips   # use a week of history for the stats
ge-analyser --no-stats flips         # legacy scoring without history
```

From that history, three per-item features are computed (cached for 30 min):

| Feature | Question it answers |
|---|---|
| `fill_buy`, `fill_sell` | In the last N days, how often did a bid at a window's instant-sell get **crossed** by a later window within 30 min (and likewise an ask at the instant-buy)? Same rule the simulator uses, so `Fill = fill_buy × fill_sell` is its round-trip fill probability. |
| `median_margin` (`Typ`) | The typical after-tax spread. Today's margin above it is treated as a spike and capped. |
| `volatility_pct` (`Vol`) | Std-dev of 5m mid-price moves. A margin smaller than this is noise. |

Score with history: `Expect = min(margin, Typ) × Qty × Fill`, then
`score = Expect × 1/(1 + age_h) × (0.5 + ln(1 + vol/h)/10) × 1/(1 + Vol% / ROI%)`.
Items with fewer than 12 two-sided windows in the period get `Fill = 1%` (they are
illiquid by definition) and sink. Without history (`--no-stats`) the legacy score is
`profit × staleness × liquidity`.

Does it help? Same strict fill model, same 10k start, all items, features computed only
from windows *before* each replay (`simulate --backtest H --compare`):

| Horizon | History features | Legacy | Win rate (hist / legacy) |
|---|---|---|---|
| 24h | +119% | +81% | 61% / 47% |
| 48h | +533% | +696% | 62% / 51% |
| 72h | +1,507% | +594% | 60% / 43% |
| 96h | +1,874% | +506% | 68% / 60% |
| 72h, F2P, 3 slots | +159% | +26% | 57% / 48% |

Four of five horizons favour the features and the win rate is higher in all of them.
Absolute returns are backtest numbers on a tiny stack and compound; read the ratio, not
the magnitude.


## Caching

Live responses are cached in `.cache/` (60 s for prices, 5 min for timeseries, 24 h for
item mapping); `--no-cache` bypasses it. Market history lives in
`~/.osrs-ge-analyser/history.db` (override `GE_ANALYSER_HISTORY`). The wiki asks only for
a descriptive User-Agent, set in `ge_analyser/api.py`; the client uses the v2 API.

## Development

```sh
pip install -e . pytest
pytest
```

## Disclaimer

Prices move; margins shown are a snapshot, not a guarantee. Other flippers see the
same data. Nothing here interacts with the game client.
