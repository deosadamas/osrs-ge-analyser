"""Analysis of real GE trades (from RuneLite) to calibrate the simulator.

Pure functions; the CLI supplies trades and wiki history.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

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
