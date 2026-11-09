"""Client for the OSRS Wiki real-time prices API.

Docs: https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices
The wiki requires a descriptive User-Agent; anonymous clients get blocked.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_URL = "https://prices.runescape.wiki/api/v1/osrs"
USER_AGENT = "osrs-ge-analyser/0.1 (https://github.com/deosadamas/osrs-ge-analyser)"


@dataclass(frozen=True)
class ItemInfo:
    id: int
    name: str
    members: bool
    limit: int  # GE buy limit per 4 hours; 0 = unknown
    value: int  # store value, used for alch calculations
    highalch: int


@dataclass(frozen=True)
class LatestPrice:
    high: int | None  # most recent instant-buy price
    high_time: int | None
    low: int | None  # most recent instant-sell price
    low_time: int | None


@dataclass(frozen=True)
class WindowPrice:
    """Aggregated prices/volumes over a 5m or 1h window."""

    avg_high: int | None
    high_volume: int
    avg_low: int | None
    low_volume: int


class GEClient:
    def __init__(self, cache_dir: Path | None = None, cache_ttl: int = 60) -> None:
        self.cache_dir = cache_dir or Path(".cache")
        self.cache_ttl = cache_ttl

    def _get(self, endpoint: str, ttl: int | None = None) -> Any:
        ttl = self.cache_ttl if ttl is None else ttl
        cache_file = self.cache_dir / (endpoint.replace("/", "_").replace("?", "_").replace("&", "_") + ".json")
        if ttl > 0 and cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
            return json.loads(cache_file.read_text(encoding="utf-8"))

        req = urllib.request.Request(f"{BASE_URL}/{endpoint}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if ttl > 0:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    def mapping(self) -> dict[int, ItemInfo]:
        # Item metadata changes rarely; cache for a day.
        raw = self._get("mapping", ttl=86_400)
        return {
            row["id"]: ItemInfo(
                id=row["id"],
                name=row["name"],
                members=row.get("members", False),
                limit=row.get("limit", 0) or 0,
                value=row.get("value", 0) or 0,
                highalch=row.get("highalch", 0) or 0,
            )
            for row in raw
        }

    def latest(self) -> dict[int, LatestPrice]:
        raw = self._get("latest")["data"]
        return {
            int(item_id): LatestPrice(
                high=row.get("high"),
                high_time=row.get("highTime"),
                low=row.get("low"),
                low_time=row.get("lowTime"),
            )
            for item_id, row in raw.items()
        }

    def window(self, step: str) -> dict[int, WindowPrice]:
        """step: '5m' or '1h'. Returns the most recent completed window."""
        if step not in ("5m", "1h"):
            raise ValueError("step must be '5m' or '1h'")
        raw = self._get(step)["data"]
        return {
            int(item_id): WindowPrice(
                avg_high=row.get("avgHighPrice"),
                high_volume=row.get("highPriceVolume") or 0,
                avg_low=row.get("avgLowPrice"),
                low_volume=row.get("lowPriceVolume") or 0,
            )
            for item_id, row in raw.items()
        }

    def timeseries(self, item_id: int, step: str = "1h") -> list[dict[str, Any]]:
        """Up to 365 data points for one item. step: 5m, 1h, 6h, 24h."""
        if step not in ("5m", "1h", "6h", "24h"):
            raise ValueError("step must be one of 5m, 1h, 6h, 24h")
        return self._get(f"timeseries?timestep={step}&id={item_id}", ttl=300)["data"]
