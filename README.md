# osrs-ge-analyser

Grand Exchange analyser for Old School RuneScape. Pulls live prices from the
[OSRS Wiki real-time prices API](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices)
and tells you what to buy and sell.

Four commands:

| Command | What it does |
|---------|--------------|
| `flips` | Ranks items by instant-sell → instant-buy margin **after the 2% GE tax**, bounded by buy limit, your budget and last-hour volume. |
| `item`  | Full detail for one item: current spread, hourly volume, 24h and ~3-month trend with a buy/sell/neutral verdict. |
| `dips`  | Scans the most liquid items and flags ones trading ≥ N standard deviations below their 24h mean (mean-reversion buys). |
| `bought` | Records what you've bought so remaining 4-hour buy limits are honoured by `flips` and `item`. |

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

## How ranking works

`score = profit × 1/(1 + age_hours) × (0.5 + ln(1 + volume)/10)`

Profit per cycle, discounted for stale prices, boosted for liquidity. The constants are
in `ge_analyser/analysis.py::evaluate_flip`.

## Caching

Responses are cached in `.cache/` (60 s for prices, 5 min for timeseries, 24 h for item
mapping). `--no-cache` bypasses it. Be polite: the wiki asks for a descriptive
User-Agent, set in `ge_analyser/api.py`.

## Development

```sh
pip install -e . pytest
pytest
```

## Disclaimer

Prices move; margins shown are a snapshot, not a guarantee. Other flippers see the
same data. Nothing here interacts with the game client.
