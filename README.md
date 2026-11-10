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
| `history` | Backfills / tops up the local market history: 5m windows (30 d), 1h windows (365 d) and daily series (1 y) for every item. |
| `peaks` | When the market, or one item, actually trades: volume / spread / fill rate by hour of day and weekday. |
| `advise` | Publishes the current suggestions + your remaining limits as JSON every minute for the RuneLite panel. |
| `tune` | Walk-forward sweep of the scoring constants; says whether anything beats the defaults out of sample. |

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
`budget = cash / free_slots`, bid **1 gp above** the current instant-sell and, once
filled, ask **1 gp below** the instant-buy seen at fill time — you are then the best
offer on each side rather than one of many at the print (`--no-improve` to quote the
exact prints; 4× worse in backtests: +1,935% vs +7,841% over the same 96h). Buys are
cancelled after 30 min unfilled; sells are re-priced to the current instant-buy after
30 min and dumped at instant-sell after 90 min so nothing sits forever. `--min-stake N`
concentrates a small stack into fewer slots (neutral in backtests; off by default).

`ge-analyser tune` runs a walk-forward sweep of the scoring constants (liquidity weight,
volatility penalty, bucket shrinkage) over several past windows, ranks candidates on the
older folds and confirms on the newest, and tells you whether anything beats the
defaults out of sample.

`dips` now consults the daily history: items whose last week sits ≥ 15% away from the
prior two months on ≥ 5 of 7 days are structural re-levels (game updates), not dips, and
are hidden unless you pass `--include-shifts`.

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

## In-game panel (RuneLite plugin)

`runelite-plugin/` is a small sideloadable RuneLite plugin: a side panel listing the
current suggestions (item, buy→sell, margin, qty, remaining limit, fill probability) fed
by `ge-analyser advise`, plus a recorder that logs your real GE offers for `sync`. It
reads game state and draws; it never places offers. See `runelite-plugin/README.md`.

```sh
ge-analyser advise --budget 10m --top 15      # refreshes ~/.osrs-ge-analyser/advice.json every 60 s
cd runelite-plugin && ./gradlew jar           # build/libs/ge-analyser-0.1.0.jar
```

## Syncing real trades from RuneLite

Either the bundled plugin above (writes `~/.osrs-ge-analyser/runelite-trades.jsonl`) or
**Flipping Utilities** from the Plugin Hub (writes `~/.runelite/flipping/<DisplayName>.json`).
Both only read game state. Then:

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

## Local market history

Every ranking command keeps a local SQLite store (`~/.osrs-ge-analyser/history.db`,
override `GE_ANALYSER_HISTORY`) filled from the wiki's bulk endpoints — one gzip request
per window for **all** items, rate-limited to 6 req/s:

| Table | Source | Default depth | Requests | Used for |
|---|---|---|---|---|
| 5m windows | `/5m?timestamp=` | 30 days | ~8,600 | fill rates, spreads, intraday timing |
| 1h windows | `/1h?timestamp=` | 365 days | ~8,800 | hour-of-week volume, market `peaks` |
| daily | `/timeseries?lookback=1y` per item | 1 year | ~3,700 | long-run trend |

The API actually serves 5+ years at every resolution; these defaults are what the
models need, not what's available. Raise them with `history --days-5m/--days-1h`.

```sh
ge-analyser history                  # full backfill once (~45 min), then incremental
ge-analyser --history-days 7 flips   # stats window (default 14 days)
ge-analyser --no-stats flips         # legacy scoring without history
```

## When does the market move? (`peaks`)

```sh
ge-analyser peaks                    # 24 x 7 heat table of market-wide volume (1h data, 90 days)
ge-analyser peaks "mahogany logs"    # per hour of day: volume, spread, P(buy fills), P(sell fills)
```

Example: Mahogany logs trade ~3× more at 01–04 UTC than at 10 UTC, and the chance a
bid at the instant-sell gets filled within 30 min swings between 60% and 87% by hour.
That is the signal the ranking uses below.

## How ranking works

From the 5m history, per item, four features are computed overall **and for each of 12
time-of-week buckets** (six 4-hour UTC blocks × weekday/weekend), cached for 6 hours:

| Feature | Question it answers |
|---|---|
| `fill_buy`, `fill_sell` | How often did a bid at a window's instant-sell get **crossed** by a later window within 30 min (and likewise an ask at the instant-buy)? Same rule the simulator uses, so `Fill = fill_buy × fill_sell` is its round-trip fill probability. |
| `median_margin` (`Typ`) | The typical after-tax spread. Today's margin above it is treated as a spike and capped. |
| `volatility_pct` (`Vol`) | Std-dev of 5m mid-price moves. A margin smaller than this is noise. |
| `typical_volume` | Median one-sided units/hour. The liquidity planned on is `min(live hourly volume, typical for this time of week)`, so a Sunday-evening volume spike doesn't size Tuesday-morning offers. |

A bucket is used for an item only when it has ≥ 72 two-sided windows (6 hours of
prints), and its estimate is shrunk toward the item's overall estimate with weight
`n / (n + 288)` — a day of prints is trusted half-way, a week 7/8. Without shrinkage the
raw bucket rates were too noisy on 14 days of data and lost to flat features. Items with
< 12 windows overall are illiquid by definition: `Fill = 1%` and they sink.

Score: `Expect = min(margin, Typ) × Qty × Fill`, then
`score = Expect × 1/(1 + age_h) × (0.5 + ln(1 + liquidity)/10) × 1/(1 + Vol% / ROI%)`.
Without history (`--no-stats`) the legacy score is `profit × staleness × liquidity`.

Does it help? Same strict fill model, same 10k start, all items, features computed only
from windows *before* each replay (`simulate --backtest H --compare` runs three
variants: time-of-week features, flat features, legacy):

| Replay (14 d of features) | Time-of-week | Flat features | Legacy |
|---|---|---|---|
| 24h | **+149%** | +101% | +104% |
| 72h | +1,380% | **+1,444%** | +1,379% |
| 96h | **+1,950%** | +1,721% | +1,745% |
| 168h | **+5,923%** | +4,959% | +1,705% |
| 96h, F2P, 3 slots | +178% | **+232%** | +155% |

Time-of-week wins three of five, flat two; both beat legacy in every run, and the gap
opens with horizon (3.5× over a week). The remaining noise between the two feature
variants is what more history (the store keeps growing) and your real fills (`sync
--calibrate`) are for.

Absolute returns are backtest numbers on a tiny stack and compound; read the ratios and
win rates, not the magnitudes.

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
