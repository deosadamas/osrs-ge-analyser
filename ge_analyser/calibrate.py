"""Analysis of real GE trades (from RuneLite) to calibrate the simulator.

Pure functions; the CLI supplies trades and wiki history.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from .analysis import ge_tax
from .runelite import RealTrade


@dataclass
class ItemPnl:
    name: str
    bought: int = 0
    sold: int = 0
    spent: int = 0
    received: int = 0  # after tax
    tax: int = 0
    realised: int = 0  # FIFO-matched profit on units both bought and sold
    unmatched_qty: int = 0  # bought but not yet sold


def fifo_pnl(trades: list[RealTrade]) -> dict[int, ItemPnl]:
    """Match sells against earlier buys per item, FIFO. Sells with no prior buy are ignored
    for realised P&L (we don't know their cost) but still counted in received/tax."""
    out: dict[int, ItemPnl] = {}
    lots: dict[int, deque[list[int]]] = {}  # item -> deque of [qty, unit_cost]
    for t in sorted(trades, key=lambda x: x.ts):
        p = out.setdefault(t.item_id, ItemPnl(t.name))
        if t.buy:
            p.bought += t.qty
            p.spent += t.qty * t.price
            lots.setdefault(t.item_id, deque()).append([t.qty, t.price])
            continue
        tax_unit = ge_tax(t.price, t.name)
        p.sold += t.qty
        p.tax += tax_unit * t.qty
        p.received += (t.price - tax_unit) * t.qty
        remaining = t.qty
        q = lots.get(t.item_id)
        while remaining > 0 and q:
            lot = q[0]
            take = min(lot[0], remaining)
            p.realised += take * (t.price - tax_unit - lot[1])
            lot[0] -= take
            remaining -= take
            if lot[0] == 0:
                q.popleft()
    for item_id, q in lots.items():
        out[item_id].unmatched_qty = sum(lot[0] for lot in q)
    return out


@dataclass
class FillStats:
    completed_buys: int = 0
    completed_sells: int = 0
    cancelled_buys: int = 0
    cancelled_sells: int = 0
    buy_fill_seconds: list[float] = field(default_factory=list)
    sell_fill_seconds: list[float] = field(default_factory=list)
    cancelled_fill_ratio: list[float] = field(default_factory=list)  # qty / offered on cancelled offers

    def median(self, values: list[float]) -> float | None:
        return statistics.median(values) if values else None


def fill_stats(trades: list[RealTrade]) -> FillStats:
    s = FillStats()
    for t in trades:
        if t.state in ("BOUGHT", "SOLD"):
            if t.buy:
                s.completed_buys += 1
                if t.fill_seconds is not None:
                    s.buy_fill_seconds.append(t.fill_seconds)
            else:
                s.completed_sells += 1
                if t.fill_seconds is not None:
                    s.sell_fill_seconds.append(t.fill_seconds)
        elif t.state in ("CANCELLED_BUY", "CANCELLED_SELL"):
            if t.buy:
                s.cancelled_buys += 1
            else:
                s.cancelled_sells += 1
            if t.offered_qty > 0:
                s.cancelled_fill_ratio.append(t.qty / t.offered_qty)
    return s


@dataclass
class Calibration:
    """How real fills relate to the wiki's market print in the same window.

    For a BUY at price p with market instant-sell L: p < L means the market had to come
    down to us (a strict-model fill); p >= L means we were at or above the print (a
    loose-model fill). Sells mirror this against instant-buy H.
    """

    classified: int = 0
    unknown: int = 0  # no wiki print in that window
    strict_fills: int = 0
    loose_fills: int = 0
    edge_pct: list[float] = field(default_factory=list)  # (market - ours)/market for buys, (ours - market)/market for sells

    @property
    def strict_share(self) -> float | None:
        return self.strict_fills / self.classified if self.classified else None

    def suggestion(self) -> str:
        share = self.strict_share
        if share is None:
            return "not enough classified fills to recommend a fill model"
        if share >= 0.6:
            return "most fills needed the market to cross your price -> keep the default strict model"
        if share <= 0.3:
            return "most fills happened at/above the market print -> try `simulate --loose` (you are getting queue priority)"
        return "mixed -> strict model is a fair floor; reality sits between strict and --loose"


def lookback_for_age(age_s: int) -> str:
    """Smallest wiki lookback whose history covers a trade this old (finer step = better match)."""
    if age_s < 23 * 3600:
        return "24h"
    if age_s < 6.5 * 86400:
        return "7d"
    if age_s < 29 * 86400:
        return "30d"
    if age_s < 180 * 86400:
        return "6m"
    return "1y"


def bucket_at(series: list[dict[str, Any]], ts: int) -> dict[str, Any] | None:
    """Latest bucket whose timestamp is <= ts."""
    best = None
    for b in series:
        if b["timestamp"] <= ts and (best is None or b["timestamp"] > best["timestamp"]):
            best = b
    return best


def calibrate(trades: list[RealTrade], history: dict[tuple[int, str], list[dict[str, Any]]], now: int) -> Calibration:
    """history: (item_id, step) -> wiki timeseries. Only completed BOUGHT/SOLD trades are used."""
    c = Calibration()
    for t in trades:
        if t.state not in ("BOUGHT", "SOLD"):
            continue
        series = history.get((t.item_id, lookback_for_age(now - t.ts)))
        b = bucket_at(series, t.ts) if series else None
        market = (b or {}).get("avgLowPrice" if t.buy else "avgHighPrice")
        if not market:
            c.unknown += 1
            continue
        c.classified += 1
        if t.buy:
            edge = (market - t.price) / market
            strict = t.price < market
        else:
            edge = (t.price - market) / market
            strict = t.price > market
        c.edge_pct.append(edge * 100)
        if strict:
            c.strict_fills += 1
        else:
            c.loose_fills += 1
    return c


# ---- execution loop: real fills against the paper fill model ---------------


def model_fill(rows: Iterable[tuple], side: str, price: int, qty: int, start: int, end: int, fill_share: int = 10, strict: bool = True) -> float:
    """Units the paper fill model (see broker.PaperBroker) gives an order that was live
    from `start` to `end`. rows: 5m windows (ts, high, hvol, low, lvol). A window counts
    in proportion to how much of it the order was live."""
    total = 0.0
    for ts, high, hvol, low, lvol in rows:
        overlap = min(end, ts + 300) - max(start, ts)
        if overlap <= 0:
            continue
        cap = 0
        if side == "buy":
            if low is not None and (low < price if strict else low <= price):
                cap = max(lvol // fill_share, 1)
        else:
            if high is not None and (high > price if strict else high >= price):
                cap = max(hvol // fill_share, 1)
            if low is not None and low >= price:
                cap = max(cap, lvol // fill_share, 1)
        total += cap * overlap / 300
    return min(total, float(qty))


@dataclass
class FillReality:
    """Value-weighted units really filled / units the paper model would have filled."""

    ratio: float | None  # None until at least one order has a model figure > 0
    orders: int
    per_item: dict[int, tuple[float, int]]  # item -> (ratio, orders)


FILL_RATIO_CAP = 2.0  # real fills are never read as more than this many times the model's


def fill_reality(orders: Iterable[Any], side: str | None = None, cap: float = FILL_RATIO_CAP) -> FillReality:
    """orders: closed execution orders carrying `filled`, `price`, `model_fill`. Orders
    the model gives nothing but that did fill count as `cap` (the model was too strict)."""
    num = den = 0.0
    n = 0
    acc: dict[int, list[float]] = {}
    for o in orders:
        if o.model_fill is None or (side and o.side != side):
            continue
        real, model = o.filled * o.price, o.model_fill * o.price
        n += 1
        num += real
        den += model
        a = acc.setdefault(o.item_id, [0.0, 0.0, 0])
        a[0] += real
        a[1] += model
        a[2] += 1

    def ratio(real: float, model: float) -> float | None:
        if model > 0:
            return min(real / model, cap)
        return cap if real > 0 else None

    per = {}
    for item_id, (real, model, count) in acc.items():
        r = ratio(real, model)
        if r is not None:
            per[item_id] = (r, int(count))
    return FillReality(ratio(num, den), n, per)


def sizing_scales(reality: FillReality, min_orders: int = 10, min_item_orders: int = 3, floor: float = 0.1) -> tuple[float, dict[int, float]]:
    """How much to scale order sizes by: (default, per item). Nothing changes until
    there are enough finished orders to say something."""
    default = 1.0
    if reality.ratio is not None and reality.orders >= min_orders:
        default = max(reality.ratio, floor)
    per = {i: max(r, floor) for i, (r, n) in reality.per_item.items() if n >= min_item_orders}
    return default, per
