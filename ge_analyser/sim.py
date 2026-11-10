"""Paper-trading engine: places simulated GE offers from the flip ranking and fills
them against later real price prints.

Fill model (deliberately conservative):
- A buy offer at price P fills on a later print whose instant-sell price (low) crosses
  below P (strict, default) or touches P (`--loose`). Sells mirror this on the
  instant-buy price. Strict models price-time priority: at a touched price you are
  queued behind everyone already offering there.
- Each print can fill at most 1/`fill_share` (default 1/10) of that window's one-sided
  volume, and fills happen at our own offer price, never the better print.
- Cash is reserved when a buy is placed (as the real GE does) and returned on cancel.
- GE tax is charged on sells. Buy limits and GE slot count are enforced.
- Unfilled buys are cancelled after `buy_timeout`. Unfilled sells are re-priced to the
  current instant-buy after `sell_reprice`, then dumped at the instant-sell after
  `sell_dump` so positions can't sit forever.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .analysis import DEFAULT_PARAMS, Flip, ItemStats, ScoreParams, find_flips, ge_tax
from .api import ItemInfo, LatestPrice, WindowPrice


@dataclass(frozen=True)
class Tick:
    """One market print for one item."""

    high: int | None
    high_time: int
    low: int | None
    low_time: int
    high_volume: int
    low_volume: int


Snapshot = dict[int, Tick]


@dataclass
class Order:
    id: int
    kind: str  # "buy" | "sell"
    item_id: int
    name: str
    price: int
    qty: int
    filled: int
    placed: int
    cost_basis: int = 0  # per-unit buy price, sells only
    repriced: bool = False

    @property
    def remaining(self) -> int:
        return self.qty - self.filled


@dataclass
class Trade:
    ts: int
    kind: str
    item_id: int
    name: str
    qty: int
    price: int
    tax: int
    profit: int  # realised per trade (sells only)


@dataclass
class Config:
    slots: int = 8
    buy_timeout: int = 30 * 60
    sell_reprice: int = 30 * 60
    sell_dump: int = 90 * 60
    min_volume: int = 50
    max_age: int = 3600
    members: bool | None = None
    fill_share: int = 10  # we capture 1/fill_share of a window's one-sided volume per print
    strict: bool = True  # fills need the market to cross our price, not just touch it
    min_margin: int = 2  # gp per unit after tax; 1-gp spreads are bot territory
    min_roi: float = 0.02
    improve: bool = True  # bid 1 gp above instant-sell / ask 1 gp below instant-buy: you become the best offer
    min_stake: int = 0  # gp per slot; fewer slots are used until equity supports this many (0 = off)
    score: ScoreParams = DEFAULT_PARAMS


@dataclass
class State:
    start_cash: int
    cash: int
    orders: list[Order] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    next_order_id: int = 1
    ticks: int = 0
    last_tick: int = 0
    log: list[str] = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "State":
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["orders"] = [Order(**o) for o in raw["orders"]]
        raw["trades"] = [Trade(**t) for t in raw["trades"]]
        return cls(**raw)

    # ---- derived -------------------------------------------------------
    @property
    def realised(self) -> int:
        return sum(t.profit for t in self.trades)

    def reserved(self) -> int:
        return sum(o.price * o.remaining for o in self.orders if o.kind == "buy")

    def holdings_value(self, snapshot: Snapshot) -> int:
        total = 0
        for o in self.orders:
            if o.kind != "sell":
                continue
            tick = snapshot.get(o.item_id)
            mark = tick.low if tick and tick.low else o.cost_basis
            total += mark * o.remaining
        return total

    def equity(self, snapshot: Snapshot) -> int:
        return self.cash + self.reserved() + self.holdings_value(snapshot)

    def open_slots(self, slots: int) -> int:
        return slots - len(self.orders)


def slots_for(cash_plus_positions: int, slots: int, min_stake: int) -> int:
    """Concentrate a small stack: never spread equity thinner than `min_stake` per slot."""
    if min_stake <= 0:
        return slots
    return max(1, min(slots, cash_plus_positions // min_stake))


class Simulator:
    def __init__(self, state: State, items: dict[int, ItemInfo], config: Config) -> None:
        self.state = state
        self.items = items
        self.cfg = config
        # Per-item history features for a given moment (time-of-week aware); None = legacy scoring.
        self.stats_at: Callable[[int], dict[int, ItemStats]] | None = None

    def usable_slots(self) -> int:
        # Equity proxy without a snapshot: cash + reserved + positions at cost.
        equity = self.state.cash + self.state.reserved() + sum(o.cost_basis * o.remaining for o in self.state.orders if o.kind == "sell")
        return slots_for(equity, self.cfg.slots, self.cfg.min_stake)

    # ---- helpers -------------------------------------------------------
    def _log(self, now: int, msg: str) -> None:
        self.state.log.append(f"{now}\t{msg}")
        del self.state.log[:-200]

    def _new_order(self, kind: str, item: ItemInfo, price: int, qty: int, now: int, cost_basis: int = 0) -> Order:
        o = Order(self.state.next_order_id, kind, item.id, item.name, price, qty, 0, now, cost_basis)
        self.state.next_order_id += 1
        self.state.orders.append(o)
        return o

    def used_limits(self, now: int) -> dict[int, int]:
        """Units bought per item in the trailing 4h, derived from the trade log."""
        used: dict[int, int] = {}
        for t in self.state.trades:
            if t.kind == "buy" and t.ts > now - 4 * 3600:
                used[t.item_id] = used.get(t.item_id, 0) + t.qty
        # Units sitting in open buy orders also count against the limit once filled;
        # reserve them so we never over-commit.
        for o in self.state.orders:
            if o.kind == "buy":
                used[o.item_id] = used.get(o.item_id, 0) + o.remaining
        return used

    # ---- fills ---------------------------------------------------------
    def process_fills(self, snapshot: Snapshot, now: int) -> None:
        for o in list(self.state.orders):
            tick = snapshot.get(o.item_id)
            if tick is None:
                continue
            if o.kind == "buy":
                self._fill_buy(o, tick, now)
            else:
                self._fill_sell(o, tick, now)

    def _fill_buy(self, o: Order, tick: Tick, now: int) -> None:
        if tick.low is None or tick.low_time <= o.placed:
            return
        if tick.low >= o.price if self.cfg.strict else tick.low > o.price:
            return
        qty = min(o.remaining, max(tick.low_volume // self.cfg.fill_share, 1))
        if qty <= 0:
            return
        o.filled += qty
        # Fill at our own bid; ignore the GE's occasional better-price refund (conservative).
        price = o.price
        self.state.trades.append(Trade(now, "buy", o.item_id, o.name, qty, price, 0, 0))
        ask = tick.high if tick.high and tick.high > price else price + 1
        if self.cfg.improve and ask - 1 > price:
            ask -= 1
        self._list_for_sale(self.items[o.item_id], qty, price, ask, now)
        self._log(now, f"BUY  {qty:,} x {o.name} @ {price:,} -> listing @ {ask:,}")
        if o.remaining == 0:
            self.state.orders.remove(o)

    def _list_for_sale(self, item: ItemInfo, qty: int, cost: int, ask: int, now: int) -> None:
        """Merge into the item's existing sell offer (one slot per item) or open a new one."""
        for o in self.state.orders:
            if o.kind == "sell" and o.item_id == item.id:
                total = o.remaining + qty
                o.cost_basis = (o.cost_basis * o.remaining + cost * qty) // total
                o.qty, o.filled = total, 0
                o.price, o.placed, o.repriced = ask, now, False
                return
        self._new_order("sell", item, ask, qty, now, cost_basis=cost)

    def _fill_sell(self, o: Order, tick: Tick, now: int) -> None:
        if tick.high is None or tick.high_time <= o.placed:
            return
        if tick.high <= o.price if self.cfg.strict else tick.high < o.price:
            return
        qty = min(o.remaining, max(tick.high_volume // self.cfg.fill_share, 1))
        if qty > 0:
            self._execute_sell(o, o.price, qty, now)  # fill at our own ask (conservative)

    def _execute_sell(self, o: Order, price: int, qty: int, now: int) -> None:
        tax = ge_tax(price, o.name)
        proceeds = (price - tax) * qty
        profit = proceeds - o.cost_basis * qty
        o.filled += qty
        self.state.cash += proceeds
        self.state.trades.append(Trade(now, "sell", o.item_id, o.name, qty, price, tax * qty, profit))
        self._log(now, f"SELL {qty:,} x {o.name} @ {price:,} (tax {tax * qty:,}) profit {profit:+,}")
        if o.remaining == 0:
            self.state.orders.remove(o)

    # ---- housekeeping --------------------------------------------------
    def expire(self, snapshot: Snapshot, now: int) -> None:
        for o in list(self.state.orders):
            age = now - o.placed
            if o.kind == "buy" and age >= self.cfg.buy_timeout:
                self.state.cash += o.price * o.remaining
                self.state.orders.remove(o)
                self._log(now, f"CANCEL buy {o.remaining:,} x {o.name} @ {o.price:,} (unfilled {age // 60}m)")
            elif o.kind == "sell":
                tick = snapshot.get(o.item_id)
                if tick is None:
                    continue
                if age >= self.cfg.sell_dump and tick.low and tick.low < o.price:
                    self._log(now, f"DUMP {o.name}: {o.remaining:,} @ instant-sell {tick.low:,} after {age // 60}m")
                    self._execute_sell(o, tick.low, o.remaining, now)
                elif not o.repriced and age >= self.cfg.sell_reprice and tick.high and tick.high < o.price:
                    self._log(now, f"REPRICE {o.name}: {o.price:,} -> {tick.high:,} after {age // 60}m")
                    o.price, o.repriced = tick.high, True

    # ---- decisions -----------------------------------------------------
    def decide(self, latest: dict[int, LatestPrice], hour: dict[int, WindowPrice], now: int) -> list[Order]:
        free = self.state.open_slots(self.usable_slots())
        if free <= 0 or self.state.cash <= 0:
            return []
        active = {o.item_id for o in self.state.orders}
        per_slot = self.state.cash // free
        flips = find_flips(
            self.items,
            latest,
            hour,
            budget=per_slot,
            now=now,
            min_volume=self.cfg.min_volume,
            min_margin=self.cfg.min_margin,
            min_roi=self.cfg.min_roi,
            max_age_s=self.cfg.max_age,
            members=self.cfg.members,
            stats=self.stats_at(now) if self.stats_at else None,
            params=self.cfg.score,
            used=self.used_limits(now),
        )
        placed: list[Order] = []
        for f in flips:
            if free == 0:
                break
            if f.item.id in active:
                continue
            bid, target = f.buy, f.sell
            if self.cfg.improve:
                # Jump the queue by 1 gp on each leg if the margin survives it.
                improved_margin = (f.sell - 1) - ge_tax(f.sell - 1, f.item.name) - (f.buy + 1)
                if improved_margin >= max(self.cfg.min_margin, 1):
                    bid, target = f.buy + 1, f.sell - 1
            qty = min(f.quantity, self.state.cash // bid)
            if qty <= 0:
                continue
            self.state.cash -= bid * qty
            placed.append(self._new_order("buy", f.item, bid, qty, now))
            active.add(f.item.id)
            free -= 1
            self._log(now, f"OFFER buy {qty:,} x {f.item.name} @ {bid:,} (target {target:,}, margin {target - ge_tax(target, f.item.name) - bid:,}/unit)")
        return placed

    # ---- one full step -------------------------------------------------
    def step(self, snapshot: Snapshot, latest: dict[int, LatestPrice], hour: dict[int, WindowPrice], now: int) -> None:
        self.process_fills(snapshot, now)
        self.expire(snapshot, now)
        self.process_fills(snapshot, now)  # dumps fill immediately on the same print
        self.decide(latest, hour, now)
        self.state.ticks += 1
        self.state.last_tick = now


# ---- snapshot construction ------------------------------------------------


def snapshot_from_live(latest: dict[int, LatestPrice], window: dict[int, WindowPrice]) -> Snapshot:
    out: Snapshot = {}
    for item_id, p in latest.items():
        w = window.get(item_id)
        out[item_id] = Tick(
            high=p.high,
            high_time=p.high_time or 0,
            low=p.low,
            low_time=p.low_time or 0,
            high_volume=w.high_volume if w else 0,
            low_volume=w.low_volume if w else 0,
        )
    return out


def snapshot_from_buckets(buckets: dict[int, dict], ts: int) -> Snapshot:
    """Build a snapshot from per-item 5m timeseries buckets for one timestamp."""
    out: Snapshot = {}
    for item_id, b in buckets.items():
        out[item_id] = Tick(
            high=b.get("avgHighPrice"),
            high_time=ts,
            low=b.get("avgLowPrice"),
            low_time=ts,
            high_volume=b.get("highPriceVolume") or 0,
            low_volume=b.get("lowPriceVolume") or 0,
        )
    return out


def latest_from_snapshot(snapshot: Snapshot) -> dict[int, LatestPrice]:
    return {i: LatestPrice(t.high, t.high_time, t.low, t.low_time) for i, t in snapshot.items()}


def hour_from_history(history: Iterable[Snapshot]) -> dict[int, WindowPrice]:
    """Aggregate several 5m snapshots (ideally 12) into an hourly window."""
    hv: dict[int, int] = {}
    lv: dict[int, int] = {}
    for snap in history:
        for i, t in snap.items():
            hv[i] = hv.get(i, 0) + t.high_volume
            lv[i] = lv.get(i, 0) + t.low_volume
    return {i: WindowPrice(None, hv[i], None, lv.get(i, 0)) for i in hv}


def replay(
    windows: Iterable[tuple[int, dict[int, dict]]],
    items: dict[int, ItemInfo],
    cfg: Config,
    budget: int,
    start: int,
    stats_at: Callable[[int], dict[int, ItemStats]] | None,
) -> tuple[State, Snapshot, int]:
    """Replay 5m windows (ts, raw rows) in time order. Windows before `start` only warm
    up the hourly-volume history; decisions begin at `start`."""
    state = State(start_cash=budget, cash=budget)
    sim = Simulator(state, items, cfg)
    sim.stats_at = stats_at
    history: list[Snapshot] = []
    snap: Snapshot = {}
    replayed = 0
    for ts, bucket in windows:
        snap = snapshot_from_buckets(bucket, ts)
        history.append(snap)
        del history[:-12]
        if ts < start:
            continue
        sim.step(snap, latest_from_snapshot(snap), hour_from_history(history), ts)
        replayed += 1
    return state, snap, replayed
