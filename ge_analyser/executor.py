"""Human-in-the-loop execution engine.

Nothing here touches the game. Each cycle the engine is given a *status* - the orders
you have in the Grand Exchange and how far each has filled, optionally your coins and
inventory - and answers with a complete, ordered list of actions:

    COLLECT  an order finished or was aborted: collect it, its slot is free
    CANCEL   abort this order and collect what it holds
    SELL     place a sell offer: item, quantity, price
    BUY      place a buy offer: item, quantity, price
    HOLD     leave this order / this stack alone

The engine never assumes an instruction was carried out. Fills, positions, profit and
the coin count come only from what the status reports (coins are stated once, then
counted from the reported fills and offers; stating them again corrects the count). An
instruction that has not shown up in the status yet is repeated unchanged (same order
id, same price) for a few minutes and then re-planned, so the list stays stable while
you are still typing offers in.

It plans within the real constraints: GE slots (buy and sell offers share them),
inventory stacks (one per item type - collect as notes), cash, 4-hour buy limits.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .analysis import DEFAULT_PARAMS, MARGIN_CAP, ItemStats, ScoreParams, capped_ask, find_flips, ge_tax, parse_gp
from .api import ItemInfo, LatestPrice, WindowPrice

COIN_IDS = frozenset({995, 13204})  # Coins, Platinum token
COIN_NAMES = frozenset({"coins", "platinum token", "platinum tokens"})
SIGNAL_MAX_AGE = 900  # a market print older than this never cancels a buy on "margin gone"
MAX_DONE = 1000  # finished orders kept for the report and the fill calibration
MAX_WITHDRAWN = 40  # never-placed instructions kept so a late placement is still recognised

_STATUS = {
    "open": "open", "pending": "open", "placed": "open", "new": "open", "waiting": "open", "active": "open",
    "filling": "filling", "partial": "filling", "partially_filled": "filling", "in_progress": "filling",
    "buying": "filling", "selling": "filling",
    "executed": "executed", "filled": "executed", "complete": "executed", "completed": "executed",
    "done": "executed", "bought": "executed", "sold": "executed",
    "cancelled": "cancelled", "canceled": "cancelled", "aborted": "cancelled",
    "cancelled_buy": "cancelled", "cancelled_sell": "cancelled",
}
_BUY_WORDS = frozenset({"buy", "b", "bid", "buying", "bought", "cancelled_buy"})
_SELL_WORDS = frozenset({"sell", "s", "ask", "selling", "sold", "cancelled_sell"})


class StatusError(ValueError):
    """The status file cannot be understood; the message says what to fix."""


# ---- status ----------------------------------------------------------------


@dataclass
class OrderReport:
    order_id: int | None = None
    side: str | None = None
    item_id: int | None = None
    status: str | None = None  # open | filling | executed | cancelled
    filled: int | None = None
    price: int | None = None
    qty: int | None = None
    spent: int | None = None  # coins paid / received (before tax) for the filled units so far
    slot: int | None = None


@dataclass
class Status:
    ts: int
    cash: int | None  # coins in the inventory; None = no correction, the engine's own count stands
    orders: list[OrderReport] = field(default_factory=list)
    inventory: dict[int, int] | None = None  # item id -> units; None = not reported
    slots: int | None = None
    inventory_capacity: int | None = None
    members: bool | None = None


def _key(text: Any) -> str:
    return str(text).strip().lower().replace("-", "_").replace(" ", "_")


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise StatusError(f"{what} must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return parse_gp(value)
        except ValueError:
            pass
    raise StatusError(f"{what} must be a number, got {value!r}")


def _first(entry: dict, *keys: str) -> Any:
    for k in keys:
        if k in entry and entry[k] is not None:
            return entry[k]
    return None


def parse_status(raw: Any, items: dict[int, ItemInfo], ts: int) -> Status:
    """Tolerant reader for status.json. See README "Execution loop" for the format."""
    if not isinstance(raw, dict):
        raise StatusError("status must be a JSON object")
    cash = _first(raw, "cash", "coins", "gp")
    names = {it.name.lower(): it.id for it in items.values()}

    def item_of(entry: dict, strict: bool) -> int | None:
        v = _first(entry, "item_id", "id")
        if v is not None and not isinstance(v, bool) and str(v).lstrip("-").isdigit():
            return int(v)
        v = _first(entry, "item", "name")
        if v is None:
            return None
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        text = str(v).strip()
        if text.isdigit():
            return int(text)
        found = names.get(text.lower())
        if found is None and strict:
            raise StatusError(f"unknown item name {text!r} (use the exact GE name or its item id)")
        return found

    orders: list[OrderReport] = []
    for n, e in enumerate(_first(raw, "orders", "offers") or []):
        if not isinstance(e, dict):
            raise StatusError(f"orders[{n}] must be an object")
        status = side = None
        s = _first(e, "status", "state", "st")
        if s is not None:
            k = _key(s)
            if k == "empty":
                continue
            if k not in _STATUS:
                raise StatusError(f"orders[{n}]: unknown status {s!r}; use open, filling, executed or cancelled")
            status = _STATUS[k]
            side = "buy" if k in _BUY_WORDS else "sell" if k in _SELL_WORDS else None
        sd = _first(e, "side", "type")
        if sd is not None:
            k = _key(sd)
            if k not in _BUY_WORDS and k not in _SELL_WORDS:
                raise StatusError(f"orders[{n}]: side must be buy or sell, got {sd!r}")
            side = "buy" if k in _BUY_WORDS else "sell"
        elif isinstance(e.get("b"), bool):
            side = "buy" if e["b"] else "sell"
        oid = _first(e, "order_id", "order")
        filled = _first(e, "filled", "done_qty", "traded")
        price = _first(e, "price", "listed")
        qty = _first(e, "qty", "quantity", "total")
        spent = _first(e, "spent", "coins")
        slot = e.get("slot")
        r = OrderReport(
            order_id=_int(oid, f"orders[{n}].order_id") if oid is not None else None,
            side=side,
            item_id=item_of(e, strict=True),
            status=status,
            filled=_int(filled, f"orders[{n}].filled") if filled is not None else None,
            price=_int(price, f"orders[{n}].price") if price is not None else None,
            qty=_int(qty, f"orders[{n}].qty") if qty is not None else None,
            spent=_int(spent, f"orders[{n}].spent") if spent is not None else None,
            slot=_int(slot, f"orders[{n}].slot") if slot is not None else None,
        )
        if r.order_id is None and (r.side is None or r.item_id is None):
            raise StatusError(f"orders[{n}] needs an order_id, or an item plus side (buy/sell)")
        if r.status is None:
            r.status = "filling" if (r.filled or 0) > 0 else "open"
        if r.status == "filling" and not (r.filled or 0):
            r.status = "open" if r.filled == 0 else "filling"
        orders.append(r)

    inventory: dict[int, int] | None = None
    inv_raw = raw.get("inventory")
    if inv_raw is not None:
        inventory = {}
        entries = [{"item": k, "qty": v} for k, v in inv_raw.items()] if isinstance(inv_raw, dict) else list(inv_raw)
        for n, e in enumerate(entries):
            if not isinstance(e, dict):
                raise StatusError(f"inventory[{n}] must be an object like {{\"item\": \"Iron ore\", \"qty\": 75}}")
            label = _first(e, "item", "name")
            if isinstance(label, str) and label.strip().lower() in COIN_NAMES:
                continue
            item_id = item_of(e, strict=False)
            qty = _int(_first(e, "qty", "quantity", "amount") or 1, f"inventory[{n}].qty")
            if qty <= 0 or (item_id is not None and item_id in COIN_IDS):
                continue
            if item_id is None:
                item_id = -(n + 1)  # something we cannot trade: it still takes a stack
            inventory[item_id] = inventory.get(item_id, 0) + qty

    slots = _first(raw, "slots", "ge_slots")
    cap = _first(raw, "inventory_capacity", "inventory_slots", "capacity")
    members = raw.get("members")
    if members is None and isinstance(raw.get("f2p"), bool):
        members = not raw["f2p"]
    if members is not None and not isinstance(members, bool):
        raise StatusError('"members" must be true or false')
    return Status(
        ts=ts,
        cash=_int(cash, "cash") if cash is not None else None,
        orders=orders,
        inventory=inventory,
        slots=_int(slots, "slots") if slots is not None else None,
        inventory_capacity=_int(cap, "inventory_capacity") if cap is not None else None,
        members=members,
    )


# ---- engine memory ---------------------------------------------------------


@dataclass
class ExecOrder:
    id: int
    side: str  # buy | sell
    item_id: int
    name: str
    price: int
    qty: int
    issued: int  # when the instruction was first given
    state: str = "pending"  # pending (instructed, not seen yet) | working | closed
    filled: int = 0
    gross: int = 0  # coins paid / received before tax for the filled units
    seen: int = 0  # when the status first confirmed it
    last_progress: int = 0  # last time it filled (or was first seen)
    target: int = 0  # buys: the ask planned once it fills
    kind: str = "list"  # sells: list | reprice | dump
    cancel_requested: int = 0
    missing_since: int = 0
    closed: int = 0
    outcome: str = ""  # executed | cancelled | withdrawn
    slot: int | None = None
    model_fill: float | None = None  # what the paper fill model gives for this order (calibration)

    @property
    def remaining(self) -> int:
        return self.qty - self.filled


@dataclass
class Position:
    name: str = ""
    qty: int = 0
    cost: float = 0.0  # average price paid per unit
    target: int = 0  # ask planned when it was bought
    stage: str = "list"  # list | reprice | dump
    listed_since: int = 0  # when the current lot was first on sale


@dataclass
class Fill:
    ts: int
    order_id: int
    side: str
    item_id: int
    name: str
    qty: int
    price: int
    tax: int
    profit: int


@dataclass
class ExecState:
    seq: int = 0
    next_id: int = 1
    orders: list[ExecOrder] = field(default_factory=list)
    done: list[ExecOrder] = field(default_factory=list)
    positions: dict[int, Position] = field(default_factory=dict)
    realised: int = 0
    tax_paid: int = 0
    sells: int = 0
    wins: int = 0
    start_ts: int = 0
    start_equity: int = 0
    # The coin count: cash = cash_anchor + flow - coins locked in working buy offers.
    flow: int = 0  # sale proceeds after tax minus purchase cost, over every fill reported
    cash_anchor: int | None = None  # set whenever a status corrects the count
    cash_given: int | None = None  # the last "cash" a status stated
    # Account settings last given in a status; they stick until a status changes them.
    members: bool | None = None
    slots: int | None = None
    inventory_capacity: int | None = None

    def locked(self) -> int:
        """Coins sitting in buy offers the status has confirmed."""
        return sum(o.price * o.remaining for o in self.orders if o.side == "buy" and o.state == "working")

    def save(self, path: Path) -> None:
        raw = asdict(self)
        raw["positions"] = {str(i): asdict(p) for i, p in self.positions.items()}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "ExecState":
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["orders"] = [ExecOrder(**o) for o in raw["orders"]]
        raw["done"] = [ExecOrder(**o) for o in raw["done"]]
        raw["positions"] = {int(i): Position(**p) for i, p in raw["positions"].items()}
        return cls(**raw)


@dataclass
class ExecConfig:
    slots: int = 8
    inventory_slots: int = 26  # item stacks you can hold (notes: one stack per item type)
    buy_timeout: int = 30 * 60  # cancel a buy that has not filled anything for this long
    sell_reprice: int = 30 * 60  # relist a sell at the market after this long without a sale
    sell_dump: int = 90 * 60  # sell at the instant-sell price once a lot has been listed this long
    dump_retry: int = 10 * 60
    pending_ttl: int = 180  # keep repeating an instruction unchanged this long before re-planning it
    missing_grace: int = 120  # an order missing from the status is asked about this long, then assumed cancelled
    min_volume: int = 200
    max_age: int = 3600
    members: bool | None = None  # None = all items, False = free-to-play only
    min_margin: int = 2
    min_roi: float = 0.02
    improve: bool = True  # bid 1 gp over the instant-sell, ask 1 gp under the instant-buy
    fill_share: int = 10  # share of crossing volume one order is expected to get: 1/fill_share
    volume_frac: float | None = None  # size orders to this share of hourly volume (None: buy_timeout / fill_share)
    partial_sell_frac: float = 0.2  # list a still-filling buy's units once they are worth this share of the order
    cancel_on_margin: bool = True
    margin_cap: float = MARGIN_CAP  # plan asks at most this multiple of the item's typical margin over cost (0 = off)
    min_order_frac: float = 0.02  # a new buy must be worth at least this share of your equity: no dust orders
    score: ScoreParams = DEFAULT_PARAMS


@dataclass
class Market:
    latest: dict[int, LatestPrice]
    hour: dict[int, WindowPrice]
    stats: dict[int, ItemStats] | None = None
    used: dict[int, int] = field(default_factory=dict)  # units bought per item in the last 4h
    volume_scale: dict[int, float] = field(default_factory=dict)  # per-item sizing scale from real fills
    default_scale: float = 1.0


@dataclass
class Plan:
    seq: int
    ts: int
    status_ts: int
    actions: list[dict[str, Any]]
    warnings: list[str]
    fills: list[Fill]
    account: dict[str, Any]
    reply_template: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "status_age_s": max(self.ts - self.status_ts, 0),
            "account": self.account,
            "actions": self.actions,
            "warnings": self.warnings,
            "fills_booked": [asdict(f) for f in self.fills],
            "reply_template": self.reply_template,
        }


# ---- engine ----------------------------------------------------------------


class Executor:
    def __init__(self, state: ExecState, items: dict[int, ItemInfo], cfg: ExecConfig) -> None:
        self.state = state
        self.items = items
        self.cfg = cfg

    # -- helpers ------------------------------------------------------------
    def _name(self, item_id: int) -> str:
        item = self.items.get(item_id)
        return item.name if item else f"item {item_id}"

    def _new_order(self, side: str, item_id: int, price: int, qty: int, now: int, target: int = 0, kind: str = "list") -> ExecOrder:
        st = self.state
        o = ExecOrder(id=st.next_id, side=side, item_id=item_id, name=self._name(item_id), price=price, qty=qty, issued=now, target=target, kind=kind)
        st.next_id += 1
        st.orders.append(o)
        return o

    def _close(self, o: ExecOrder, outcome: str, now: int) -> None:
        st = self.state
        o.state, o.outcome, o.closed = "closed", outcome, now
        if o in st.orders:
            st.orders.remove(o)
        st.done.append(o)
        if outcome == "withdrawn":
            # Instructions that were never placed are only kept so a late placement can be
            # recognised; they must not push real, finished orders out of the history.
            stale = [x for x in st.done if x.outcome == "withdrawn"][:-MAX_WITHDRAWN]
            if stale:
                gone = {id(x) for x in stale}
                st.done[:] = [x for x in st.done if id(x) not in gone]
        del st.done[:-MAX_DONE]
        if o.side == "sell" and outcome == "executed":
            pos = st.positions.get(o.item_id)
            if pos:  # units bought since this lot was listed start a fresh listing
                pos.stage, pos.listed_since = "list", 0

    def _line(self, action: str, o: ExecOrder, why: str, **extra: Any) -> dict[str, Any]:
        d: dict[str, Any] = {"action": action, "order_id": o.id, "item": o.name, "item_id": o.item_id, "side": o.side, "qty": o.qty, "price": o.price}
        if action in ("HOLD", "CANCEL", "COLLECT"):
            d["filled"] = o.filled
        d.update(extra)
        d["why"] = why
        return d

    def _book_fill(self, o: ExecOrder, units: int, unit_price: int, now: int, fills: list[Fill]) -> None:
        st = self.state
        pos = st.positions.get(o.item_id)
        if o.side == "buy":
            if pos is None:
                pos = st.positions[o.item_id] = Position(name=o.name)
            pos.cost = (pos.cost * pos.qty + unit_price * units) / (pos.qty + units)
            pos.qty += units
            if o.target:
                pos.target = o.target
            st.flow -= unit_price * units
            fills.append(Fill(now, o.id, "buy", o.item_id, o.name, units, unit_price, 0, 0))
        else:
            tax = ge_tax(unit_price, o.name)
            cost = pos.cost if pos else float(unit_price - tax)  # unknown cost: book no profit
            profit = int(round((unit_price - tax - cost) * units))
            st.realised += profit
            st.tax_paid += tax * units
            st.sells += 1
            st.wins += profit > 0
            if pos:
                pos.qty -= units
                if pos.qty <= 0:
                    del st.positions[o.item_id]
            st.flow += (unit_price - tax) * units
            fills.append(Fill(now, o.id, "sell", o.item_id, o.name, units, unit_price, tax * units, profit))
        o.filled += units
        o.gross += unit_price * units
        o.last_progress = now

    def _margin_now(self, price: int, name: str, lp: LatestPrice | None, target: int) -> int:
        """Per-unit profit if a unit bought at `price` were sold at today's ask."""
        if lp is None or lp.high is None:
            ask = target
        else:
            ask = lp.high - 1 if self.cfg.improve and lp.high - 1 > price else lp.high
        return ask - ge_tax(ask, name) - price if ask else 0

    def _ask(self, pos: Position, lp: LatestPrice | None, stats: ItemStats | None = None) -> int:
        cfg = self.cfg
        high = lp.high if lp else None
        low = lp.low if lp else None
        floor = int(pos.cost) + 1
        if pos.stage == "dump" and low:
            ask = max(low, 1)
        elif pos.stage == "reprice" and high:
            ask = max(high - 1 if cfg.improve else high, 1)
        elif not high:
            ask = max(pos.target, floor)  # no market print: fall back to the ask planned at buy time
        elif high <= pos.cost:
            ask = floor  # market is under water: list just above cost, the reprice/dump rules take over
        else:
            ask = high - 1 if cfg.improve and high - 1 > pos.cost else high
        # Whatever the last print says, never list above the item's usual margin over cost.
        return capped_ask(ask, pos.cost, stats, pos.name, cfg.margin_cap, floor_margin=cfg.min_margin)

    # -- reconcile what the status says with the book -------------------------
    def _apply_report(self, o: ExecOrder, r: OrderReport, now: int, fills: list[Fill], collect: list[dict]) -> None:
        st = self.state
        if o.state == "pending":
            o.state, o.seen, o.last_progress = "working", now, now
            if o.side == "sell":
                pos = st.positions.get(o.item_id)
                if pos and not pos.listed_since:
                    pos.listed_since = now
        o.missing_since = 0
        if r.slot is not None:
            o.slot = r.slot
        if r.price and o.filled == 0:
            o.price = r.price  # placed at a different price than instructed
        if r.qty and r.qty >= max(o.filled, 1):
            o.qty = r.qty
        filled = r.filled
        if r.status == "executed":
            if not filled or filled <= o.filled:
                filled = o.qty  # "executed" says it all; a stale or missing count is not a smaller fill
            elif filled < o.qty:
                o.qty = filled  # executed with fewer units: the offer placed was smaller than instructed
        elif filled is None:
            filled = o.filled
        filled = max(o.filled, min(filled, o.qty))
        delta = filled - o.filled
        if delta > 0:
            unit = o.price
            if r.spent is not None and r.spent > o.gross:
                unit = max(int(round((r.spent - o.gross) / delta)), 1)
            self._book_fill(o, delta, unit, now, fills)
        if r.status == "executed" or o.filled >= o.qty:
            verb = "bought" if o.side == "buy" else "sold"
            self._close(o, "executed", now)
            collect.append(self._line("COLLECT", o, f"executed: {o.filled:,}/{o.qty:,} {verb}; collect to free the slot"))
        elif r.status == "cancelled":
            self._close(o, "cancelled", now)
            collect.append(self._line("COLLECT", o, f"cancelled at {o.filled:,}/{o.qty:,}; collect to free the slot"))

    def _resolve_by_inventory(self, o: ExecOrder, status: Status, now: int, fills: list[Fill]) -> bool:
        """An order left the GE without a final report: read its fate off the inventory."""
        if status.inventory is None:
            return False
        st = self.state
        others = [x for x in st.orders if x is not o and x.item_id == o.item_id and x.state == "working"]
        if any(x.side == "buy" and x.filled for x in others):
            return False  # bought units may still sit uncollected in another offer: cannot tell
        inv = status.inventory.get(o.item_id, 0)
        pos = st.positions.get(o.item_id)
        tracked = pos.qty if pos else 0
        selling = sum(x.remaining for x in others if x.side == "sell")
        if o.side == "buy":
            more = max(0, min(inv + selling - tracked, o.remaining))
        else:
            more = max(0, min(tracked - selling - inv, o.remaining))  # tracked still includes this order's unsold units
        if more:
            self._book_fill(o, more, o.price, now, fills)
        self._close(o, "executed" if o.filled >= o.qty else "cancelled", now)
        return True

    def _reconcile(self, status: Status, now: int, fills: list[Fill], collect: list[dict], hold: list[dict], warnings: list[str]) -> int:
        """Apply the status to the book. Returns GE slots held by offers we do not manage."""
        st = self.state
        active = {o.id: o for o in st.orders}
        by_key: dict[tuple[str, int], ExecOrder] = {}
        for o in st.orders:  # working orders win over pending ones for item+side matching
            k = (o.side, o.item_id)
            if k not in by_key or (by_key[k].state != "working" and o.state == "working"):
                by_key[k] = o
        done = {o.id: o for o in st.done}
        seen: set[int] = set()
        foreign = 0
        for r in status.orders:
            o = None
            if r.order_id is not None:
                o = active.get(r.order_id)
                if o is None and r.order_id in done:
                    d = done[r.order_id]
                    if d.outcome != "withdrawn" or r.status == "cancelled":
                        continue  # already closed and reported again: nothing to do
                    st.done.remove(d)  # an instruction we had withdrawn was placed after all
                    d.state, d.outcome, d.closed = "pending", "", 0
                    st.orders.append(d)
                    o = d
                elif o is None:
                    warnings.append(f"status mentions order #{r.order_id}, which this engine never issued: ignored")
                    continue
            elif r.side and r.item_id is not None:
                o = by_key.get((r.side, r.item_id))
            if o is None:
                if r.status in ("open", "filling"):
                    foreign += 1
                    hold.append({
                        "action": "HOLD", "item": self._name(r.item_id) if r.item_id is not None else "?", "item_id": r.item_id,
                        "side": r.side, "why": "not placed by this engine: left alone (it uses a GE slot)",
                    })
                continue
            if o.id in seen:
                continue
            seen.add(o.id)
            self._apply_report(o, r, now, fills, collect)

        for o in list(st.orders):
            if o.state != "working" or o.id in seen:
                continue
            if self._resolve_by_inventory(o, status, now, fills):
                continue
            if o.cancel_requested:
                self._close(o, "cancelled", now)  # aborted as instructed; last known fill stands
                continue
            if not o.missing_since:
                o.missing_since = now
            if now - o.missing_since >= self.cfg.missing_grace:
                warnings.append(f"order #{o.id} ({o.side} {o.name}) disappeared from the status without a final report: assumed cancelled at {o.filled:,}/{o.qty:,}")
                self._close(o, "cancelled", now)
            else:
                foreign += 1
                hold.append(self._line("HOLD", o, "missing from your status: report it once more as executed or cancelled (with filled)", needs_status=True))
        return foreign

    # -- decisions on orders already in the GE --------------------------------
    def _review(self, o: ExecOrder, lp: LatestPrice | None, now: int) -> tuple[str, str]:
        cfg = self.cfg
        if o.cancel_requested:
            return "CANCEL", "abort and collect (asked earlier, still open)"
        idle = now - o.last_progress
        progress = f"{o.filled:,}/{o.qty:,}" + (f", last fill {idle // 60}m ago" if o.filled else f", open {idle // 60}m")
        if o.side == "buy":
            fresh = lp is not None and lp.high is not None and lp.high_time is not None and now - lp.high_time <= SIGNAL_MAX_AGE
            if cfg.cancel_on_margin and fresh:
                margin = self._margin_now(o.price, o.name, lp, o.target)
                if margin < 1:
                    return "CANCEL", f"margin gone: instant-buy is now {lp.high:,}, your bid {o.price:,} would lose after tax"
            if idle >= cfg.buy_timeout:
                return "CANCEL", f"no fill for {idle // 60}m ({o.filled:,}/{o.qty:,}); free the slot and the coins"
            return "HOLD", f"buying {progress}"
        pos = self.state.positions.get(o.item_id)
        if pos is not None and lp is not None:
            age = now - (pos.listed_since or o.seen)
            if lp.low and age >= cfg.sell_dump and lp.low < o.price and (pos.stage != "dump" or idle >= cfg.dump_retry):
                pos.stage = "dump"
                return "CANCEL", f"unsold for {age // 60}m: relist at the instant-sell price {lp.low:,}"
            if pos.stage == "list" and idle >= cfg.sell_reprice and lp.high:
                new = lp.high - 1 if cfg.improve else lp.high
                if new < o.price:
                    pos.stage = "reprice"
                    return "CANCEL", f"no sale for {idle // 60}m and buyers now pay {lp.high:,}: relist at {new:,}"
        return "HOLD", f"selling {progress}"

    # -- new sell offers for stock we hold ------------------------------------
    def _plan_sells(self, market: Market, now: int, free: int, sell: list[dict], hold: list[dict]) -> int:
        st, cfg = self.state, self.cfg
        for item_id, pos in sorted(st.positions.items(), key=lambda kv: -kv[1].qty * kv[1].cost):
            mine = [o for o in st.orders if o.item_id == item_id]
            # A sell that is missing from the status is still treated as listed until its fate is known.
            working = [o for o in mine if o.side == "sell" and o.state == "working" and not o.cancel_requested]
            pending = next((o for o in mine if o.side == "sell" and o.state == "pending"), None)
            free_units = pos.qty - sum(o.remaining for o in working)
            reason = ""
            if free_units <= 0:
                reason = "-"
            elif working:
                reason = f"{free_units:,} more wait until sell order #{working[0].id} finishes"
            else:
                buy_leg = next((o for o in mine if o.side == "buy" and o.state == "working" and not o.cancel_requested), None)
                if buy_leg is not None and free_units * pos.cost < cfg.partial_sell_frac * buy_leg.qty * buy_leg.price:
                    reason = f"{free_units:,} bought so far; listing once buy order #{buy_leg.id} is further along"
                elif free <= 0:
                    reason = f"{free_units:,} ready to sell but no GE slot is free"
            if reason:
                if pending:
                    self._close(pending, "withdrawn", now)
                if reason != "-":
                    hold.append({"action": "HOLD", "item": pos.name, "item_id": item_id, "side": "inventory", "qty": free_units, "why": reason})
                continue
            lp = market.latest.get(item_id)
            if pending and pending.kind == pos.stage and now - pending.issued < cfg.pending_ttl:
                o = pending
                o.qty = free_units  # sell what you hold now
            else:
                if pending:
                    self._close(pending, "withdrawn", now)
                ask = self._ask(pos, lp, market.stats.get(item_id) if market.stats else None)
                o = self._new_order("sell", item_id, ask, free_units, now, kind=pos.stage)
            each = o.price - ge_tax(o.price, o.name) - pos.cost
            label = {"list": "list bought stock", "reprice": "relist at the market", "dump": "sell now at the instant-sell price"}[pos.stage]
            boxed = [b.id for b in mine if b.side == "buy" and b.state == "working" and b.filled]
            extra: dict[str, Any] = {"cost_each": round(pos.cost, 2), "profit_each": round(each, 2)}
            if boxed:
                extra["collect_first"] = boxed
            sell.append(self._line("SELL", o, f"{label}: cost {pos.cost:,.1f}, {each:+,.1f}/ea after tax", **extra))
            free -= 1
        return free

    # -- new buy offers ---------------------------------------------------------
    def _plan_buys(self, market: Market, now: int, free: int, cash: int, held: set[int], capacity: int, members: bool | None, buy: list[dict], equity: int) -> None:
        st, cfg = self.state, self.cfg
        working = {o.item_id for o in st.orders if o.state == "working"}
        for o in [o for o in st.orders if o.side == "buy" and o.state == "pending"]:
            lp = market.latest.get(o.item_id)
            keep = (
                free > 0
                and o.item_id not in working  # e.g. an earlier instruction for this item was placed after all
                and o.item_id not in st.positions
                and now - o.issued < cfg.pending_ttl
                and o.price * o.qty <= cash
                and self._margin_now(o.price, o.name, lp, o.target) >= 1
                and (o.item_id in held or len(held) < capacity)
            )
            if not keep:
                self._close(o, "withdrawn", now)
                continue
            each = o.target - ge_tax(o.target, o.name) - o.price
            buy.append(self._line("BUY", o, f"not in your status yet: place it as given (+{each:,}/ea at {o.target:,})", then_sell_at=o.target, profit_each=each))
            cash -= o.price * o.qty
            free -= 1
            held.add(o.item_id)
        # Loose change is not spread over dust orders: with less than `floor` to spend nothing
        # new is bought, and the per-order budget never falls below it. An order the market
        # itself keeps small (thin volume, buy limit) is not dust and is placed all the same.
        floor = max(int(cfg.min_order_frac * equity), 1)
        if free <= 0 or cash < floor:
            return
        spread = min(free, cash // floor)
        busy = set(st.positions) | {o.item_id for o in st.orders}
        base = cfg.volume_frac if cfg.volume_frac is not None else (cfg.buy_timeout / 3600) / cfg.fill_share
        scale, default = market.volume_scale, market.default_scale
        frac = (lambda i: base * scale.get(i, default)) if (scale or default != 1.0) else base
        flips = find_flips(
            self.items,
            market.latest,
            market.hour,
            budget=cash // spread,
            now=now,
            min_volume=cfg.min_volume,
            min_margin=cfg.min_margin,
            min_roi=cfg.min_roi,
            max_age_s=cfg.max_age,
            members=members,
            used=market.used,
            stats=market.stats,
            params=cfg.score,
            volume_frac=frac,
            require_history=market.stats is not None,
        )
        for f in flips:
            if free <= 0 or cash < floor:
                break  # what is left is loose change: it waits for a sale instead of becoming a dust order
            if f.score <= 0:
                break  # ranked by score: nothing below has any expected profit
            if f.item.id in busy:
                continue
            if f.item.id not in held and len(held) >= capacity:
                continue  # would need one more inventory stack than you have
            bid, target = f.buy, f.sell
            if cfg.improve:
                improved = (f.sell - 1) - ge_tax(f.sell - 1, f.item.name) - (f.buy + 1)
                if improved >= max(cfg.min_margin, 1):
                    bid, target = f.buy + 1, f.sell - 1
            target = capped_ask(target, bid, f.stats, f.item.name, cfg.margin_cap)
            each = target - ge_tax(target, f.item.name) - bid
            if each < max(cfg.min_margin, 1) or each < cfg.min_roi * bid:
                continue  # not worth it once the planned ask is held to the item's usual margin
            qty = min(f.quantity, cash // bid)
            if qty <= 0:
                continue
            o = self._new_order("buy", f.item.id, bid, qty, now, target=target)
            fill = f"{f.stats.p_fill * 100:.0f}%" if f.stats and f.stats.windows else "?"
            why = f"+{each:,}/ea selling at {target:,} ({each / bid * 100:.1f}%), fill chance {fill}, {f.liquidity:,} traded/h"
            buy.append(self._line("BUY", o, why, then_sell_at=target, profit_each=each))
            cash -= bid * qty
            free -= 1
            held.add(f.item.id)
            busy.add(f.item.id)

    # -- one cycle ----------------------------------------------------------------
    def step(self, status: Status, market: Market, now: int) -> Plan:
        st, cfg = self.state, self.cfg
        if status.cash is None and st.cash_anchor is None:
            raise StatusError('the first status needs "cash": the coins in your inventory')
        st.seq += 1
        collect: list[dict] = []
        cancel: list[dict] = []
        sell: list[dict] = []
        buy: list[dict] = []
        hold: list[dict] = []
        warnings: list[str] = []
        fills: list[Fill] = []

        # Account settings stick: a status that leaves them out keeps the last ones given.
        if status.members is not None:
            st.members = status.members
        if status.slots is not None:
            st.slots = status.slots
        if status.inventory_capacity is not None:
            st.inventory_capacity = status.inventory_capacity
        members = cfg.members
        if st.members is not None:
            members = None if st.members else False
        slots_total = st.slots or (min(cfg.slots, 3) if st.members is False else cfg.slots)
        capacity = st.inventory_capacity or cfg.inventory_slots

        occupied = self._reconcile(status, now, fills, collect, hold, warnings)

        # Coins are counted from the book: what you started with, plus every sale and minus
        # every purchase you reported, minus what is locked in buy offers. A "cash" figure is
        # a correction only when you change it - the same number again says nothing new.
        reserved = st.locked()
        correction = 0
        if status.cash is not None and (st.cash_anchor is None or status.cash != st.cash_given):
            if st.cash_anchor is not None:
                correction = status.cash - (st.cash_anchor + st.flow - reserved)
            st.cash_anchor = status.cash - st.flow + reserved
            st.cash_given = status.cash
        coins = st.cash_anchor + st.flow - reserved

        freed_cash = 0
        for o in list(st.orders):
            if o.state != "working" or o.missing_since:
                continue
            verdict, why = self._review(o, market.latest.get(o.item_id), now)
            if verdict == "CANCEL":
                if not o.cancel_requested:
                    o.cancel_requested = now
                cancel.append(self._line("CANCEL", o, why))
                if o.side == "buy":
                    freed_cash += o.price * o.remaining
            else:
                occupied += 1
                hold.append(self._line("HOLD", o, why))
        free = max(slots_total - occupied, 0)
        cash = max(coins, 0) + freed_cash

        held = {i for i, p in st.positions.items() if p.qty > 0}
        held |= {o.item_id for o in st.orders if o.side == "buy" and o.state == "working"}
        if status.inventory is not None:
            held |= set(status.inventory)
        if len(held) > capacity:
            warnings.append(f"{len(held)} item stacks held or incoming but capacity is {capacity}: no new items until some sell")

        stock = 0
        for i, p in st.positions.items():
            lp = market.latest.get(i)
            stock += p.qty * (lp.low if lp and lp.low else int(p.cost))
        equity = coins + reserved + stock
        if not st.start_ts:
            st.start_ts, st.start_equity = now, equity

        free = self._plan_sells(market, now, free, sell, hold)
        self._plan_buys(market, now, free, cash, held, capacity, members, buy, equity)

        actions = [{"step": n, **a} for n, a in enumerate(collect + cancel + sell + buy + hold, 1)]
        account = {
            "cash": coins,
            "cash_after_actions": coins + freed_cash - sum(a["price"] * a["qty"] for a in buy),
            "cash_correction": correction,
            "in_buy_offers": reserved,
            "stock_value": stock,
            "equity_est": equity,
            "realised": st.realised,
            "tax_paid": st.tax_paid,
            "sells": st.sells,
            "wins": st.wins,
            "ge_slots_used": occupied + len(sell) + len(buy),
            "ge_slots": slots_total,
            "inventory_stacks": len(held),
            "inventory_capacity": capacity,
            "items_allowed": "all" if members is None else ("free-to-play" if members is False else "members only"),
        }

        expect = []
        for o in st.orders:
            if o.state == "working" and o.cancel_requested:
                expect.append({"order_id": o.id, "item": o.name, "side": o.side, "status": "cancelled", "filled": o.filled})
            elif o.state == "working":
                expect.append({"order_id": o.id, "item": o.name, "side": o.side, "status": "filling" if o.filled else "open", "filled": o.filled})
            elif o.state == "pending":
                expect.append({"order_id": o.id, "item": o.name, "side": o.side, "status": "open", "filled": 0})
        # Settings and orders only: copying this over status.json can never feed back a stale coin count.
        reply = {"members": members is not False, "slots": slots_total, "inventory_capacity": capacity, "orders": expect}
        return Plan(st.seq, now, status.ts, actions, warnings, fills, account, reply)
