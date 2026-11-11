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
ge-analyser execute                      # leave it running: it answers the moment status.json changes
ge-analyser execute --speak              # ...and has the next thing to do read aloud (see below)
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
| `SELL` | Place a sell offer: `item`, `qty`, `price`. `collect_first` lists buy offers whose items to take out first. `all: true` means `qty` is every unit of the item you hold. If you hold fewer than `qty`, sell what you hold. |
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
  "calibration": {"finished_buy_orders_measured": 0, "share_assumed": "1/30", "real_fill_vs_model": null, "share_measured": null, "order_size_scale": 1.0}
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

### Or let RuneLite write it

With the [plugin](runelite-plugin/README.md) loaded you never touch `status.json`: it
rewrites the file from the client every time an offer, your coins or your inventory
changes (and once a minute otherwise). It reads; it never clicks.

```sh
ge-analyser execute        # leave it running next to RuneLite; no --init needed
```

Then the whole job is: do what it says, top to bottom. The plugin's side panel shows the
same list next to the game (what to collect, abort, sell and buy, then the offers to
leave alone), redrawn within a second or two of every change, so the terminal can stay
out of sight.

- **Coins are exact.** `cash` is what is in your inventory (platinum tokens count as
  1,000 each), read every time, so nothing has to be corrected. Coins still sitting in
  an offer are not counted until you collect them.
- **`COLLECT` stays until it is done.** The plugin sees a finished offer sitting in its
  slot, so the instruction is repeated until the slot is empty, and nothing is booked
  twice. A finished offer of your own is pointed out the same way.
- **No order ids.** The game does not know the engine's ids, so the plugin names every
  offer itself (`ref`) and says when it saw it placed (`since`). An offer is taken as an
  instruction's when it was placed after the instruction was given, or is running with
  exactly the instructed price and size. A finished offer that was already sitting
  there never counts as a new instruction's fill.
- **Your own offers are left alone**, and the engine does not buy an item you already
  have an offer in.
- **One account per session.** A status for another account is refused; give that
  account its own folder (plugin setting *Status file*, `--dir`).
- `inventory_capacity` is not something the client knows: set it with
  `--inventory-slots` (default 26). Members worlds and your 8 or 3 slots are read off
  the client.

Extra fields the plugin writes: `source: "runelite"`, `account`, and per offer `slot`,
`ref`, `since`, `spent` and — for a finished offer kept listed for 10 minutes after you
collected it, so its final count cannot be missed — `collected: true`.

### Where the instructions appear

- **Terminal.** The list is printed when what you have to do changes, and once a
  minute. A fill that changes nothing for you does not scroll it.
- **RuneLite side panel.** The same list next to the game, with the plugin.
- **Read aloud**, with `--speak`: the next thing to do, one sentence at a time, as a
  line `epoch-ms<TAB>sentence` appended to a file that a screen-reading tool tails.
  The default file is `~/.runelite/skillassist-alerts.log`, which the OSRS Access
  Panel already speaks; `--speak FILE` writes elsewhere. A sentence is written when
  - there is something to do after there was nothing: *Grand Exchange: 3 things to
    do. Collect Iron ore: 387 bought.*
  - you have done one, and this is the next: *Sell all 387 Iron ore at 46 each.*
  - something more urgent has come to the top: an offer that finished, one to abort;
  - you have done everything: *Grand Exchange: nothing left to do.*

  An instruction that is only re-planned while it waits for you (after three minutes
  unplaced, at the current price) is not read out again. The terminal and the panel
  always show the current one.

### How it behaves

- **Only your status moves the books.** Nothing is assumed bought or sold until you
  report it. An instruction that is not in your status yet is repeated unchanged — same
  order id, same price — for 3 minutes, then re-planned at current prices. If you place
  a withdrawn instruction after all and report it, it is taken back on the book.
- **Buys**: bid 1 gp over the instant-sell, for as many units as an hour of your share
  of the item's volume can fill (`--order-hours`). Aborted after 30 minutes without a
  fill, 60 once your coins, offers and stock are worth 100k (`--buy-timeout` fixes one
  timeout for any stack). An abort frees the slot and the coins for a replacement in the
  same batch. A bid is not aborted merely because the market has moved against it;
  `--margin-cancel` brings that back.
- **Sells**: listed 1 gp under the instant-buy as soon as the buy finishes (or once a
  still-filling buy has delivered 20% of its value), then left alone. After 4 hours
  without a sale it is relisted at the market (`--sell-reprice`), after 8 hours at the
  instant-sell price (`--sell-dump`) — a loss is taken and reported as one rather than
  left to sit. One buy and one sell offer per item at a time.
- **Quantities that are quick to enter and easy to hear**: a buy is rounded to one digit
  that is not zero (400, 3,000), or to two (250, 14,000) where one would change the
  order by more than 15% — and never to more than your coins or the buy limit allow. A
  sell of every unit you hold of the item says `all`: it is your whole stack, so there
  is no number to carry over (this needs your inventory in the status, which the plugin
  writes). Place a unit more or fewer than asked and the engine follows the offer as
  you placed it. `--no-round-quantities` gives the exact sizes back; rounding costs a
  few percent of the profit (next section).
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

### How patient it is, and how many offers that takes

Every offer you place or abort is work, so the timeouts and the order size were chosen
to ask for as few as the profit allows. Four weeks of history (Oct 14 – Nov 10), 8
slots, N = 30 (N is explained in the next section); "offers" are offers placed or
aborted, the things you do by hand:

| Start | Earlier settings | Settings now | Profit now, as % of earlier (lowest–highest week) |
|---|---|---|---|
| 10k | +652k a week, 821 offers a day | +782k, 386 | 119% (96–134%) |
| 30k | +741k, 822 | +973k, 367 | 130% (114–145%) |
| 100k | +832k, 819 | +1.02m, 328 | 122% (95–155%) |
| 300k | +913k, 813 | +1.28m, 296 | 139% (117–182%) |
| 1m | +1.04m, 807 | +1.30m, 272 | 126% (113–143%) |

Over the 20 week-and-size pairs: 26% more profit (95% interval 17–34%, 18 of 20 pairs
up) from 40% of the offers, so each offer earns about three times what it did (2.6 to
3.8 times). What changed, each step measured on the same 20 pairs against the one
above it:

| Change | Profit | Offers |
|---|---|---|
| **Stock is left on sale**: 4 hours before it is relisted at the market, 8 before it goes at the instant-sell price (were 30 and 90 minutes). **A bid is no longer aborted** because the market moved. **Orders are twice as big.** | +38% (30–46%; up in 20 of 20 pairs) | −51% |
| **The stack-size rule**: a buy that is not filling gets 60 minutes instead of 30 once you have 100k | −4% (−9 to +1) | −18% |
| **Buy quantities rounded** | −5% (−10 to 0) | +1% |

In the first row it is the patient selling that earns: on its own +32% (24–41%). Not
aborting bids and doubling the orders add nothing measurable to that (+3%, −4 to +11);
they take the offers from about 590 a day to about 400.

**The last two rows are a trade, not a gain.** Together they cost 9% of the profit
(4–14%) and buy 18% fewer offers and quantities that are quick to enter.
`--buy-timeout 30 --no-round-quantities` takes the profit instead.

**Patience by stack size holds up less well than one week suggested.** Giving every
buy 60 minutes made 6% less than 30 minutes (0–11%) for 21% fewer offers: 18% less
started with 10k, between 6% less and 2% more started with 30k to 1m. That is why the
engine keeps a small stack at 30 minutes — but the four weeks cannot tell the rule
from a flat 60 minutes (which made 99% of the rule's profit, 94–103%), and they do not
say where the threshold belongs. 100k is a cautious choice, not a measured optimum.

**Fewer offers still**, against the settings above (starting with 10k / 100k / 1m):

| | Profit | Offers a day |
|---|---|---|
| `--order-hours 2`: orders twice as big again | +8% (−2 to +19) | 336 / 285 / 227 |
| `--buy-timeout 120` | −7% (−15 to +2) | 279 / 260 / 221 |
| acting every 30 minutes instead of every 5 (`--look-every 30` in a backtest) | 63% / 78% / 91% of it | 256 / 226 / 197 |
| acting once an hour | 30% / 46% / 69% of it | 163 / 157 / 143 |
| at the GE 19:00–23:00 only (`--play 19-23`) | 8% / – / 31% of it | 95 / – / 54 |

Bigger orders look like a free gain, but they are the one setting that can turn a
profit into a loss (next section): raise `--order-hours` only once `--report` has shown
the share you really get. Acting less often costs a small stack most, because it grows
by turning over; offers keep filling while you are away, which is what a big stack
lives on.

An earlier version of this page recommended `--buy-timeout 120 --sell-reprice 120
--sell-dump 240` from about 100k, on the strength of one week. Over four weeks it made
97% of the earlier settings' profit (88–108%; 87% started with 10k, 112% with 300k)
for 44% of the offers. It is withdrawn: the settings above earn more at every size.

What this does not show. Four weeks is little, and differences of less than about 10%
between settings are not real: the same week started 5–20 minutes later gave 623k–735k
where the table's run gave 680k. When each week ended, a fifth to a half of the stack
was in stock waiting to sell, so a market-wide drop would hurt. One item (Ascension
shards, in the week of Oct 21) was up to a third of that week's profit. And all of it
rests on the paper fill model.

To measure other settings on your own stack:

```sh
ge-analyser execute --backtest 168 --budget 1m --buy-timeout 30 --look-every 30
```

The table it prints has `Offers/day` and `gp/offer` columns.

### The share of volume it assumes you get

A buy is sized to what an hour of *your share* of the item's volume can fill, so the
engine has to assume a share before it has seen you trade. The paper model gives an
order 1/N of the volume that crosses its price; the engine assumes N = 30
(`--fill-share`), the middle of the 10–100 the backtests cover, and then corrects
itself: once 10 of your buy orders have finished (3 for an item), order sizes are
scaled by what really filled against what the model gives for the same orders over the
same 5-minute windows — down to a tenth, up to double. `execute --report` prints the
share your fills imply.

Why the middle: being wrong costs in both directions, and assuming too big a share
costs most. Profit as a percentage of what the earlier settings made *knowing* N (four
weeks, the assumption never corrected):

| You really get | Start 10k, it assumes 1/30 | Start 1m, it assumes 1/30 |
|---|---|---|
| 1/10 | 93% | 115% |
| 1/30 (it is right) | 119% | 126% |
| 1/100 | 141% | 120% |

Assuming 1/100 when you get 1/10 made 57–68%: orders too small to be worth their
slots. Assuming 1/10 when you get 1/100, with 1m to spend, made 46% and lost money in
one week of the four: orders far bigger than the market takes off your hands again
(measured before the stack-size rule and the rounding were added). Twice the order
size (`--order-hours 2`) doubles that mistake too.

None of these runs had the correction running: they show what a wrong assumption costs
if it is never put right. Live, it is put right from buy orders that have finished,
and an order that is too big finishes late — so an assumption that is too bold is the
slowest to be corrected. With a large stack, read `--report` after your first ten buys
before trusting the sizes.

### What re-evaluating the model found

```sh
ge-analyser execute --backtest 168 --compare --budget 1m     # last week for your stack: N = 10, 30, 100, and a wrong assumption
ge-analyser execute --backtest 168 --budget 1m --play 19-23  # at the GE 19:00-23:00 only; offers fill in between
```

`--backtest` replays history through the **same engine** with a paper broker that obeys
GE mechanics: coins reserved, slots shared, Collect once per cycle, 26 stacks. The
engine counts its own coins as in the live loop and the broker checks the count: in the
713 week-long runs behind this page it was never off by a coin, and the engine never
asked for an offer that could not be placed. What it showed:

1. **One freak print carried the old results.** During the first live paper run a junk
   item (Steel knife(p+)) printed ~5,500 units at 49,999 gp in 20 minutes — almost
   certainly coins being moved between two accounts. The paper agent "bought at 501 and
   sold at 49,998"; that single trade was 7.8m of its 8.9m profit (87%). Items without
   history are now never traded, and asks are capped at twice the usual margin.
2. **Bonds were the "best flip" for a big stack.** With 10m or more to spend, up to 91%
   of the backtest profit came from Old school bonds, which cannot be flipped at all.
   They are excluded.
3. **The result hangs on one unknown: the share of volume you get.** The paper model
   gives an order 1/N of the volume that crosses its price. Profit a week with the
   settings now, mean of four weeks (lowest–highest week), all items, 8 slots, at the
   GE around the clock, the engine knowing N:

   | Start | N = 10 | N = 30 | N = 100 |
   |---|---|---|---|
   | 10k | +1.88m (1.43m–2.93m) | +782k (578k–1.13m) | +304k (231k–487k) |
   | 1m | +3.29m (2.26m–4.51m) | +1.30m (1.16m–1.57m) | +539k (323k–675k) |
   | 10m | +3.30m (2.89m–3.84m) | +1.44m (1.20m–1.60m) | +541k (373k–724k) |
   | 100m | | +1.44m (1.20m–1.60m) | |
   | 10k, free-to-play (3 slots) | +128k (89k–190k) | +85k (69k–99k) | +48k (32k–63k) |

   Against the earlier settings in the same weeks, started with 10k and with 1m: 112%
   and 132% at N = 10, 119% and 126% at N = 30, 145% and 172% at N = 100; free-to-play
   106% (84–120%) at N = 30. At worst one item was a third of a week's profit at
   N = 30 (Ascension shards, week of Oct 21) and 40% at N = 10; with 1m or more at
   N = 100 Chaos runes were up to half, and free-to-play Gold bars up to 46%.
4. **Profit is capped by market flow, not by your stack.** Orders are sized to what an
   item really trades, so from about 1m up a bigger stack earns the same gp: 100m makes
   exactly what 10m makes. Admitting thinner, pricier items (`--min-volume 10`) did not
   help in a 24-hour test. This is a strategy for cheap, liquid items.
5. **It is still a lot of offers for the gp.** 250–400 offers placed or aborted a day
   with 8 slots, about 220 with 3: 100–1,700 gp an offer with 8 slots (35–80 with 3), or
   2k–20k gp an hour around the clock (300–800 with 3), depending on N and stack. Four
   hours a day (`--play 19-23`, N = 30) keeps 31% of the weekly profit for a 1m stack,
   because offers fill while you are away (+391k a week from 54 offers a day, 14k gp per
   hour at the GE); a 10k stack keeps 8% (+62k), having lost its compounding.
6. **Real mechanics cost 12–26% against the old simulator** on the same data and N.
   That was measured with the earlier settings, whose 30-minute patience the old
   simulator shares (+1.50m vs +1.71m from 10k, +2.24m vs +3.01m from 1m, week of Nov 3;
   nothing with 3 free-to-play slots). The 26-stack limit never binds: at most 8 stacks
   were ever held.

What no backtest can know is N. The loop measures it from your own fills and
`execute --report` prints it: see "The share of volume it assumes you get" above.

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
| Each print fills at most 1/N of that window's one-sided volume. | `--fill-share N` (default 10 here; `execute` assumes 30) |
| Fills happen at our offer price, never the better print. | — |
| Only enter flips with ≥ 2 gp and ≥ 2% margin after tax; 1-gp spreads on bot items aren't real money for a solo flipper. | `--min-margin`, `--min-roi` |

Its absolute returns are not a forecast: they scale with the assumed fill share exactly
as the execution-loop table shows, and before the guards existed a single freak print
produced 87% of one paper agent's profit.

State lives in `~/.osrs-ge-analyser/sim.json` (override with `GE_ANALYSER_SIM`).

## In-game panel (RuneLite plugin)

`runelite-plugin/` is a small RuneLite plugin. Its side panel shows the execution loop's
instructions while `ge-analyser execute` is answering, and otherwise the current
suggestions (item, buy→sell, margin, qty, remaining limit, fill probability) from
`ge-analyser advise`. It also keeps the loop's `status.json` and logs your real GE
offers for `sync`. It reads game state and draws; it never places offers. See
`runelite-plugin/README.md`.

```sh
ge-analyser advise --budget 10m --top 15      # refreshes ~/.osrs-ge-analyser/advice.json every 60 s
cd runelite-plugin && ./gradlew run           # RuneLite with the plugin built in
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
