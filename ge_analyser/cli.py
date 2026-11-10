"""Command-line interface.

    ge-analyser flips --budget 10m --top 20
    ge-analyser item "Abyssal whip"
    ge-analyser dips --budget 10m --top 15
    ge-analyser simulate --budget 10k --backtest 24
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Callable
from datetime import datetime, timezone

from .analysis import (
    Flip,
    ItemStats,
    find_flips,
    fmt_gp,
    ge_tax,
    parse_gp,
    remaining_limit,
    trend_from_timeseries,
)
from .api import GEClient, ItemInfo
from .calibrate import calibrate, fifo_pnl, fill_stats, lookback_for_age
from .features import TimedStats, bucket_label, bucket_of, cached_timed_stats, compute_timed_stats, crossings
from .history import DEFAULT_DEPTH_DAYS, STEPS, HistoryStore
from .ledger import Ledger, Purchase
from . import runelite
from .sim import (
    Config,
    Simulator,
    Snapshot,
    State,
    hour_from_history,
    latest_from_snapshot,
    snapshot_from_buckets,
    snapshot_from_live,
)


def _table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(row: list[str]) -> str:
        return "  ".join(c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(row))

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows]
    return "\n".join(lines)


def _age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"


def _print_flips(flips: list[Flip]) -> None:
    with_stats = any(f.stats for f in flips)
    rows = []
    for f in flips:
        row = [
            f.item.name,
            fmt_gp(f.buy),
            fmt_gp(f.sell),
            fmt_gp(f.margin),
            f"{f.roi * 100:.1f}%",
            (f"{f.limit_left:,}/{f.item.limit:,}" if f.limit_left != f.item.limit else f"{f.item.limit:,}") if f.item.limit else "-",
            f"{f.hourly_volume:,}",
            f"{f.quantity:,}",
            fmt_gp(f.profit),
        ]
        if with_stats:
            s = f.stats
            if s is None or s.windows == 0:
                row += ["?", "?", "?", fmt_gp(f.expected_profit)]
            else:
                row += [f"{s.p_fill * 100:.0f}%", fmt_gp(s.median_margin), f"{s.volatility_pct:.1f}%", fmt_gp(f.expected_profit)]
        row.append(_age(f.price_age_s))
        rows.append(row)
    headers = ["Item", "Buy", "Sell", "Margin", "ROI", "Limit", "Vol/h", "Qty", "Profit"]
    if with_stats:
        headers += ["Fill", "Typ", "Vol", "Expect"]
    headers.append("Age")
    print(_table(headers, rows, right=set(range(1, len(headers)))))


def _find_item(items: dict[int, ItemInfo], query: str) -> ItemInfo:
    if query.isdigit() and int(query) in items:
        return items[int(query)]
    q = query.lower()
    exact = [i for i in items.values() if i.name.lower() == q]
    if exact:
        return exact[0]
    partial = sorted((i for i in items.values() if q in i.name.lower()), key=lambda i: len(i.name))
    if not partial:
        sys.exit(f"No item matching '{query}'")
    if len(partial) > 1:
        print(f"Ambiguous; using '{partial[0].name}'. Other matches: " + ", ".join(i.name for i in partial[1:6]), file=sys.stderr)
    return partial[0]


def _members_filter(args: argparse.Namespace) -> bool | None:
    if args.f2p:
        return False
    if args.members:
        return True
    return None


def _used(args: argparse.Namespace, ledger: Ledger, now: int) -> dict[int, int]:
    return {} if args.ignore_ledger else ledger.used(now)


def _top_up(store: HistoryStore, client: GEClient, days: float) -> int:
    missing = len(store.missing(int(time.time()) - int(days * 86400), int(time.time()) - 300))
    if missing > 30:
        print(f"Fetching {missing} 5-minute windows of history (one-off; later runs top up incrementally)...", file=sys.stderr)
    return store.update(client, days)


def _timed_stats(args: argparse.Namespace, client: GEClient, items: dict[int, ItemInfo], now: int) -> TimedStats | None:
    if args.no_stats:
        return None
    store = HistoryStore()
    _top_up(store, client, args.history_days)
    return cached_timed_stats(store, {i: it.name for i, it in items.items()}, args.history_days, now)


def _stats(args: argparse.Namespace, client: GEClient, items: dict[int, ItemInfo], now: int) -> dict[int, ItemStats] | None:
    timed = _timed_stats(args, client, items, now)
    return timed.at(now) if timed else None


def cmd_flips(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    now = int(time.time())
    items = client.mapping()
    latest = client.latest()
    hour = client.hourly()
    flips = find_flips(
        items,
        latest,
        hour,
        budget=parse_gp(args.budget),
        now=now,
        min_volume=args.min_volume,
        min_margin=args.min_margin,
        min_roi=args.min_roi / 100,
        max_age_s=args.max_age * 60,
        members=_members_filter(args),
        used=_used(args, ledger, now),
        stats=_stats(args, client, items, now),
    )
    if not flips:
        ages = [now - min(p.high_time, p.low_time) for p in latest.values() if p.high_time and p.low_time]
        freshest = min(ages) if ages else None
        print("No flips matched the filters.")
        if freshest is not None and freshest > args.max_age * 60:
            print(f"Newest price in the feed is {_age(freshest)} old (> --max-age {args.max_age}m); the wiki feed may be lagging. Try --max-age {max(freshest // 60 + 30, 60)}.")
        return
    print(f"Top {min(args.top, len(flips))} of {len(flips)} candidate flips (budget {fmt_gp(parse_gp(args.budget))})\n")
    _print_flips(flips[: args.top])
    print("\nBuy = place buy offer at this price; Sell = place sell offer at this price. Margin is after 2% GE tax.")
    print("Limit = units still buyable this 4h window; record purchases with `ge-analyser bought <item> <qty>`.")
    if any(f.stats for f in flips):
        print("Fill = P(both legs cross within 30 min) from history; Typ = typical after-tax margin; Vol = 5m volatility; Expect = Typ x Qty x Fill.")


def cmd_bought(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    now = int(time.time())
    items = client.mapping()
    if args.clear:
        item = _find_item(items, args.query) if args.query else None
        n = ledger.clear(item.id if item else None)
        print(f"Cleared {n} purchase record(s){f' for {item.name}' if item else ''}.")
        return
    if args.query and args.qty is not None:
        if args.qty <= 0:
            sys.exit("qty must be positive")
        item = _find_item(items, args.query)
        ledger.record(item.id, args.qty, now)
        used = ledger.used(now)[item.id]
        left = remaining_limit(item, used)
        if item.limit:
            print(f"Recorded {args.qty:,} x {item.name}. {used:,}/{item.limit:,} used, {left:,} left; resets in {_age(ledger.resets_at(item.id, now) - now)}.")
        else:
            print(f"Recorded {args.qty:,} x {item.name} (buy limit unknown).")
        return
    if args.query or args.qty is not None:
        sys.exit("usage: ge-analyser bought <item> <qty>   |   ge-analyser bought [--clear [item]]")

    used = ledger.used(now)
    if not used:
        print("No purchases in the current 4h window.")
        return
    rows = []
    for item_id, qty in sorted(used.items(), key=lambda kv: items[kv[0]].name if kv[0] in items else ""):
        item = items.get(item_id)
        name = item.name if item else f"id {item_id}"
        limit = item.limit if item else 0
        reset = ledger.resets_at(item_id, now) or now
        rows.append([name, f"{qty:,}", f"{limit:,}" if limit else "-", f"{remaining_limit(item, qty):,}" if item and limit else "-", _age(max(reset - now, 0))])
    print(_table(["Item", "Bought", "Limit", "Left", "Resets in"], rows, right={1, 2, 3, 4}))


def cmd_item(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    items = client.mapping()
    item = _find_item(items, args.query)
    latest = client.latest().get(item.id)
    hour = client.hourly().get(item.id)
    now = int(time.time())

    print(f"{item.name} (id {item.id}){' [members]' if item.members else ''}")
    used = ledger.used(now).get(item.id, 0)
    if item.limit and used:
        reset = ledger.resets_at(item.id, now)
        left = remaining_limit(item, used)
        print(f"  Buy limit: {left}/{item.limit} left (bought {used}, resets in {_age(max(reset - now, 0))})   High alch: {fmt_gp(item.highalch)}")
    else:
        print(f"  Buy limit: {item.limit or 'unknown'}   High alch: {fmt_gp(item.highalch)}")
    if latest and latest.high and latest.low:
        tax = ge_tax(latest.high, item.name)
        margin = latest.high - tax - latest.low
        print(f"  Instant-buy:  {fmt_gp(latest.high):>10}  ({_age(now - (latest.high_time or now))} ago)")
        print(f"  Instant-sell: {fmt_gp(latest.low):>10}  ({_age(now - (latest.low_time or now))} ago)")
        print(f"  Flip margin:  {fmt_gp(margin):>10}  ({margin / latest.low * 100:.2f}% ROI, tax {fmt_gp(tax)})")
    if hour:
        print(f"  Last hour: {hour.high_volume:,} bought @ avg {fmt_gp(hour.avg_high or 0)}, {hour.low_volume:,} sold @ avg {fmt_gp(hour.avg_low or 0)}")

    timed = _timed_stats(args, client, items, now)
    if timed and item.id in timed.overall:
        st = timed.overall[item.id]
        print(
            f"  History ({args.history_days:g}d, {st.windows} windows): fill P(buy) {st.fill_buy * 100:.0f}%  P(sell) {st.fill_sell * 100:.0f}%  "
            f"typical margin {fmt_gp(st.median_margin)}  5m volatility {st.volatility_pct:.2f}%  typical volume {st.typical_volume:,}/h"
        )
        b = bucket_of(now)
        bs = timed.buckets.get(b, {}).get(item.id)
        if bs:
            print(
                f"  Now ({bucket_label(b)}, {bs.windows} windows): fill P(buy) {bs.fill_buy * 100:.0f}%  P(sell) {bs.fill_sell * 100:.0f}%  "
                f"typical margin {fmt_gp(bs.median_margin)}  typical volume {bs.typical_volume:,}/h"
            )

    for lookback, label in (("24h", "24h (5m candles)"), ("30d", "30 days (1h candles)"), ("6m", "6 months (6h candles)")):
        trend = trend_from_timeseries(client.timeseries(item.id, lookback))
        if trend is None:
            continue
        signal = "BUY (below average)" if trend.z_score < -1 else "SELL (above average)" if trend.z_score > 1 else "neutral"
        print(f"  Trend {label}: mean {fmt_gp(trend.mean)}, now {fmt_gp(trend.latest)}, z={trend.z_score:+.2f}, change {trend.change_pct:+.1f}%  -> {signal}")


def cmd_dips(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    """Among liquid items, find those currently priced well below their 24h average."""
    now = int(time.time())
    items = client.mapping()
    latest = client.latest()
    hour = client.hourly()
    budget = parse_gp(args.budget)
    members = _members_filter(args)

    candidates = []
    for item_id, h in hour.items():
        item = items.get(item_id)
        lp = latest.get(item_id)
        if item is None or lp is None or lp.low is None or lp.low > budget:
            continue
        if members is not None and item.members != members:
            continue
        if min(h.high_volume, h.low_volume) < args.min_volume:
            continue
        candidates.append((h.high_volume + h.low_volume, item, lp))
    candidates.sort(key=lambda c: c[0], reverse=True)
    candidates = candidates[: args.scan]

    rows = []
    for _, item, lp in candidates:
        trend = trend_from_timeseries(client.timeseries(item.id, "24h"))
        if trend is None or trend.z_score > -args.z:
            continue
        upside = trend.mean - lp.low
        tax = ge_tax(int(trend.mean), item.name)
        rows.append((trend.z_score, item, lp.low, trend, upside - tax))
    rows.sort(key=lambda r: r[0])
    if not rows:
        print("No dips found; relax --z or widen --scan.")
        return
    print(f"Items trading below their 24h mean (scanned {len(candidates)} liquid items, z <= -{args.z})\n")
    print(
        _table(
            ["Item", "Now", "24h mean", "z", "24h chg", "Rebound/unit (after tax)"],
            [
                [item.name, fmt_gp(low), fmt_gp(t.mean), f"{z:+.2f}", f"{t.change_pct:+.1f}%", fmt_gp(up)]
                for z, item, low, t, up in rows[: args.top]
            ],
            right={1, 2, 3, 4, 5},
        )
    )
    print("\nDips are mean-reversion bets: check the drop isn't caused by a game update before buying.")


def _sim_path() -> Path:
    override = os.environ.get("GE_ANALYSER_SIM")
    return Path(override) if override else Path.home() / ".osrs-ge-analyser" / "sim.json"


def _print_sim(state: State, snapshot: Snapshot, now: int, tail: int = 15) -> None:
    equity = state.equity(snapshot)
    pnl = equity - state.start_cash
    sells = [t for t in state.trades if t.kind == "sell"]
    wins = sum(1 for t in sells if t.profit > 0)
    print(f"Tick {state.ticks}  cash {fmt_gp(state.cash)}  reserved {fmt_gp(state.reserved())}  holdings {fmt_gp(state.holdings_value(snapshot))}")
    print(f"Equity {fmt_gp(equity)} ({pnl:+,} gp, {pnl / state.start_cash * 100:+.1f}%)   realised {state.realised:+,}   sells {len(sells)} (win {wins})")
    if state.orders:
        rows = []
        for o in state.orders:
            tick = snapshot.get(o.item_id)
            mkt = (tick.low if o.kind == "buy" else tick.high) if tick else None
            rows.append([o.kind, o.name, f"{o.remaining:,}", fmt_gp(o.price), fmt_gp(mkt) if mkt else "-", fmt_gp(o.cost_basis) if o.kind == "sell" else "", _age(max(now - o.placed, 0))])
        print("\nOpen offers:")
        print(_table(["Side", "Item", "Qty", "Price", "Market", "Cost", "Age"], rows, right={2, 3, 4, 5, 6}))
    if state.log:
        print(f"\nLast {min(tail, len(state.log))} events:")
        for line in state.log[-tail:]:
            ts, msg = line.split("\t", 1)
            print(f"  {time.strftime('%m-%d %H:%M', time.localtime(int(ts)))}  {msg}")


def _sim_config(args: argparse.Namespace) -> Config:
    members = _members_filter(args)
    slots = args.slots if args.slots is not None else (3 if members is False else 8)
    return Config(
        slots=slots,
        min_volume=args.min_volume,
        max_age=args.max_age * 60,
        members=members,
        fill_share=args.fill_share,
        strict=not args.loose,
        min_margin=args.min_margin,
        min_roi=args.min_roi / 100,
    )


def cmd_simulate(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    path = _sim_path()
    if args.reset:
        if path.exists():
            path.unlink()
        print("Simulation state deleted.")
        return
    items = client.mapping()
    cfg = _sim_config(args)

    if args.backtest:
        _backtest(args, client, items, cfg)
        return

    state = State.load(path) if path.exists() else State(start_cash=parse_gp(args.budget), cash=parse_gp(args.budget))
    sim = Simulator(state, items, cfg)
    if args.status:
        snap = snapshot_from_live(client.latest(), client.window("5m"))
        _print_sim(state, snap, int(time.time()))
        return

    n = 0
    while args.ticks == 0 or n < args.ticks:
        now = int(time.time())
        latest, five, hour = client.latest(), client.window("5m"), client.hourly()
        snap = snapshot_from_live(latest, five)
        timed = _timed_stats(args, client, items, now)
        sim.stats_at = timed.at if timed else None
        sim.step(snap, latest, hour, now)
        state.save(path)
        print(f"--- {time.strftime('%H:%M:%S', time.localtime(now))} ---")
        _print_sim(state, snap, now, tail=8)
        n += 1
        if args.ticks == 0 or n < args.ticks:
            time.sleep(args.interval)
    print(f"\nState saved to {path}. Resume with `ge-analyser simulate --ticks N`, inspect with --status.")


def _run_backtest(
    store: HistoryStore,
    items: dict[int, ItemInfo],
    cfg: Config,
    budget: int,
    start: int,
    end: int,
    stats_at: Callable[[int], dict[int, ItemStats]] | None,
) -> tuple[State, Snapshot, int]:
    state = State(start_cash=budget, cash=budget)
    sim = Simulator(state, items, cfg)
    sim.stats_at = stats_at
    history: list[Snapshot] = []
    snap: Snapshot = {}
    replayed = 0
    for ts, bucket in store.windows(start - 3600, end + 1):
        snap = snapshot_from_buckets(bucket, ts)
        history.append(snap)
        del history[:-12]
        if ts < start:
            continue  # warm-up: populates hourly volume before the first decision
        sim.step(snap, latest_from_snapshot(snap), hour_from_history(history), ts)
        replayed += 1
    return state, snap, replayed


def _backtest(args: argparse.Namespace, client: GEClient, items: dict[int, ItemInfo], cfg: Config) -> None:
    budget = parse_gp(args.budget)
    store = HistoryStore()
    need_days = args.backtest / 24 + args.history_days + 0.1
    _top_up(store, client, need_days)
    _, last, _ = store.coverage()
    if last is None:
        sys.exit("No history available.")
    end = last
    start = end - int(args.backtest * 3600)
    names = {i: it.name for i, it in items.items()}
    # Features come only from windows *before* the replay starts: no look-ahead.
    timed = None if args.no_stats else compute_timed_stats(store, names, start - int(args.history_days * 86400), start)
    cfg.max_age = 10 * 60  # buckets are always fresh relative to their own timestamp
    span = f"{time.strftime('%m-%d %H:%M', time.localtime(start))} to {time.strftime('%m-%d %H:%M', time.localtime(end))}"
    print(f"Backtest: {args.backtest:g}h of 5m history ({span}), all items, start {fmt_gp(budget)}, {cfg.slots} slots")
    if timed is not None:
        bucketed = sum(len(d) for d in timed.buckets.values())
        print(f"Scoring with history features for {len(timed.overall)} items, {bucketed} item-buckets by time of week ({args.history_days:g} days before the replay)")

    if args.compare:
        variants = [("time-of-week features", timed.at if timed else None), ("flat features", timed.flat_at if timed else None), ("legacy (no history)", None)]
        rows = []
        for label, provider in variants:
            if timed is None and provider is None and label != "legacy (no history)":
                continue
            state, snap, _ = _run_backtest(store, items, cfg, budget, start, end, provider)
            eq = state.equity(snap)
            sells = [t for t in state.trades if t.kind == "sell"]
            rows.append([label, fmt_gp(eq), f"{eq - budget:+,}", f"{(eq - budget) / budget * 100:+.1f}%", str(len(sells)), str(sum(1 for t in sells if t.profit > 0))])
        print()
        print(_table(["Scoring", "Equity", "P&L", "Return", "Sells", "Wins"], rows, right={1, 2, 3, 4, 5}))
        return

    state, snap, replayed = _run_backtest(store, items, cfg, budget, start, end, timed.at if timed else None)
    print(f"Replayed {replayed} buckets\n")
    _print_sim(state, snap, end, tail=25)
    sells = [t for t in state.trades if t.kind == "sell"]
    if sells:
        by_item: dict[str, int] = {}
        for t in sells:
            by_item[t.name] = by_item.get(t.name, 0) + t.profit
        ranked = sorted(by_item.items(), key=lambda kv: kv[1], reverse=True)
        print("\nRealised P&L by item:")
        print(_table(["Item", "Profit"], [[n, f"{p:+,}"] for n, p in ranked], right={1}))


def cmd_history(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    store = HistoryStore()
    t0 = time.time()

    def progress(tag):
        return lambda d, n: print(f"  {tag}: {d}/{n} ({time.time() - t0:.0f}s)", file=sys.stderr, flush=True) if d % 500 == 0 or d == n else None

    for step, days in (("5m", args.days_5m), ("1h", args.days_1h)):
        missing = len(store.missing(int(time.time()) - int(days * 86400), int(time.time()) - STEPS[step], step))
        if missing:
            print(f"Fetching {missing} {step} windows...", file=sys.stderr)
        store.update(client, days, step=step, rate=args.rate, progress=progress(step))
    if not args.no_daily:
        ids = sorted(client.mapping())
        store.update_daily(client, ids, rate=args.rate, progress=progress("daily"))
    for step in ("5m", "1h"):
        first, last, n = store.coverage(step)
        if first is None:
            print(f"{step}: empty")
            continue
        span = f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(first))} .. {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}"
        print(f"{step}: {n:,} windows, {span}")
    n, first = store.daily_coverage()
    print(f"daily: {n:,} item-days" + (f" since {time.strftime('%Y-%m-%d', time.localtime(first))}" if first else ""))
    print(f"Store: {store.path} ({store.path.stat().st_size / 1e6:.0f} MB)")


DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _heat(values: dict[tuple[int, int], float]) -> str:
    """24 x 7 table of values scaled to % of the max."""
    top = max(values.values(), default=0) or 1
    rows = []
    for hour in range(24):
        cells = [f"{values.get((hour, d), 0) / top * 100:3.0f}" for d in range(7)]
        avg = sum(values.get((hour, d), 0) for d in range(7)) / 7 / top * 100
        rows.append([f"{hour:02d}:00", *cells, f"{avg:3.0f}"])
    return _table(["UTC", *DOW, "avg"], rows, right=set(range(1, 9)))


def cmd_peaks(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    store = HistoryStore()
    now = int(time.time())
    local_off = (datetime.now() - datetime.utcnow()).total_seconds() / 3600
    if not args.query:
        days = args.days or 90
        first, _, _ = store.coverage("1h")
        if first is None:
            sys.exit("No hourly history yet; run `ge-analyser history` first.")
        since = now - int(days * 86400)
        vol: dict[tuple[int, int], float] = {}
        for ts, hvol, lvol in store.db.execute("SELECT ts, hvol, lvol FROM w1h WHERE ts >= ?", (since,)):
            d = datetime.fromtimestamp(ts, timezone.utc)
            key = (d.hour, d.weekday())
            vol[key] = vol.get(key, 0) + min(hvol, lvol)
        occ: dict[tuple[int, int], int] = {}
        for (ts,) in store.db.execute("SELECT DISTINCT ts FROM w1h WHERE ts >= ?", (since,)):
            d = datetime.fromtimestamp(ts, timezone.utc)
            occ[(d.hour, d.weekday())] = occ.get((d.hour, d.weekday()), 0) + 1
        mean = {k: v / occ.get(k, 1) for k, v in vol.items()}
        print(f"Market-wide two-sided volume (min of buys/sells, summed over items), % of the busiest hour, last {days:g} days. Local time = UTC{local_off:+.0f}h.\n")
        print(_heat(mean))
        best = sorted(mean.items(), key=lambda kv: kv[1], reverse=True)[:5]
        worst = sorted(mean.items(), key=lambda kv: kv[1])[:5]
        print("\nBusiest: " + ", ".join(f"{DOW[d]} {h:02d}:00" for (h, d), _ in best))
        print("Quietest: " + ", ".join(f"{DOW[d]} {h:02d}:00" for (h, d), _ in worst))
        return

    items = client.mapping()
    item = _find_item(items, args.query)
    days = args.days or 30
    rows = list(store.db.execute("SELECT ts, high, hvol, low, lvol FROM w5 WHERE item=? AND ts >= ? ORDER BY ts", (item.id, now - int(days * 86400))))
    ws = crossings(rows)
    if len(ws) < 48:
        sys.exit(f"Only {len(ws)} two-sided 5m windows for {item.name} in {days:g} days; not enough to say anything.")
    by_hour: dict[int, list] = {}
    for w in ws:
        by_hour.setdefault(datetime.fromtimestamp(w.ts, timezone.utc).hour, []).append(w)
    print(f"{item.name}: by hour of day (UTC; local = UTC{local_off:+.0f}h), last {days:g} days, {len(ws)} two-sided windows\n")
    out = []
    for hour in range(24):
        hws = by_hour.get(hour, [])
        judged = [w for w in hws if w.judged]
        if not hws:
            out.append([f"{hour:02d}:00", "0", "-", "-", "-", "-"])
            continue
        margins = [w.high - ge_tax(w.high, item.name) - w.low for w in hws]
        out.append(
            [
                f"{hour:02d}:00",
                str(len(hws)),
                f"{int(statistics.median(w.hourly_volume for w in hws)):,}",
                fmt_gp(int(statistics.median(margins))),
                f"{sum(w.buy_hit for w in judged) / len(judged) * 100:.0f}%" if judged else "-",
                f"{sum(w.sell_hit for w in judged) / len(judged) * 100:.0f}%" if judged else "-",
            ]
        )
    print(_table(["UTC", "Windows", "Vol/h", "Margin", "P(buy)", "P(sell)"], out, right={1, 2, 3, 4, 5}))
    print("\nVol/h = median one-sided units per hour; Margin = median after-tax spread; P = chance a bid/ask at the instant price is crossed within 30 min.")
    wk = [w for w in ws if datetime.fromtimestamp(w.ts, timezone.utc).weekday() >= 5]
    wd = [w for w in ws if datetime.fromtimestamp(w.ts, timezone.utc).weekday() < 5]
    if wk and wd:
        print(f"Weekend vs weekday median Vol/h: {int(statistics.median(w.hourly_volume for w in wk)):,} vs {int(statistics.median(w.hourly_volume for w in wd)):,}")


def _fmt_secs(s: float | None) -> str:
    if s is None:
        return "-"
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.1f}m" if s < 5400 else f"{s / 3600:.1f}h"


def cmd_sync(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    now = int(time.time())
    paths: list[Path] = []
    for p in args.paths:
        path = Path(p).expanduser()
        if path.is_dir():
            paths.extend(runelite.account_files(path))
        elif path.is_file():
            paths.append(path)
        else:
            sys.exit(f"Not found: {path}")
    if not args.paths:
        paths = runelite.account_files()
    if not paths:
        sys.exit(
            f"No Flipping Utilities data in {runelite.default_dir()}. Install 'Flipping Utilities' from the RuneLite "
            "Plugin Hub, do some GE trades, then re-run; or pass an account .json path explicitly."
        )
    trades = runelite.load_trades(paths)
    accounts = sorted({t.account for t in trades})
    print(f"Loaded {len(trades)} trade events from {len(paths)} file(s): {', '.join(accounts) or '-'}")
    if not trades:
        return

    # 1. Buy-limit ledger: every buy event (complete, partial or cancelled-after-partial-fill)
    #    counts the units it actually bought against the 4h limit.
    if not args.no_ledger:
        total = 0
        for account in accounts:
            rows = [Purchase(t.item_id, t.qty, t.ts, f"runelite:{account}") for t in trades if t.account == account and t.buy]
            total += ledger.replace_source(f"runelite:{account}", rows, now)
        print(f"Ledger: {total} purchase(s) inside the current 4h window imported (manual entries kept).")

    # 2. Report window.
    since = now - int(args.days * 86400)
    window = [t for t in trades if t.ts >= since]
    completed = [t for t in window if t.state in ("BOUGHT", "SOLD")]
    print(f"\nLast {args.days:g} day(s): {len(window)} events, {len(completed)} completed offers")

    pnl = fifo_pnl(window)
    rows = sorted(pnl.values(), key=lambda p: p.realised, reverse=True)
    if rows:
        print(
            _table(
                ["Item", "Bought", "Sold", "Spent", "Received", "Tax", "Realised", "Unsold"],
                [
                    [p.name, f"{p.bought:,}", f"{p.sold:,}", fmt_gp(p.spent), fmt_gp(p.received), fmt_gp(p.tax), f"{p.realised:+,}", f"{p.unmatched_qty:,}"]
                    for p in rows
                ],
                right={1, 2, 3, 4, 5, 6, 7},
            )
        )
        print(f"Total realised: {sum(p.realised for p in rows):+,} gp   tax paid: {fmt_gp(sum(p.tax for p in rows))}")

    fs = fill_stats(window)
    print(
        f"\nFills: {fs.completed_buys} buys (median {_fmt_secs(fs.median(fs.buy_fill_seconds))}), "
        f"{fs.completed_sells} sells (median {_fmt_secs(fs.median(fs.sell_fill_seconds))}); "
        f"cancelled {fs.cancelled_buys} buys / {fs.cancelled_sells} sells"
        + (f", cancelled offers were {statistics.mean(fs.cancelled_fill_ratio) * 100:.0f}% filled on average" if fs.cancelled_fill_ratio else "")
    )

    # 3. Calibration against the wiki print.
    if args.calibrate:
        items = sorted({(t.item_id, lookback_for_age(now - t.ts)) for t in completed})
        distinct = {i for i, _ in items}
        if len(distinct) > args.max_items:
            keep = set(sorted(distinct, key=lambda i: -sum(t.qty * t.price for t in completed if t.item_id == i))[: args.max_items])
            items = [(i, s) for i, s in items if i in keep]
        history = {(i, s): client.timeseries(i, s) for i, s in items}
        c = calibrate(completed, history, now)
        print(f"\nCalibration: {c.classified} fills classified, {c.unknown} without a wiki print")
        if c.classified:
            print(f"  needed the market to cross your price: {c.strict_fills} ({c.strict_share * 100:.0f}%)   at/above the print: {c.loose_fills}")
            print(f"  median edge vs print: {statistics.median(c.edge_pct):+.2f}%  (positive = better than the instant price)")
        print(f"  -> {c.suggestion()}")




def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ge-analyser", description="OSRS Grand Exchange flip & trend analyser")
    p.add_argument("--no-cache", action="store_true", help="bypass the local response cache")
    p.add_argument("--ignore-ledger", action="store_true", help="assume fresh buy limits, ignoring recorded purchases")
    p.add_argument("--no-stats", action="store_true", help="legacy scoring: ignore the local price history")
    p.add_argument("--history-days", type=float, default=14, help="days of 5m history used for per-item fill/margin/volatility stats and time-of-week buckets (default 14)")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--budget", default="10m", help="cash available, e.g. 500k, 10m, 1b (default 10m)")
        sp.add_argument("--top", type=int, default=20, help="rows to show")
        sp.add_argument("--min-volume", type=int, default=50, help="min two-sided units traded in the last hour")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--f2p", action="store_true", help="free-to-play items only")
        g.add_argument("--members", action="store_true", help="members items only")

    f = sub.add_parser("flips", help="rank instant-buy/instant-sell margin flips")
    common(f)
    f.add_argument("--min-margin", type=int, default=1, help="min per-unit profit after tax")
    f.add_argument("--min-roi", type=float, default=0.0, help="min ROI percent, e.g. 2 for 2%%")
    f.add_argument("--max-age", type=int, default=60, help="ignore prices older than N minutes")
    f.set_defaults(func=cmd_flips)

    i = sub.add_parser("item", help="detail + trend for one item (name or id)")
    i.add_argument("query")
    i.set_defaults(func=cmd_item)

    b = sub.add_parser("bought", help="record a purchase so remaining buy limits are tracked")
    b.add_argument("query", nargs="?", help="item name or id")
    b.add_argument("qty", nargs="?", type=int, help="units bought")
    b.add_argument("--clear", action="store_true", help="forget recorded purchases (all, or for the given item)")
    b.set_defaults(func=cmd_bought)

    d = sub.add_parser("dips", help="liquid items priced below their 24h average")
    common(d)
    d.add_argument("--scan", type=int, default=60, help="how many of the most liquid items to fetch history for")
    d.add_argument("--z", type=float, default=1.0, help="min standard deviations below the mean")
    d.set_defaults(func=cmd_dips)

    s = sub.add_parser("simulate", help="paper-trading agent: places and fills simulated offers from the flip ranking")
    s.add_argument("--budget", default="10k", help="starting cash for a new simulation (default 10k)")
    s.add_argument("--slots", type=int, default=None, help="GE slots (default 8, or 3 with --f2p)")
    s.add_argument("--min-volume", type=int, default=50, help="min two-sided units traded per hour")
    s.add_argument("--max-age", type=int, default=60, help="ignore prices older than N minutes (live mode)")
    s.add_argument("--fill-share", type=int, default=10, help="assume we capture 1/N of each window's volume (higher = more conservative)")
    s.add_argument("--loose", action="store_true", help="fill when the market touches our price instead of crossing it (optimistic)")
    s.add_argument("--min-margin", type=int, default=2, help="min per-unit profit after tax to enter a flip")
    s.add_argument("--min-roi", type=float, default=2.0, help="min ROI percent to enter a flip")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--f2p", action="store_true", help="free-to-play items only")
    g.add_argument("--members", action="store_true", help="members items only")
    s.add_argument("--interval", type=int, default=300, help="seconds between live ticks")
    s.add_argument("--ticks", type=int, default=1, help="live ticks to run (0 = forever)")
    s.add_argument("--backtest", type=float, metavar="HOURS", help="replay the last N hours of 5m history instead of trading live")
    s.add_argument("--compare", action="store_true", help="backtest: run with and without history features and compare")
    s.add_argument("--status", action="store_true", help="show the live simulation portfolio and exit")
    s.add_argument("--reset", action="store_true", help="delete the live simulation state")
    s.set_defaults(func=cmd_simulate)

    h = sub.add_parser("history", help="backfill / top up the local market history (5m, 1h and daily, all items)")
    h.add_argument("--days-5m", type=float, default=DEFAULT_DEPTH_DAYS["5m"], help="5-minute windows to keep (default 30 days)")
    h.add_argument("--days-1h", type=float, default=DEFAULT_DEPTH_DAYS["1h"], help="hourly windows to keep (default 365 days)")
    h.add_argument("--no-daily", action="store_true", help="skip the 1-year daily series per item")
    h.add_argument("--rate", type=float, default=6.0, help="max requests per second to the wiki (default 6)")
    h.set_defaults(func=cmd_history)

    k = sub.add_parser("peaks", help="when does the market (or one item) trade: volume, spread and fill rate by hour of day and weekday")
    k.add_argument("query", nargs="?", help="item name or id; omit for the whole market")
    k.add_argument("--days", type=float, default=None, help="history window (default 90 for market from 1h data, 30 for an item from 5m data)")
    k.set_defaults(func=cmd_peaks)

    y = sub.add_parser("sync", help="import real trades from RuneLite Flipping Utilities: buy limits, P&L, fill times, model calibration")
    y.add_argument("paths", nargs="*", help="account .json files or directories (default ~/.runelite/flipping)")
    y.add_argument("--days", type=float, default=7, help="report window in days (default 7)")
    y.add_argument("--no-ledger", action="store_true", help="report only; don't update the buy-limit ledger")
    y.add_argument("--calibrate", action="store_true", help="fetch wiki history for each completed trade and classify fills (network heavy)")
    y.add_argument("--max-items", type=int, default=40, help="calibrate: cap on distinct items to fetch history for")
    y.set_defaults(func=cmd_sync)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    client = GEClient(cache_ttl=0 if args.no_cache else 60)
    args.func(args, client, Ledger())


if __name__ == "__main__":
    main()
