# osrs-ge-analyser

Grand Exchange analyser for Old School RuneScape. Pulls live prices from the
[OSRS Wiki real-time prices API](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices)
and tells you what to buy and sell.

Three signals:

| Command | What it does |
|---------|--------------|
| `flips` | Ranks items by instant-sell → instant-buy margin **after the 2% GE tax**, bounded by buy limit, your budget and last-hour volume. |
| `item`  | Full detail for one item: current spread, hourly volume, 24h and ~3-month trend with a buy/sell/neutral verdict. |
| `dips`  | Scans the most liquid items and flags ones trading ≥ N standard deviations below their 24h mean (mean-reversion buys). |

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
- **Margin**: `Sell − tax − Buy`. Tax is 2% of the sell price, floored, capped at 5m, zero under 50 gp, zero for bonds/tools.
- **Qty**: `min(buy limit, budget / Buy, Vol/h ÷ 4)`; assumes you capture at most a quarter of an hour's volume.
- **Profit**: `Margin × Qty` for one 4-hour buy-limit cycle.
- **Age**: time since the older of the two prices; stale prices are scored down.

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
