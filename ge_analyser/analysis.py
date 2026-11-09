"""Pure analysis functions: GE tax, flip margins, ranking, trend signals."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable

from .api import ItemInfo, LatestPrice, WindowPrice

# Grand Exchange tax: 2% of sell price, floored, capped at 5M per item sold.
# Sales under 50 gp are untaxed. A fixed set of items is exempt.
TAX_RATE = 0.02
TAX_CAP = 5_000_000
TAX_FREE_BELOW = 50
TAX_EXEMPT_NAMES = frozenset(
    {
        "Old school bond",
        "Chisel",
        "Gardening trowel",
        "Glassblowing pipe",
        "Hammer",
        "Needle",
        "Pestle and mortar",
        "Rake",
        "Saw",
        "Secateurs",
        "Seed dibber",
        "Shears",
        "Spade",
        "Watering can",
    }
)


def ge_tax(sell_price: int, item_name: str = "") -> int:
    if item_name in TAX_EXEMPT_NAMES or sell_price < TAX_FREE_BELOW:
        return 0
    return min(math.floor(sell_price * TAX_RATE), TAX_CAP)


@dataclass(frozen=True)
class Flip:
    item: ItemInfo
    buy: int  # price to place buy offer at (current instant-sell price)
    sell: int  # price to place sell offer at (current instant-buy price)
    tax: int
    margin: int  # per-unit profit after tax
    roi: float  # margin / buy
    hourly_volume: int  # conservative: min(buys, sells) in last hour
    quantity: int  # units you can realistically flip given limit, budget, volume
    profit: int  # margin * quantity
    price_age_s: int  # seconds since the older of the two latest prices
    score: float


def flip_quantity(item: ItemInfo, buy: int, hourly_volume: int, budget: int) -> int:
    """Units for one flip cycle: bounded by buy limit, budget, and recent volume."""
    by_budget = budget // buy if buy > 0 else 0
    by_limit = item.limit if item.limit > 0 else by_budget
    # Assume you can capture at most ~1/4 of an hour's two-sided volume.
    by_volume = max(hourly_volume // 4, 1)
    return max(min(by_budget, by_limit, by_volume), 0)


def evaluate_flip(
    item: ItemInfo,
    latest: LatestPrice,
    hour: WindowPrice | None,
    budget: int,
    now: int,
) -> Flip | None:
    if latest.high is None or latest.low is None or latest.high_time is None or latest.low_time is None:
        return None
    buy, sell = latest.low, latest.high
    if buy <= 0 or sell <= buy:
        return None
    tax = ge_tax(sell, item.name)
    margin = sell - tax - buy
    if margin <= 0:
        return None
    hourly_volume = min(hour.high_volume, hour.low_volume) if hour else 0
    qty = flip_quantity(item, buy, hourly_volume, budget)
    if qty == 0:
        return None
    profit = margin * qty
    roi = margin / buy
    age = now - min(latest.high_time, latest.low_time)
    # Score: profit per cycle, discounted by staleness and rewarding liquidity.
    staleness = 1.0 / (1.0 + age / 3600)
    liquidity = math.log1p(hourly_volume)
    score = profit * staleness * (0.5 + liquidity / 10)
    return Flip(
        item=item,
        buy=buy,
        sell=sell,
        tax=tax,
        margin=margin,
        roi=roi,
        hourly_volume=hourly_volume,
        quantity=qty,
        profit=profit,
        price_age_s=age,
        score=score,
    )


def find_flips(
    items: dict[int, ItemInfo],
    latest: dict[int, LatestPrice],
    hour: dict[int, WindowPrice],
    budget: int,
    now: int,
    min_volume: int = 50,
    min_margin: int = 1,
    min_roi: float = 0.0,
    max_age_s: int = 3600,
    members: bool | None = None,
) -> list[Flip]:
    out: list[Flip] = []
    for item_id, price in latest.items():
        item = items.get(item_id)
        if item is None:
            continue
        if members is not None and item.members != members:
            continue
        flip = evaluate_flip(item, price, hour.get(item_id), budget, now)
        if flip is None:
            continue
        if flip.hourly_volume < min_volume or flip.margin < min_margin:
            continue
        if flip.roi < min_roi or flip.price_age_s > max_age_s:
            continue
        out.append(flip)
    out.sort(key=lambda f: f.score, reverse=True)
    return out


@dataclass(frozen=True)
class Trend:
    points: int
    mean: float
    stdev: float
    latest: float
    z_score: float  # (latest - mean) / stdev; negative = below average = buy signal
    change_pct: float  # first vs last point in the series
    total_volume: int


def trend_from_timeseries(series: Iterable[dict[str, Any]]) -> Trend | None:
    """Mid-price trend over a timeseries response. Points lacking both prices are skipped."""
    mids: list[float] = []
    volume = 0
    for p in series:
        hi, lo = p.get("avgHighPrice"), p.get("avgLowPrice")
        if hi is None and lo is None:
            continue
        mid = (hi + lo) / 2 if hi is not None and lo is not None else float(hi if hi is not None else lo)
        mids.append(mid)
        volume += (p.get("highPriceVolume") or 0) + (p.get("lowPriceVolume") or 0)
    if len(mids) < 2:
        return None
    mean = statistics.fmean(mids)
    stdev = statistics.pstdev(mids)
    latest = mids[-1]
    z = (latest - mean) / stdev if stdev > 0 else 0.0
    change = (mids[-1] - mids[0]) / mids[0] * 100 if mids[0] else 0.0
    return Trend(
        points=len(mids),
        mean=mean,
        stdev=stdev,
        latest=latest,
        z_score=z,
        change_pct=change,
        total_volume=volume,
    )


def parse_gp(text: str) -> int:
    """Parse '10m', '2.5k', '1b', '12345' into gp."""
    s = text.strip().lower().replace(",", "").replace("gp", "")
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("b"):
        mult, s = 1_000_000_000, s[:-1]
    return int(float(s) * mult)


def fmt_gp(n: int | float) -> str:
    n = int(n)
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}b"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}m"
    if abs(n) >= 10_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,}"
