"""Local ledger of recent purchases, used to track remaining GE buy limits.

The GE buy limit is a rolling window: every unit bought counts against the item's
limit for the next 4 hours. We store (item_id, qty, timestamp) rows in a JSON file
and sum the ones still inside the window.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

LIMIT_WINDOW_S = 4 * 3600


@dataclass(frozen=True)
class Purchase:
    item_id: int
    qty: int
    ts: int


def default_path() -> Path:
    override = os.environ.get("GE_ANALYSER_LEDGER")
    if override:
        return Path(override)
    return Path.home() / ".osrs-ge-analyser" / "ledger.json"


class Ledger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self._rows: list[Purchase] = []
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._rows = [Purchase(int(r["item_id"]), int(r["qty"]), int(r["ts"])) for r in raw]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([{"item_id": p.item_id, "qty": p.qty, "ts": p.ts} for p in self._rows]),
            encoding="utf-8",
        )

    def active(self, now: int) -> list[Purchase]:
        cutoff = now - LIMIT_WINDOW_S
        return [p for p in self._rows if p.ts > cutoff]

    def record(self, item_id: int, qty: int, now: int) -> None:
        """Record a purchase; drops rows that have already left the 4h window."""
        self._rows = self.active(now)
        self._rows.append(Purchase(item_id, qty, now))
        self._save()

    def used(self, now: int) -> dict[int, int]:
        """Units bought per item within the current 4h window."""
        out: dict[int, int] = {}
        for p in self.active(now):
            out[p.item_id] = out.get(p.item_id, 0) + p.qty
        return out

    def resets_at(self, item_id: int, now: int) -> int | None:
        """Timestamp when the oldest in-window purchase of this item expires."""
        ts = [p.ts for p in self.active(now) if p.item_id == item_id]
        return min(ts) + LIMIT_WINDOW_S if ts else None

    def clear(self, item_id: int | None = None) -> int:
        """Forget purchases (all, or for one item). Returns rows removed."""
        before = len(self._rows)
        self._rows = [] if item_id is None else [p for p in self._rows if p.item_id != item_id]
        self._save()
        return before - len(self._rows)
