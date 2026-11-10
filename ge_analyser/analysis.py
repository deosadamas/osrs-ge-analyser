"""Pure analysis functions: GE tax, flip margins, ranking, trend signals."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable

from .api import ItemInfo, LatestPrice, WindowPrice

# Grand Exchange tax (since 29 May 2025): 2% of sell price, rounded down, capped at
# 5M per item sold. Rounding down means sales under 50 gp pay nothing.
# Exempt list per https://oldschool.runescape.wiki/w/Grand_Exchange#Exempt_from_tax
TAX_RATE = 0.02
TAX_CAP = 5_000_000
TAX_EXEMPT_NAMES = frozenset(
    {
        "Old school bond",
        # Energy potions
        "Energy potion(1)",
        "Energy potion(2)",
        "Energy potion(3)",
        "Energy potion(4)",
        # Low level combat consumables
        "Bronze arrow",
        "Bronze dart",
        "Iron arrow",
        "Iron dart",
        "Mind rune",
        "Steel arrow",
        "Steel dart",
        # Low level food
        "Bass",
        "Bread",
        "Cake",
        "Cooked chicken",
        "Cooked meat",
        "Herring",
        "Lobster",
        "Mackerel",
        "Meat pie",
        "Pike",
        "Salmon",
        "Shrimps",
        "Tuna",
        # Teleport items
        "Ardougne teleport (tablet)",
        "Camelot teleport (tablet)",
        "Civitas illa fortis teleport",
        "Falador teleport (tablet)",
        "Games necklace(8)",
        "Kourend castle teleport (tablet)",
        "Lumbridge teleport (tablet)",
        "Ring of dueling(8)",
        "Teleport to house (tablet)",
        "Varrock teleport (tablet)",
        # Tools
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
    if item_name in TAX_EXEMPT_NAMES:
        return 0
    return min(math.floor(sell_price * TAX_RATE), TAX_CAP)


@dataclass(frozen=True)
class ItemStats:
    """Per-item features from recent 5m history (see features.compute_stats)."""

    windows: int  # 5m windows with both a buy and a sell print
    fill_buy: float  # P(a bid at the window's instant-sell is crossed within 30 min)
    fill_sell: float  # P(an ask at the window's instant-buy is crossed within 30 min)
    median_margin: int  # median after-tax spread per unit across windows
    volatility_pct: float  # std-dev of 5m mid-price % changes
    typical_volume: int = 0  # median one-sided units per hour (0 = unknown)

    @property
    def p_fill(self) -> float:
        return self.fill_buy * self.fill_sell


# Used when history is enabled but an item has too few two-sided 5m windows to measure:
# such items are illiquid, so assume both legs rarely fill and trust today's margin.
UNKNOWN_STATS = ItemStats(windows=0, fill_buy=0.1, fill_sell=0.1, median_margin=0, volatility_pct=0.0, typical_volume=0)

@dataclass(frozen=True)
class Flip:
    item: ItemInfo
    buy: int  # price to place buy offer at (current instant-sell price)
    sell: int  # price to place sell offer at (current instant-buy price)
    tax: int
    margin: int  # per-unit profit after tax
    roi: float  # margin / buy
    hourly_volume: int  # conservative: min(buys, sells) in the last hour
    liquidity: int  # units/h planned on: min(hourly_volume, typical for this time of week)
    limit_left: int  # remaining 4h buy limit (item.limit when nothing recorded; 0 = unknown)
    quantity: int  # units you can realistically flip given limit, budget, volume
    profit: int  # margin * quantity
    price_age_s: int  # seconds since the older of the two latest prices
    score: float
    stats: ItemStats | None = None  # per-item history features when available
    expected_profit: int = 0  # profit x fill probability on the typical margin (= profit without stats)


def remaining_limit(item: ItemInfo, used: int) -> int:
    """Units still purchasable this 4h window. Unknown limit (0) stays 0."""
    if item.limit <= 0:
        return 0
    return max(item.limit - used, 0)


def flip_quantity(item: ItemInfo, buy: int, hourly_volume: int, budget: int, used: int = 0) -> int:
    """Units for one flip cycle: bounded by remaining buy limit, budget, and recent volume."""
    by_budget = budget // buy if buy > 0 else 0
    by_limit = remaining_limit(item, used) if item.limit > 0 else by_budget
    # Assume you can capture at most ~1/4 of an hour's two-sided volume.
    by_volume = max(hourly_volume // 4, 1)
    return max(min(by_budget, by_limit, by_volume), 0)


@dataclass(frozen=True)
class ScoreParams:
    """Tunable constants in the score (see `ge-analyser tune`)."""

    liq_weight: float = 10.0  # liquidity term is 0.5 + ln(1 + units/h) / liq_weight
    vol_penalty: float = 1.0  # risk term is 1 / (1 + vol_penalty * Vol% / ROI%)


DEFAULT_PARAMS = ScoreParams()


def evaluate_flip(
    item: ItemInfo,
    latest: LatestPrice,
    hour: WindowPrice | None,
    budget: int,
    now: int,
    used: int = 0,
    stats: ItemStats | None = None,
    params: ScoreParams = DEFAULT_PARAMS,
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
    # Liquidity we plan on: never more than the market is doing right now, and never
    # more than this item typically does at this time of week.
    liq = min(hourly_volume, stats.typical_volume) if stats and stats.typical_volume > 0 else hourly_volume
    qty = flip_quantity(item, buy, liq, budget, used)
    if qty == 0:
        return None
    profit = margin * qty
    roi = margin / buy
    age = now - min(latest.high_time, latest.low_time)
    staleness = 1.0 / (1.0 + age / 3600)
    liquidity = math.log1p(liq)
    liq_term = 0.5 + liquidity / params.liq_weight
    if stats is None:
        # No history: profit per cycle, discounted by staleness, rewarding liquidity.
        expected = profit
        score = profit * staleness * liq_term
    else:
        # With history: expected profit = typical margin (not today's spike) x units x
        # probability both legs fill, then penalised when 5m volatility rivals the margin.
        typical = margin if stats.windows == 0 else stats.median_margin
        edge = max(min(margin, typical), 1)
        expected = int(edge * qty * stats.p_fill)
        risk = 1.0 / (1.0 + params.vol_penalty * stats.volatility_pct / max(roi * 100, 0.01))
        score = expected * staleness * liq_term * risk
    return Flip(
        item=item,
        buy=buy,
        sell=sell,
        tax=tax,
        margin=margin,
        roi=roi,
        hourly_volume=hourly_volume,
        liquidity=liq,
        limit_left=remaining_limit(item, used),
        quantity=qty,
        profit=profit,
        price_age_s=age,
        score=score,
        stats=stats,
        expected_profit=expected,
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
    used: dict[int, int] | None = None,
    stats: dict[int, ItemStats] | None = None,
    params: ScoreParams = DEFAULT_PARAMS,
) -> list[Flip]:
    used = used or {}
    out: list[Flip] = []
    for item_id, price in latest.items():
        item = items.get(item_id)
        if item is None:
            continue
        if members is not None and item.members != members:
            continue
        item_stats = None if stats is None else stats.get(item_id, UNKNOWN_STATS)
        flip = evaluate_flip(item, price, hour.get(item_id), budget, now, used.get(item_id, 0), item_stats, params)
        if flip is None:
            continue
        if flip.liquidity < min_volume or flip.margin < min_margin:
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
