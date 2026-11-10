"""Per-item features from the local 5m history store, overall and by time of week.

Features answer questions the live spread can't:
- fill_buy / fill_sell: did a bid (ask) placed at a window's instant-sell (instant-buy)
  get crossed by a later window within 30 min? Same strict-crossing rule the simulator
  uses, so the rates are directly a fill-probability estimate for it.
- median_margin: the typical after-tax spread; today's margin above it is a spike.
- volatility_pct: how much the mid moves per 5 min; margins smaller than this are noise.
- typical_volume: median one-sided units per hour; the liquidity you can expect.

Time-of-week buckets: the GE has a strong daily and weekly rhythm (EU evening peaks,
weekend volume). Each item gets overall stats plus stats for each of 12 buckets
(six 4-hour UTC blocks x weekday/weekend) when that bucket has enough data;
`TimedStats.at(ts)` returns the right mix for a given moment.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .analysis import ItemStats, ge_tax
from .history import HistoryStore

CROSS_WINDOW_S = 30 * 60
MIN_WINDOWS = 12  # at least an hour of two-sided prints to say anything overall
MIN_BUCKET_WINDOWS = 72  # six hours of two-sided prints before trusting a bucket at all
SHRINK_WINDOWS = 288  # prior weight (in 5m windows) pulling bucket stats toward overall
N_BUCKETS = 12
CACHE_VERSION = 3


def bucket_of(ts: int) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.hour // 4 + (6 if d.weekday() >= 5 else 0)


def bucket_label(b: int) -> str:
    block = b % 6
    return f"{'Sat-Sun' if b >= 6 else 'Mon-Fri'} {block * 4:02d}-{block * 4 + 4:02d} UTC"


@dataclass
class Window:
    ts: int
    high: int
    low: int
    hourly_volume: int  # min(buy, sell) units in this 5m window x 12
    buy_hit: bool
    sell_hit: bool
    judged: bool  # had >= 30 min of future to decide the hits


def crossings(rows: list[tuple]) -> list[Window]:
    """rows: (ts, high, hvol, low, lvol) in time order. Windows missing a side are dropped."""
    quoted = [r for r in rows if r[1] is not None and r[3] is not None]
    out: list[Window] = []
    n = len(quoted)
    last_ts = quoted[-1][0] if quoted else 0
    for i, (ts, high, hvol, low, lvol) in enumerate(quoted):
        judged = last_ts - ts >= CROSS_WINDOW_S
        buy_hit = sell_hit = False
        if judged:
            k = i + 1
            while k < n and quoted[k][0] - ts <= CROSS_WINDOW_S:
                if not buy_hit and quoted[k][3] < low:
                    buy_hit = True
                if not sell_hit and quoted[k][1] > high:
                    sell_hit = True
                if buy_hit and sell_hit:
                    break
                k += 1
        out.append(Window(ts, high, low, min(hvol, lvol) * 12, buy_hit, sell_hit, judged))
    return out


def stats_from_windows(ws: list[Window], name: str, min_windows: int) -> ItemStats | None:
    if len(ws) < min_windows:
        return None
    judged = [w for w in ws if w.judged]
    fill_buy = sum(w.buy_hit for w in judged) / len(judged) if judged else 0.0
    fill_sell = sum(w.sell_hit for w in judged) / len(judged) if judged else 0.0
    margins = [w.high - ge_tax(w.high, name) - w.low for w in ws]
    mids = [(w.high + w.low) / 2 for w in ws]
    changes = [(b - a) / a * 100 for a, b in zip(mids, mids[1:]) if a > 0]
    return ItemStats(
        windows=len(ws),
        fill_buy=fill_buy,
        fill_sell=fill_sell,
        median_margin=int(statistics.median(margins)),
        volatility_pct=statistics.pstdev(changes) if len(changes) > 1 else 0.0,
        typical_volume=int(statistics.median(w.hourly_volume for w in ws)),
    )


def item_stats(rows: list[tuple], name: str) -> ItemStats | None:
    return stats_from_windows(crossings(rows), name, MIN_WINDOWS)


@dataclass
class TimedStats:
    overall: dict[int, ItemStats] = field(default_factory=dict)
    buckets: dict[int, dict[int, ItemStats]] = field(default_factory=dict)

    def at(self, ts: int) -> dict[int, ItemStats]:
        """Overall stats, overridden per item by the stats of ts's time-of-week bucket."""
        merged = dict(self.overall)
        merged.update(self.buckets.get(bucket_of(ts), {}))
        return merged

    def flat_at(self, ts: int) -> dict[int, ItemStats]:
        """Provider ignoring time of week; for comparing against `at`."""
        return self.overall

    def encode(self) -> dict:
        enc = lambda d: {str(i): [s.windows, s.fill_buy, s.fill_sell, s.median_margin, s.volatility_pct, s.typical_volume] for i, s in d.items()}
        return {"overall": enc(self.overall), "buckets": {str(b): enc(d) for b, d in self.buckets.items()}}

    @classmethod
    def decode(cls, raw: dict) -> "TimedStats":
        dec = lambda d: {int(i): ItemStats(*v) for i, v in d.items()}
        return cls(overall=dec(raw["overall"]), buckets={int(b): dec(d) for b, d in raw["buckets"].items()})


def shrink(bucket: ItemStats, overall: ItemStats, k: int = SHRINK_WINDOWS) -> ItemStats:
    """Blend a bucket's estimate toward the item's overall estimate with weight n/(n+k).
    A bucket with a day of prints is trusted half-way; a bucket with a week, 7/8."""
    w = bucket.windows / (bucket.windows + k)
    mix = lambda a, b: w * a + (1 - w) * b
    return ItemStats(
        windows=bucket.windows,
        fill_buy=mix(bucket.fill_buy, overall.fill_buy),
        fill_sell=mix(bucket.fill_sell, overall.fill_sell),
        median_margin=int(round(mix(bucket.median_margin, overall.median_margin))),
        volatility_pct=mix(bucket.volatility_pct, overall.volatility_pct),
        typical_volume=int(round(mix(bucket.typical_volume, overall.typical_volume))),
    )


def compute_timed_stats(
    store: HistoryStore, names: dict[int, str], since: int, until: int | None = None, shrink_k: int = SHRINK_WINDOWS
) -> TimedStats:
    ts_stats = TimedStats(buckets={b: {} for b in range(N_BUCKETS)})
    for item_id, rows in store.iter_series(since, until):
        name = names.get(item_id, "")
        ws = crossings(rows)
        overall = stats_from_windows(ws, name, MIN_WINDOWS)
        if overall is None:
            continue
        ts_stats.overall[item_id] = overall
        by_bucket: dict[int, list[Window]] = {}
        for w in ws:
            by_bucket.setdefault(bucket_of(w.ts), []).append(w)
        for b, bws in by_bucket.items():
            s = stats_from_windows(bws, name, MIN_BUCKET_WINDOWS)
            if s is not None:
                ts_stats.buckets[b][item_id] = shrink(s, overall, shrink_k)
    return ts_stats


def compute_stats(store: HistoryStore, names: dict[int, str], since: int, until: int | None = None) -> dict[int, ItemStats]:
    """Overall (bucket-free) stats; kept for callers that don't care about time of week."""
    return compute_timed_stats(store, names, since, until).overall


def cached_timed_stats(
    store: HistoryStore,
    names: dict[int, str],
    days: float,
    now: int | None = None,
    max_age: int = 6 * 3600,
    cache: Path | None = None,
) -> TimedStats:
    """Stats over the trailing `days`, recomputed at most every `max_age` seconds
    (bucket statistics move slowly; a 14-day window shifts little in six hours)."""
    now = now or int(time.time())
    cache = cache or store.path.with_name("stats.json")
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
        if raw.get("v") == CACHE_VERSION and now - raw.get("ts", 0) < max_age and raw.get("days") == days:
            return TimedStats.decode(raw["stats"])
    stats = compute_timed_stats(store, names, now - int(days * 86400), now)
    cache.write_text(json.dumps({"v": CACHE_VERSION, "ts": now, "days": days, "stats": stats.encode()}), encoding="utf-8")
    return stats
