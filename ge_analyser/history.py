"""Local store of market history for every item.

Three resolutions, each from a different wiki endpoint:

| table | source                      | default depth | use |
|-------|-----------------------------|---------------|-----|
| w5    | /5m?timestamp=  (all items) | 30 days       | fill rates, spreads, intraday timing |
| w1h   | /1h?timestamp=  (all items) | 365 days      | hour-of-week volume / liquidity patterns |
| daily | /timeseries?lookback=1y     | 1 year        | long-run trend per item |

Bulk windows are one request per window for all items (gzip, ~20-45 KB each), fetched
with a rate limit so a full backfill (~17k requests) stays well under the wiki's
acceptable-use line. Backed by SQLite at ~/.osrs-ge-analyser/history.db
(override: GE_ANALYSER_HISTORY).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .api import GEClient, price_int

STEPS = {"5m": 300, "1h": 3600}
TABLES = {"5m": "w5", "1h": "w1h"}
DEFAULT_DEPTH_DAYS = {"5m": 30, "1h": 365}
STEP = STEPS["5m"]  # 5m is the primary resolution used by features
DAILY_REFRESH_S = 20 * 3600


@dataclass(frozen=True)
class Row:
    ts: int
    high: int | None
    high_vol: int
    low: int | None
    low_vol: int


def default_path() -> Path:
    override = os.environ.get("GE_ANALYSER_HISTORY")
    return Path(override) if override else Path.home() / ".osrs-ge-analyser" / "history.db"


def align(ts: int, step: str = "5m") -> int:
    unit = STEPS[step]
    return ts // unit * unit


class RateLimiter:
    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        if start > now:
            time.sleep(start - now)


class HistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS w5 (
                ts INTEGER NOT NULL, item INTEGER NOT NULL,
                high INTEGER, hvol INTEGER NOT NULL, low INTEGER, lvol INTEGER NOT NULL,
                PRIMARY KEY (ts, item)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS w5_item ON w5(item, ts);
            CREATE TABLE IF NOT EXISTS w1h (
                ts INTEGER NOT NULL, item INTEGER NOT NULL,
                high INTEGER, hvol INTEGER NOT NULL, low INTEGER, lvol INTEGER NOT NULL,
                PRIMARY KEY (ts, item)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS w1h_item ON w1h(item, ts);
            CREATE TABLE IF NOT EXISTS daily (
                item INTEGER NOT NULL, ts INTEGER NOT NULL,
                high INTEGER, hvol INTEGER NOT NULL, low INTEGER, lvol INTEGER NOT NULL,
                PRIMARY KEY (item, ts)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS fetched_w (step TEXT NOT NULL, ts INTEGER NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (step, ts));
            CREATE TABLE IF NOT EXISTS fetched_daily (item INTEGER PRIMARY KEY, at INTEGER NOT NULL, n INTEGER NOT NULL);
            """
        )
        self._migrate()

    def _migrate(self) -> None:
        old = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fetched'").fetchone()
        if old:
            self.db.execute("INSERT OR IGNORE INTO fetched_w SELECT '5m', ts, n FROM fetched")
            self.db.execute("DROP TABLE fetched")
            self.db.commit()

    # ---- coverage ------------------------------------------------------
    def fetched_timestamps(self, step: str = "5m") -> set[int]:
        return {r[0] for r in self.db.execute("SELECT ts FROM fetched_w WHERE step=?", (step,))}

    def coverage(self, step: str = "5m") -> tuple[int | None, int | None, int]:
        row = self.db.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM fetched_w WHERE step=?", (step,)).fetchone()
        return row[0], row[1], row[2]

    def daily_coverage(self) -> tuple[int, int | None]:
        row = self.db.execute("SELECT COUNT(*), MIN(ts) FROM daily").fetchone()
        return row[0], row[1]

    def missing(self, since: int, until: int, step: str = "5m") -> list[int]:
        have = self.fetched_timestamps(step)
        unit = STEPS[step]
        return [ts for ts in range(align(since, step), align(until, step), unit) if ts not in have]

    # ---- ingest --------------------------------------------------------
    def store_window(self, ts: int, rows: dict[int, dict], step: str = "5m") -> None:
        table = TABLES[step]
        with self.lock:
            self.db.executemany(
                f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?)",
                (
                    (ts, item, price_int(r.get("avgHighPrice")), r.get("highPriceVolume") or 0, price_int(r.get("avgLowPrice")), r.get("lowPriceVolume") or 0)
                    for item, r in rows.items()
                ),
            )
            self.db.execute("INSERT OR REPLACE INTO fetched_w VALUES (?,?,?)", (step, ts, len(rows)))
            self.db.commit()

    def update(
        self,
        client: GEClient,
        days: float,
        now: int | None = None,
        step: str = "5m",
        workers: int = 4,
        rate: float = 6.0,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Fetch every `step` window in the last `days` that we don't have yet. The most
        recent window may still be aggregating, so stop one step short of now."""
        now = now or int(time.time())
        todo = self.missing(now - int(days * 86400), now - STEPS[step], step)
        if not todo:
            return 0
        limiter = RateLimiter(rate)

        def fetch(ts: int) -> dict[int, dict]:
            limiter.wait()
            return client.window_at(step, ts)

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for ts, rows in zip(todo, pool.map(fetch, todo)):
                self.store_window(ts, rows, step)
                done += 1
                if progress:
                    progress(done, len(todo))
        return done

    def update_daily(
        self,
        client: GEClient,
        item_ids: Iterable[int],
        now: int | None = None,
        workers: int = 4,
        rate: float = 6.0,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Refresh the 1-year daily series for items not fetched in the last ~20h."""
        now = now or int(time.time())
        fresh = {r[0] for r in self.db.execute("SELECT item FROM fetched_daily WHERE at > ?", (now - DAILY_REFRESH_S,))}
        todo = [i for i in item_ids if i not in fresh]
        if not todo:
            return 0
        limiter = RateLimiter(rate)

        def fetch(item: int) -> list[dict]:
            limiter.wait()
            return client.timeseries(item, "1y")

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for item, series in zip(todo, pool.map(fetch, todo)):
                with self.lock:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?)",
                        (
                            (item, p["timestamp"], price_int(p.get("avgHighPrice")), p.get("highPriceVolume") or 0, price_int(p.get("avgLowPrice")), p.get("lowPriceVolume") or 0)
                            for p in series
                        ),
                    )
                    self.db.execute("INSERT OR REPLACE INTO fetched_daily VALUES (?,?,?)", (item, now, len(series)))
                    self.db.commit()
                done += 1
                if progress:
                    progress(done, len(todo))
        return done

    # ---- query ---------------------------------------------------------
    def iter_series(self, since: int, until: int | None = None, step: str = "5m") -> Iterator[tuple[int, list[tuple]]]:
        """Yield (item, [(ts, high, hvol, low, lvol), ...]) in time order, one item at a time."""
        until = until or 1 << 40
        table = TABLES[step]
        current: int | None = None
        rows: list[tuple] = []
        for item, ts, high, hvol, low, lvol in self.db.execute(
            f"SELECT item, ts, high, hvol, low, lvol FROM {table} WHERE ts >= ? AND ts < ? ORDER BY item, ts", (since, until)
        ):
            if item != current:
                if current is not None:
                    yield current, rows
                current, rows = item, []
            rows.append((ts, high, hvol, low, lvol))
        if current is not None:
            yield current, rows

    def series(self, since: int, until: int | None = None, step: str = "5m") -> dict[int, list[Row]]:
        return {item: [Row(*r) for r in rows] for item, rows in self.iter_series(since, until, step)}

    def daily_series(self, item: int) -> list[Row]:
        return [Row(*r) for r in self.db.execute("SELECT ts, high, hvol, low, lvol FROM daily WHERE item=? ORDER BY ts", (item,))]

    def windows(self, since: int, until: int | None = None, step: str = "5m") -> Iterator[tuple[int, dict[int, dict]]]:
        """Yield (ts, {item: raw-shaped row}) in time order; shaped like the API so the
        simulator's snapshot_from_buckets can consume it."""
        until = until or 1 << 40
        table = TABLES[step]
        current_ts: int | None = None
        bucket: dict[int, dict] = {}
        for ts, item, high, hvol, low, lvol in self.db.execute(
            f"SELECT ts, item, high, hvol, low, lvol FROM {table} WHERE ts >= ? AND ts < ? ORDER BY ts, item", (since, until)
        ):
            if ts != current_ts:
                if current_ts is not None:
                    yield current_ts, bucket
                current_ts, bucket = ts, {}
            bucket[item] = {"avgHighPrice": high, "highPriceVolume": hvol, "avgLowPrice": low, "lowPriceVolume": lvol}
        if current_ts is not None:
            yield current_ts, bucket

    def close(self) -> None:
        self.db.close()
