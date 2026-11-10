"""Per-item features from the local 5m history store.

All three features answer questions the live spread can't:
- fill_buy / fill_sell: did a bid (ask) placed at a window's instant-sell (instant-buy)
  get crossed by a later window within 30 min? This is the same strict-crossing rule
  the simulator uses, so the rates are directly a fill-probability estimate for it.
- median_margin: the typical after-tax spread; today's margin above it is a spike.
- volatility_pct: how much the mid moves per 5 min; margins smaller than this are noise.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from .analysis import ItemStats, ge_tax
from .history import HistoryStore, Row

CROSS_WINDOW_S = 30 * 60
MIN_WINDOWS = 12  # need at least an hour of two-sided prints to say anything


def item_stats(rows: list[Row], name: str) -> ItemStats | None:
    quoted = [r for r in rows if r.high is not None and r.low is not None]
    if len(quoted) < MIN_WINDOWS:
        return None

    last_ts = quoted[-1].ts

    def crossed(getter, cmp) -> float:
        hits = n = 0
        for i, r in enumerate(quoted):
            if last_ts - r.ts < CROSS_WINDOW_S:
                break  # not enough future to judge this window
            n += 1
            ref = getter(r)
            k = i + 1
            while k < len(quoted) and quoted[k].ts - r.ts <= CROSS_WINDOW_S:
                if cmp(getter(quoted[k]), ref):
                    hits += 1
                    break
                k += 1
        return hits / n if n else 0.0

    fill_buy = crossed(lambda r: r.low, lambda later, ref: later < ref)
    fill_sell = crossed(lambda r: r.high, lambda later, ref: later > ref)
    margins = [r.high - ge_tax(r.high, name) - r.low for r in quoted]
    mids = [(r.high + r.low) / 2 for r in quoted]
    changes = [(b - a) / a * 100 for a, b in zip(mids, mids[1:]) if a > 0]
    return ItemStats(
        windows=len(quoted),
        fill_buy=fill_buy,
        fill_sell=fill_sell,
        median_margin=int(statistics.median(margins)),
        volatility_pct=statistics.pstdev(changes) if len(changes) > 1 else 0.0,
    )


def compute_stats(store: HistoryStore, names: dict[int, str], since: int, until: int | None = None) -> dict[int, ItemStats]:
    out: dict[int, ItemStats] = {}
    for item_id, rows in store.series(since, until).items():
        s = item_stats(rows, names.get(item_id, ""))
        if s is not None:
            out[item_id] = s
    return out


def _encode(stats: dict[int, ItemStats]) -> dict:
    return {str(i): [s.windows, s.fill_buy, s.fill_sell, s.median_margin, s.volatility_pct] for i, s in stats.items()}


def _decode(raw: dict) -> dict[int, ItemStats]:
    return {int(i): ItemStats(*v) for i, v in raw.items()}


def cached_stats(
    store: HistoryStore,
    names: dict[int, str],
    days: float,
    now: int | None = None,
    max_age: int = 1800,
    cache: Path | None = None,
) -> dict[int, ItemStats]:
    """Stats over the trailing `days`, recomputed at most every `max_age` seconds."""
    now = now or int(time.time())
    cache = cache or store.path.with_name("stats.json")
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
        if now - raw.get("ts", 0) < max_age and raw.get("days") == days:
            return _decode(raw["stats"])
    stats = compute_stats(store, names, now - int(days * 86400), now)
    cache.write_text(json.dumps({"ts": now, "days": days, "stats": _encode(stats)}), encoding="utf-8")
    return stats
