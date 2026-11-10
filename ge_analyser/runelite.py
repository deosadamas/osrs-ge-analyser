"""Reader for RuneLite Flipping Utilities data.

The plugin stores one file per account at ~/.runelite/flipping/<DisplayName>.json
(plus accountwide.json, which we ignore). Field names are Gson @SerializedName
abbreviations from the plugin source (com.flippingutilities.model):

    trades[]            FlippingItem
      id, name, tGL     item id, name, GE buy limit
      h.sO[]            HistoryManager.compressedOfferEvents: ONE OfferEvent per trade,
                        holding the latest state of that trade
        b               true = buy offer
        id              item id
        cQIT            units traded so far in this trade
        tQIT            units the offer was placed for
        p               average price paid/received per unit (spent / quantity)
        t               java.time.Instant via plain Gson -> {"seconds": n, "nanos": n}
        s               GE slot (-1 = added manually from GE history)
        st              GrandExchangeOfferState name (BOUGHT, SOLD, BUYING, SELLING,
                        CANCELLED_BUY, CANCELLED_SELL, EMPTY)
        tSFO            game ticks (0.6 s) between the offer being placed and this state
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TICK_SECONDS = 0.6
COMPLETE_STATES = {"BOUGHT", "SOLD", "CANCELLED_BUY", "CANCELLED_SELL"}


@dataclass(frozen=True)
class RealTrade:
    account: str
    item_id: int
    name: str
    buy: bool
    qty: int  # units actually traded
    offered_qty: int  # units the offer was placed for
    price: int  # average per unit
    ts: int  # unix seconds of the latest state update
    state: str
    complete: bool
    fill_seconds: float | None  # time from placement to this state; None if unknown


def default_dir() -> Path:
    return Path.home() / ".runelite" / "flipping"


def account_files(directory: Path | None = None) -> list[Path]:
    directory = directory or default_dir()
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.json") if p.name != "accountwide.json")


def parse_instant(value: Any) -> int | None:
    """Gson writes Instant as {"seconds","nanos"}; tolerate ISO strings and epoch numbers too."""
    if value is None:
        return None
    if isinstance(value, dict):
        return int(value.get("seconds", 0))
    if isinstance(value, (int, float)):
        return int(value if value < 1e11 else value / 1000)
    if isinstance(value, str):
        from datetime import datetime

        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    return None


def parse_account(path: Path) -> list[RealTrade]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    account = path.stem
    out: list[RealTrade] = []
    for item in raw.get("trades", []):
        item_id = int(item.get("id", 0))
        name = item.get("name") or f"id {item_id}"
        for ev in (item.get("h") or {}).get("sO", []):
            qty = int(ev.get("cQIT", 0))
            state = str(ev.get("st", ""))
            ts = parse_instant(ev.get("t"))
            if qty <= 0 or state == "EMPTY" or ts is None:
                continue
            ticks = ev.get("tSFO")
            out.append(
                RealTrade(
                    account=account,
                    item_id=int(ev.get("id", item_id)),
                    name=name,
                    buy=bool(ev.get("b", False)),
                    qty=qty,
                    offered_qty=int(ev.get("tQIT", qty)),
                    price=int(ev.get("p", 0)),
                    ts=ts,
                    state=state,
                    complete=state in COMPLETE_STATES,
                    fill_seconds=ticks * TICK_SECONDS if isinstance(ticks, (int, float)) and ticks > 0 else None,
                )
            )
    out.sort(key=lambda t: t.ts)
    return out


def plugin_log_path() -> Path:
    return Path.home() / ".osrs-ge-analyser" / "runelite-trades.jsonl"


def parse_plugin_log(path: Path) -> list[RealTrade]:
    """Trades recorded by the bundled GE Analyser RuneLite plugin (runelite-plugin/).

    One JSON object per line, one line per GrandExchangeOfferChanged event:
      {"t": unix, "acc": name, "slot": n, "b": bool, "id": item, "name": str,
       "qty": traded so far, "total": offered, "price": avg per unit, "st": state,
       "placed": unix when the offer was first seen in this slot}
    Each trade is represented by its last event (events for the same slot and item
    supersede earlier ones until a complete state is seen).
    """
    last: dict[tuple[str, int], dict] = {}
    done: list[dict] = []
    seen: set[tuple] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        if ev.get("st") == "EMPTY" or int(ev.get("id", 0)) == 0:
            continue
        key = (ev.get("acc", path.stem), int(ev.get("slot", -1)))
        last[key] = ev
        if ev.get("st") in COMPLETE_STATES:
            # The client replays every slot's final state on login; an identical completed
            # trade in the same slot is that replay, not a second trade.
            sig = (*key, ev.get("id"), ev.get("b"), ev.get("qty"), ev.get("total"), ev.get("price"), ev.get("st"))
            if sig not in seen:
                seen.add(sig)
                done.append(ev)
            del last[key]
    out: list[RealTrade] = []
    for ev in done + list(last.values()):
        qty = int(ev.get("qty", 0))
        if qty <= 0:
            continue
        ts = int(ev["t"])
        placed = ev.get("placed")
        out.append(
            RealTrade(
                account=ev.get("acc", path.stem),
                item_id=int(ev["id"]),
                name=ev.get("name") or f"id {ev['id']}",
                buy=bool(ev.get("b", False)),
                qty=qty,
                offered_qty=int(ev.get("total", qty)),
                price=int(ev.get("price", 0)),
                ts=ts,
                state=str(ev.get("st", "")),
                complete=ev.get("st") in COMPLETE_STATES,
                fill_seconds=float(ts - placed) if isinstance(placed, (int, float)) and ts > placed else None,
            )
        )
    out.sort(key=lambda t: t.ts)
    return out


def load_trades(paths: Iterable[Path]) -> list[RealTrade]:
    trades: list[RealTrade] = []
    for p in paths:
        trades.extend(parse_plugin_log(p) if p.suffix == ".jsonl" else parse_account(p))
    trades.sort(key=lambda t: t.ts)
    return trades
