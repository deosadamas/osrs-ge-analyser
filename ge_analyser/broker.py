"""Paper broker: a simulated Grand Exchange account that carries out an execution plan
the way a person at the GE would, so the execution engine can be backtested unchanged.

GE mechanics modelled:
- a fixed number of offer slots shared by buy and sell offers;
- a buy offer reserves its coins when placed; aborting refunds the unspent part;
- bought items and sale proceeds wait in the offer until collected; the person clicks
  Collect once per cycle, before reporting status;
- inventory holds a limited number of item stacks (one per item type);
- 2% tax on sales; purchases are logged for the 4-hour buy limits.

Fill model - the unknown that the live loop exists to measure:
- a resting buy fills in a later 5m window whose instant-sell price is below the bid
  (`strict=False`: at or below);
- a resting sell fills when a later window's instant-buy price is above the ask, or
  when the instant-sell price is at/above it (the bid has reached the ask);
- each window fills at most 1/fill_share of that window's one-sided volume, at the
  offer's own price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .analysis import ItemStats, ge_tax
from .api import ItemInfo, LatestPrice
from .executor import ExecConfig, ExecState, Executor, Market, OrderReport, Plan, Status
from .sim import Snapshot, hour_from_history, snapshot_from_buckets

LIMIT_WINDOW_S = 4 * 3600


@dataclass
class BOffer:
    order_id: int
    side: str
    item_id: int
    price: int
    qty: int
    placed: int
    filled: int = 0
    collected: int = 0  # buy: units taken into inventory; sell: units whose coins were taken
    state: str = "open"  # open | executed | cancelled
    credit: float = 0.0  # fraction of a unit earned from crossing flow, not yet filled

    @property
    def remaining(self) -> int:
        return self.qty - self.filled


class PaperBroker:
    def __init__(self, items: dict[int, ItemInfo], cash: int, slots: int = 8, capacity: int = 26, fill_share: int = 10, strict: bool = True) -> None:
        self.items = items
        self.cash = cash
        self.slots = slots
        self.capacity = capacity
        self.fill_share = fill_share
        self.strict = strict
        self.offers: list[BOffer] = []
        self.inventory: dict[int, int] = {}
        self.buys: list[tuple[int, int, int]] = []  # (ts, item, units) for buy limits
        self.last_cost: dict[int, int] = {}
        self._final: list[OrderReport] = []  # orders that left the GE since the last status
        self.placed = {"buy": 0, "sell": 0, "cancel": 0}
        self.blocked = 0  # collections that did not fit in the inventory
        self.rejected = 0  # instructions that could not be carried out (no slot / coins / units)
        self.max_offers = 0
        self.max_stacks = 0

    # ---- helpers -------------------------------------------------------
    def _tax(self, item_id: int, price: int) -> int:
        item = self.items.get(item_id)
        return ge_tax(price, item.name if item else "")

    def _stack_ok(self, item_id: int) -> bool:
        return item_id in self.inventory or len(self.inventory) < self.capacity

    def _report(self, o: BOffer) -> OrderReport:
        status = o.state if o.state != "open" else ("filling" if o.filled else "open")
        return OrderReport(order_id=o.order_id, side=o.side, item_id=o.item_id, status=status, filled=o.filled, price=o.price, qty=o.qty)

    def _fill(self, o: BOffer, units: int, now: int) -> None:
        if units <= 0:
            return
        o.filled += units
        if o.side == "buy":
            self.buys.append((now, o.item_id, units))
            self.last_cost[o.item_id] = o.price
        if o.filled >= o.qty:
            o.state = "executed"

    def _take(self, o: BOffer, volume: int, now: int) -> None:
        """Give the offer its 1/fill_share of `volume` units of crossing flow. Shares
        below one unit accumulate, so a thin item fills as often as its flow justifies
        rather than at least one unit every window."""
        o.credit += volume / self.fill_share
        units = min(o.remaining, int(o.credit))
        o.credit -= units
        self._fill(o, units, now)

    # ---- market ----------------------------------------------------------
    def process_fills(self, snapshot: Snapshot, now: int) -> None:
        share = self.fill_share
        for o in self.offers:
            if o.state != "open" or o.remaining <= 0:
                continue
            t = snapshot.get(o.item_id)
            if t is None:
                continue
            volume = 0
            if o.side == "buy":
                if t.low is not None and t.low_time > o.placed and (t.low < o.price if self.strict else t.low <= o.price):
                    volume = t.low_volume
            else:
                if t.high is not None and t.high_time > o.placed and (t.high > o.price if self.strict else t.high >= o.price):
                    volume = t.high_volume
                if t.low is not None and t.low_time > o.placed and t.low >= o.price:
                    volume = max(volume, t.low_volume)
            if volume:
                self._take(o, volume, now)

    # ---- the person at the GE -------------------------------------------
    def collect_all(self) -> None:
        for o in list(self.offers):
            n = o.filled - o.collected
            if n > 0:
                if o.side == "sell":
                    self.cash += (o.price - self._tax(o.item_id, o.price)) * n
                    o.collected = o.filled
                elif self._stack_ok(o.item_id):
                    self.inventory[o.item_id] = self.inventory.get(o.item_id, 0) + n
                    o.collected = o.filled
                else:
                    self.blocked += 1
            if o.state != "open" and o.collected == o.filled:
                self.offers.remove(o)
                self._final.append(self._report(o))

    def status(self, now: int) -> Status:
        self.collect_all()
        reports = [self._report(o) for o in self.offers] + self._final
        self._final = []
        self.max_stacks = max(self.max_stacks, len(self.inventory))
        return Status(ts=now, cash=self.cash, orders=reports, inventory=dict(self.inventory), slots=self.slots, inventory_capacity=self.capacity)

    def apply(self, plan: Plan, snapshot: Snapshot, now: int) -> None:
        for a in plan.actions:
            kind = a["action"]
            oid = a.get("order_id")
            if kind == "CANCEL":
                o = next((x for x in self.offers if x.order_id == oid), None)
                if o is None or o.state != "open":
                    continue
                if o.side == "sell" and o.remaining and not self._stack_ok(o.item_id):
                    self.blocked += 1
                    continue
                o.state = "cancelled"
                self.placed["cancel"] += 1
                if o.side == "buy":
                    self.cash += o.price * o.remaining
                elif o.remaining:
                    self.inventory[o.item_id] = self.inventory.get(o.item_id, 0) + o.remaining
                self.collect_all()  # abort + collect: the slot is free for the rest of this batch
            elif kind == "SELL":
                item_id = a["item_id"]
                qty = min(a["qty"], self.inventory.get(item_id, 0))
                if qty <= 0 or len(self.offers) >= self.slots:
                    self.rejected += 1
                    continue
                self.inventory[item_id] -= qty
                if not self.inventory[item_id]:
                    del self.inventory[item_id]
                o = BOffer(oid, "sell", item_id, a["price"], qty, now)
                self.offers.append(o)
                self.placed["sell"] += 1
                t = snapshot.get(item_id)
                if t is not None and t.low is not None and t.low >= o.price:  # the bid is at/above the ask: sells at once
                    self._take(o, t.low_volume, now)
            elif kind == "BUY":
                price = a["price"]
                qty = min(a["qty"], self.cash // price)
                if qty <= 0 or len(self.offers) >= self.slots:
                    self.rejected += 1
                    continue
                self.cash -= price * qty
                self.offers.append(BOffer(oid, "buy", a["item_id"], price, qty, now))
                self.placed["buy"] += 1
        self.max_offers = max(self.max_offers, len(self.offers))

    # ---- bookkeeping -------------------------------------------------------
    def used(self, now: int) -> dict[int, int]:
        cutoff = now - LIMIT_WINDOW_S
        self.buys = [b for b in self.buys if b[0] > cutoff]
        out: dict[int, int] = {}
        for _, item_id, units in self.buys:
            out[item_id] = out.get(item_id, 0) + units
        return out

    def equity(self, latest: dict[int, LatestPrice]) -> int:
        def mark(item_id: int) -> int:
            lp = latest.get(item_id)
            return lp.low if lp and lp.low else self.last_cost.get(item_id, 0)

        total = self.cash
        for o in self.offers:
            if o.side == "buy":
                if o.state == "open":
                    total += o.price * o.remaining
                total += (o.filled - o.collected) * mark(o.item_id)
            else:
                total += (o.price - self._tax(o.item_id, o.price)) * (o.filled - o.collected)
                total += o.remaining * mark(o.item_id)
        for item_id, units in self.inventory.items():
            total += units * mark(item_id)
        return total


# phrase in a CANCEL's "why" -> short label, for backtest reports
CANCEL_KINDS = {"margin gone": "margin gone", "no fill": "buy timeout", "unsold for": "dump", "no sale": "reprice"}


@dataclass
class BacktestResult:
    start_cash: int
    equity: int
    steps: int  # cycles in which you were at the GE
    hours: float  # hours at the GE (steps x 5 min)
    span_hours: float  # length of the replay
    state: ExecState
    broker: PaperBroker
    pnl_by_item: dict[str, int]
    cancel_reasons: dict[str, int]
    coin_drift: int = 0  # largest gap between the engine's own coin count and the paper account's coins

    @property
    def ret(self) -> float:
        return self.equity / self.start_cash - 1

    @property
    def actions_per_hour(self) -> float:
        """Offers placed or aborted per hour at the GE."""
        return sum(self.broker.placed.values()) / self.hours if self.hours else 0.0

    @property
    def profit_per_hour(self) -> float:
        """gp gained per hour at the GE."""
        return (self.equity - self.start_cash) / self.hours if self.hours else 0.0


def run_backtest(
    windows: Iterable[tuple[int, dict[int, dict]]],
    items: dict[int, ItemInfo],
    cfg: ExecConfig,
    budget: int,
    start: int,
    stats_at: Callable[[int], dict[int, ItemStats]] | None = None,
    fill_share: int = 10,
    strict: bool = True,
    active: Callable[[int], bool] | None = None,
) -> BacktestResult:
    """Replay 5m windows through the execution engine and a paper broker. Windows before
    `start` only warm up the hourly volume; the same engine code runs as in the live loop.
    `active(ts)` says whether you are at the GE in that window; when you are not, offers
    keep filling but nothing is collected, cancelled or placed."""
    state = ExecState()
    engine = Executor(state, items, cfg)
    broker = PaperBroker(items, budget, slots=cfg.slots, capacity=cfg.inventory_slots, fill_share=fill_share, strict=strict)
    history: list[Snapshot] = []
    latest: dict[int, LatestPrice] = {}  # last print per item, carried forward like the wiki's /latest
    steps = first = last = drift = 0
    pnl: dict[str, int] = {}
    cancels: dict[str, int] = {}
    for ts, bucket in windows:
        snap = snapshot_from_buckets(bucket, ts)
        history.append(snap)
        del history[:-12]
        for item_id, t in snap.items():
            prev = latest.get(item_id)
            latest[item_id] = LatestPrice(
                high=t.high if t.high is not None else (prev.high if prev else None),
                high_time=ts if t.high is not None else (prev.high_time if prev else None),
                low=t.low if t.low is not None else (prev.low if prev else None),
                low_time=ts if t.low is not None else (prev.low_time if prev else None),
            )
        if ts < start:
            continue
        broker.process_fills(snap, ts)  # the market fills your offers whether you are there or not
        last = ts
        first = first or ts
        if active is not None and not active(ts):
            continue  # away from the GE: nothing is collected, cancelled or placed
        status = broker.status(ts)
        coins = status.cash
        if steps:
            status.cash = None  # as in the live loop: coins are stated once, then the engine counts them itself
        market = Market(latest=latest, hour=hour_from_history(history), stats=stats_at(ts) if stats_at else None, used=broker.used(ts))
        plan = engine.step(status, market, ts)
        drift = max(drift, abs(plan.account["cash"] - coins))
        for f in plan.fills:
            if f.side == "sell":
                pnl[f.name] = pnl.get(f.name, 0) + f.profit
        for a in plan.actions:
            if a["action"] == "CANCEL" and "asked earlier" not in a["why"]:
                reason = next((k for k in CANCEL_KINDS if k in a["why"]), "other")
                cancels[CANCEL_KINDS.get(reason, "other")] = cancels.get(CANCEL_KINDS.get(reason, "other"), 0) + 1
        broker.apply(plan, snap, ts)
        steps += 1
    span = (last - first) / 3600 + 1 / 12 if first else 0.0
    return BacktestResult(budget, broker.equity(latest), steps, steps / 12, span, state, broker, pnl, cancels, drift)
