# osrs-ge-analyser

Grand Exchange analyser for Old School RuneScape. Pulls live prices and a local history
of 5-minute market windows from the
[OSRS Wiki real-time prices API](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices)
and tells you what to buy and sell.

Commands:

| Command | What it does |
|---------|--------------|
| `execute` | **Execution loop.** Reads your order status from `status.json`, writes what to do next (`BUY` / `SELL` / `HOLD` / `CANCEL` / `COLLECT`, with item, quantity and price) to `orders.json`. You place the offers; it plans within your GE slots, 26 inventory stacks, cash and buy limits. |
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

## Execution loop: it says what to do, you do it

`execute` runs the tool as a trading desk. It never touches the game: it reads a
**status file you write** (your GE orders and how far each has filled) and answers
with an **orders file** saying exactly what to do next. You click; it plans, counts the
coins, keeps the books, and measures its own model against what really filled.

```sh
ge-analyser execute --init --cash 250k   # writes ~/.osrs-ge-analyser/exec/status.json
ge-analyser execute                      # leave it running: answers every 60 s and the moment status.json changes
ge-analyser execute --once               # ...or answer the current status once and exit
ge-analyser execute --report             # profit so far, the book, stock held, real fills vs the model
```

The folder (`--dir`, or `GE_ANALYSER_EXEC`) holds three files: `status.json` (yours),
`orders.json` (the answer), `state.json` (the engine's memory; do not edit).

### What it tells you: `orders.json`

Do the actions top to bottom.

| Action | Meaning |
|---|---|
| `COLLECT` | This order finished or was aborted. Collect it; its slot is free. |
| `CANCEL` | Abort this offer and collect what it holds. |
| `SELL` | Place a sell offer: `item`, `qty`, `price`. `collect_first` lists buy offers whose items to take out first. If you hold fewer than `qty`, sell what you hold. |
| `BUY` | Place a buy offer: `item`, `qty`, `price`. `then_sell_at` is the planned ask. |
| `HOLD` | Leave this offer, or this stack in your inventory, alone. |

```json
{
  "time": "2026-11-10 23:12:40", "seq": 3, "status_age_s": 1,
  "account": {"cash": 1529, "cash_after_actions": 29, "cash_correction": 0, "in_buy_offers": 6890, "stock_value": 1872, "equity_est": 10291, "realised": 275,
              "ge_slots_used": 8, "ge_slots": 8, "inventory_stacks": 8, "inventory_capacity": 26, "items_allowed": "all"},
  "actions": [
    {"step": 1, "action": "COLLECT", "order_id": 2, "item": "Iron dart", "side": "buy", "qty": 312, "price": 4, "filled": 312,
     "why": "executed: 312/312 bought; collect to free the slot"},
    {"step": 3, "action": "SELL", "order_id": 10, "item": "Iron dart", "item_id": 807, "side": "sell", "qty": 312, "price": 6,
     "cost_each": 4.0, "profit_each": 2.0, "why": "list bought stock: cost 4.0, +2.0/ea after tax"},
    {"step": 4, "action": "BUY", "order_id": 11, "item": "Watermelon seed", "item_id": 5321, "side": "buy", "qty": 30, "price": 50,
     "then_sell_at": 62, "profit_each": 11, "why": "+11/ea selling at 62 (22.0%), fill chance 37%, 637 traded/h"},
    {"step": 5, "action": "HOLD", "order_id": 3, "item": "Adamant dart", "side": "buy", "qty": 56, "price": 22, "filled": 0,
     "why": "buying 0/56, open 0m"}
  ],
  "warnings": [],
  "reply_template": {"members": true, "slots": 8, "inventory_capacity": 26,
                     "orders": [{"order_id": 3, "item": "Adamant dart", "side": "buy", "status": "open", "filled": 0}]},
  "calibration": {"finished_buy_orders_measured": 0, "real_fill_vs_model": null, "order_size_scale": 1.0}
}
```

### What you tell it: `status.json`

Each minute:

1. Press **Collect** at the GE (coins and items from every offer to your inventory).
2. Copy `reply_template` over `status.json` and fix each order's `status` and `filled`.
   The template holds your account settings and the orders the engine expects you to
   have once the batch is done.
3. Do what the new `orders.json` says.

```json
{
  "orders": [
    {"order_id": 10, "status": "filling", "filled": 120},
    {"order_id": 11, "status": "open"},
    {"order_id": 3,  "status": "executed"}
  ]
}
```

You do not type your coins: `account.cash` is the engine's count, and
`cash_after_actions` what you should hold once the batch is placed. If your coin stack
says otherwise, add `"cash": <your coins>` to the status once.

| Field | |
|---|---|
| `cash` | Coins in your inventory (numbers or `"250k"`). **Needed once, in the first status.** After that the engine counts your coins itself from the fills and offers you report. Give it again only to correct the count: a changed number replaces it (the answer shows `cash_correction`); the same number left in the file is ignored. |
| `orders[]` | Every offer of yours that is in the GE, plus each one that finished or was aborted — report those **once**. |
| `orders[].order_id` | The id from `orders.json`. Or give `item` + `side` (`buy`/`sell`) instead. |
| `orders[].status` | `open` (nothing yet), `filling`, `executed`, `cancelled`. Synonyms work: `pending`, `partial`, `filled`, `bought`, `sold`, `aborted`, RuneLite's `BUYING`/`BOUGHT`/`SELLING`/`SOLD`/`CANCELLED_BUY`/`CANCELLED_SELL`. |
| `orders[].filled` | Units traded so far. `executed` without a count means completely filled. |
| `orders[].price`, `.qty` | Only if you placed something different from the instruction. `spent` (coins paid/received so far) gives exact average prices. |
| `members` | `false` = free-to-play: F2P items only, 3 slots. Default `true` (or `--f2p`). |
| `slots`, `inventory_capacity` | GE slots and item stacks you can use. Defaults 8 (3 F2P) and **26**. These three settings stick: leave them out and the last values given stay in force. |
| `inventory` | Optional: `[{"item": "Iron dart", "qty": 312}]`. If you give it, give all of it — it is used to settle orders that vanished without a final report and to count stacks the engine does not manage. |

The file may be UTF-8 or UTF-16 (PowerShell). If it cannot be understood, `orders.json`
carries an `error` saying what to fix and no actions.

### How it behaves

- **Only your status moves the books.** Nothing is assumed bought or sold until you
  report it. An instruction that is not in your status yet is repeated unchanged — same
  order id, same price — for 3 minutes, then re-planned at current prices. If you place
  a withdrawn instruction after all and report it, it is taken back on the book.
- **Buys**: bid 1 gp over the instant-sell. Cancelled after 30 min without a fill, or as
  soon as the market has moved so the bid would lose money after tax. A cancel frees the
  slot and the coins for a replacement in the same batch.
- **Sells**: listed 1 gp under the instant-buy as soon as the buy finishes (or once a
  still-filling buy has delivered 20% of its value). After 30 min without a sale it is
  relisted at the market, after 90 min at the instant-sell price — a loss is taken and
  reported as one rather than left to sit. One buy and one sell offer per item at a time.
- **An order missing from your status** is not guessed at: for 2 minutes the answer
  holds its slot and asks you to report it (`"needs_status": true`); after that it is
  assumed cancelled at the last count you gave, with a warning.
- **Limits it plans within**: GE slots shared by buys and sells, inventory stacks (one
  per item type — collect as notes; a new item is bought only if a stack is free for
  it), your cash, and 4-hour buy limits (fills are logged to the `bought` ledger).
- **No dust orders**: with less than 2% of your equity loose, nothing new is bought.
  The change waits for a sale instead of being spread over every free slot. An order
  the market itself keeps small (thin volume, buy limit) is still placed.
- **Prices it refuses to believe**: an item with no trading history is never traded,
  and no ask is planned above twice the item's usual margin over cost (`--margin-cap`).
  Old school bonds are never suggested: a bond bought on the GE arrives untradeable and
  costs 10% of its price to convert back. See "What re-evaluating the model found".
- **What it does not know**: connected buy limits (potion doses), offers you place
  outside it (they only count as used slots), and the sell restrictions on new
  free-to-play accounts (under 20 hours played, 10 quest points or total level 100 some
  items cannot be sold on the GE). Live prices are single last trades; backtests see
  5-minute averages.

### What re-evaluating the model found

```sh
ge-analyser execute --backtest 168 --compare --budget 1m     # the table below, for your stack
ge-analyser execute --backtest 168 --budget 1m --play 19-23  # at the GE 19:00-23:00 only; offers fill in between
```

`--backtest` replays history through the **same engine** with a paper broker that obeys
GE mechanics: coins reserved, slots shared, Collect once per cycle, 26 stacks. The
engine counts its own coins as in the live loop and the broker checks the count (never
off by a coin in any run below). What it showed:

1. **One freak print carried the old results.** During the first live paper run a junk
   item (Steel knife(p+)) printed ~5,500 units at 49,999 gp in 20 minutes — almost
   certainly coins being moved between two accounts. The paper agent "bought at 501 and
   sold at 49,998"; that single trade was 7.8m of its 8.9m profit (87%). Items without
   history are now never traded, and asks are capped at twice the usual margin.
2. **Bonds were the "best flip" for a big stack.** With 10m or more to spend, up to 91%
   of the backtest profit came from Old school bonds, which cannot be flipped at all.
   They are excluded.
3. **The result hangs on one unknown: the share of volume you get.** The paper model
   gives an order 1/N of the volume that crosses its price. Profit over one week
   (Nov 3–10), all items, 8 slots, at the GE around the clock:

   | Start | N = 10 | N = 30 | N = 100 |
   |---|---|---|---|
   | 10k | +1.50m | +539k | +182k |
   | 1m | +2.24m | +1.03m | +369k |
   | 10m | +2.43m | +891k | +368k |
   | 100m | | +891k | |
   | 10k, free-to-play (3 slots) | +127k | +103k | +72k |

   Start the same run 5–20 minutes later and the profit moves by about ±10% (491k–586k
   for the 10k / N = 30 cell), so smaller differences mean nothing. No single item is
   more than 14% of the profit at 10k; with 1m+ and N = 100, Chaos runes are 47%.
4. **Profit is capped by market flow, not by your stack.** Orders are sized to what an
   item really trades, so from about 1m up a bigger stack earns the same gp: 100m makes
   exactly what 10m makes. Admitting thinner, pricier items (`--min-volume 10`) did not
   help in a 24-hour test. This is a strategy for cheap, liquid items.
5. **It is a lot of clicking for the gp.** About 34 offers placed or aborted per hour
   with 8 slots (14 with 3). Around the clock that is 1k–14k gp per hour at the GE
   depending on N and stack. Four hours a day (`--play 19-23`, N = 30) keeps 26–40% of
   the weekly profit for a 1m+ stack because offers fill while you are away (+271k from
   1m, 9.7k gp/h at the GE); a 10k stack loses its compounding (+51k instead of +539k).
6. **Real mechanics cost 12–26% against the old simulator** on the same data and N
   (+1.50m vs +1.71m from 10k, +2.24m vs +3.01m from 1m; nothing with 3 free-to-play
   slots). The 26-stack limit never binds: at most 8 stacks were ever held.

What no backtest can know is N. The loop measures it: `execute --report` compares each
finished order with what the paper model gives for the same order over the same
5-minute windows. After 10 finished buy orders that ratio starts scaling order sizes
(per item after 3); below 1.0 means the table overstates, and by how much.

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

## Paper-trading agent (the older fill simulator)

`simulate` is the self-contained paper model: it places simulated offers from the same
ranking and fills them itself against the *next* real price prints. It is handy for
quick what-ifs and unattended paper runs; the execution engine above is what is
evaluated and used for real orders. Where it differs from the engine: a bought lot is
listed for sale instantly and its sell offer does not need a free slot, every crossing
window fills at least one unit, and there is no inventory limit. It shares the engine's
guards (no history → no trade; asks capped at twice the usual margin).

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
offer on each side rather than one of many at the print (`--no-improve` quotes the
exact prints; in this paper model that is 2.8× worse over the same 96 h, +2,135% vs
6,055%). Buys are cancelled after 30 min unfilled; sells are re-priced to the current
instant-buy after 30 min and dumped at instant-sell after 90 min so nothing sits
forever. `--min-stake N` concentrates a small stack into fewer slots (neutral in
backtests; off by default).

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

Its absolute returns are not a forecast: they scale with the assumed fill share exactly
as the execution-loop table shows, and before the guards existed a single freak print
produced 87% of one paper agent's profit.

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
< 12 windows overall are illiquid by definition: `Fill = 1%`, at most 10% of the price is
believed as margin, and the trading engines skip them entirely.

Score: `Expect = min(margin, Typ) × Qty × Fill`, then
`score = Expect × 1/(1 + age_h) × (0.5 + ln(1 + liquidity)/10) × 1/(1 + Vol% / ROI%)`.
Without history (`--no-stats`) the legacy score is `profit × staleness × liquidity`.

Does it help? `simulate --backtest H --compare` runs time-of-week features, flat
features and no history side by side. An earlier version of this page quoted a 3.5×
edge over a week from that comparison; it is withdrawn. Re-run on the current model the
three variants are within 5% of each other over 72 h (+3,106% / +2,942% / +3,063%) and
the no-history variant cannot be compared fairly at all, because without history it has
no protection against freak prints and a week containing one decides its result. What
history demonstrably buys is that protection — the engines refuse to trade without it.

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
