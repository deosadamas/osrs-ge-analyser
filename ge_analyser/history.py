"""Local store of 5-minute market windows for every item.

Filled from the wiki's bulk `/5m?timestamp=` endpoint (one request per window, all
items), so a week of history is ~2,000 requests once and then one request per 5 min.
Backed by SQLite at ~/.osrs-ge-analyser/history.db (override: GE_ANALYSER_HISTORY).
"""

from __future__ import annotations

import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .api import GEClient, price_int

STEP = 300


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


def align(ts: int) -> int:
    return ts // STEP * STEP


class HistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS w5 (
                ts INTEGER NOT NULL, item INTEGER NOT NULL,
                high INTEGER, hvol INTEGER NOT NULL, low INTEGER, lvol INTEGER NOT NULL,
                PRIMARY KEY (ts, item)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS w5_item ON w5(item, ts);
            CREATE TABLE IF NOT EXISTS fetched (ts INTEGER PRIMARY KEY, n INTEGER NOT NULL);
            """
        )

    # ---- coverage ------------------------------------------------------
    def fetched_timestamps(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT ts FROM fetched")}

    def coverage(self) -> tuple[int | None, int | None, int]:
        row = self.db.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM fetched").fetchone()
        return row[0], row[1], row[2]

    def missing(self, since: int, until: int) -> list[int]:
        have = self.fetched_timestamps()
        return [ts for ts in range(align(since), align(until), STEP) if ts not in have]

    # ---- ingest --------------------------------------------------------
    def store_window(self, ts: int, rows: dict[int, dict]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO w5 VALUES (?,?,?,?,?,?)",
            (
                (ts, item, price_int(r.get("avgHighPrice")), r.get("highPriceVolume") or 0, price_int(r.get("avgLowPrice")), r.get("lowPriceVolume") or 0)
                for item, r in rows.items()
            ),
        )
        self.db.execute("INSERT OR REPLACE INTO fetched VALUES (?,?)", (ts, len(rows)))
        self.db.commit()

    def update(
        self,
        client: GEClient,
        days: float,
        now: int | None = None,
        workers: int = 4,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Fetch every window in the last `days` that we don't have yet. The most recent
        window may still be filling, so stop one step short of now."""
        now = now or int(time.time())
        todo = self.missing(now - int(days * 86400), now - STEP)
        if not todo:
            return 0
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for ts, rows in zip(todo, pool.map(lambda t: client.window_at("5m", t), todo)):
                self.store_window(ts, rows)
                done += 1
                if progress:
                    progress(done, len(todo))
        return done

    # ---- query ---------------------------------------------------------
    def series(self, since: int, until: int | None = None) -> dict[int, list[Row]]:
        """Per-item rows in [since, until), ordered by time."""
        until = until or 1 << 40
        out: dict[int, list[Row]] = {}
        for item, ts, high, hvol, low, lvol in self.db.execute(
            "SELECT item, ts, high, hvol, low, lvol FROM w5 WHERE ts >= ? AND ts < ? ORDER BY item, ts", (since, until)
        ):
            out.setdefault(item, []).append(Row(ts, high, hvol, low, lvol))
        return out

    def windows(self, since: int, until: int | None = None) -> Iterator[tuple[int, dict[int, dict]]]:
        """Yield (ts, {item: raw-shaped row}) in time order; shaped like the API so the
        simulator's snapshot_from_buckets can consume it."""
        until = until or 1 << 40
        current_ts: int | None = None
        bucket: dict[int, dict] = {}
        for ts, item, high, hvol, low, lvol in self.db.execute(
            "SELECT ts, item, high, hvol, low, lvol FROM w5 WHERE ts >= ? AND ts < ? ORDER BY ts, item", (since, until)
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
