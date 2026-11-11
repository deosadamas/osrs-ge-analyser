"""Command-line interface.

    ge-analyser flips --budget 10m --top 20
    ge-analyser item "Abyssal whip"
    ge-analyser dips --budget 10m --top 15
    ge-analyser simulate --budget 10k --backtest 24
    ge-analyser execute --init --cash 250k && ge-analyser execute
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .analysis import (
    MARGIN_CAP,
    NOT_FLIPPABLE,
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
from .broker import run_backtest
from .calibrate import FillReality, calibrate, fifo_pnl, fill_reality, fill_stats, lookback_for_age, model_fill, sizing_scales
from .executor import ExecConfig, ExecState, Executor, Market, StatusError, parse_status
from .features import TimedStats, bucket_label, bucket_of, cached_timed_stats, compute_timed_stats, crossings
from .history import DEFAULT_DEPTH_DAYS, STEPS, HistoryStore
from .ledger import Ledger, Purchase
from . import runelite
from .regime import detect
from . import tune
from .sim import (
    Config,
    Simulator,
    Snapshot,
    State,
    replay,
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
        if item.name in NOT_FLIPPABLE:
            print("  Not flippable: bought on the GE it arrives untradeable, and converting it back costs 10% of its price.")
        else:
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

    regime = detect(HistoryStore().daily_series(item.id))
    if regime:
        verdict = "structural re-level, do not expect reversion" if regime.structural else "no regime change"
        print(f"  60d regime: last 7d mean {fmt_gp(regime.recent_mean)} vs prior 60d {fmt_gp(regime.prior_mean)} ({regime.change_pct:+.1f}%, {regime.persistent_days}/7 days beyond 15%) -> {verdict}")

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

    store = HistoryStore()
    rows = []
    skipped = 0
    for _, item, lp in candidates:
        trend = trend_from_timeseries(client.timeseries(item.id, "24h"))
        if trend is None or trend.z_score > -args.z:
            continue
        regime = detect(store.daily_series(item.id))
        if regime and regime.structural and not args.include_shifts:
            skipped += 1
            continue
        upside = trend.mean - lp.low
        tax = ge_tax(int(trend.mean), item.name)
        rows.append((trend.z_score, item, lp.low, trend, upside - tax, regime))
    rows.sort(key=lambda r: r[0])
    if not rows:
        print("No dips found; relax --z or widen --scan." + (f" ({skipped} hidden as structural shifts; --include-shifts to see them)" if skipped else ""))
        return
    print(f"Items trading below their 24h mean (scanned {len(candidates)} liquid items, z <= -{args.z}" + (f", {skipped} hidden as structural shifts" if skipped else "") + ")\n")
    print(
        _table(
            ["Item", "Now", "24h mean", "z", "24h chg", "Rebound/unit (after tax)", "60d regime"],
            [
                [item.name, fmt_gp(low), fmt_gp(t.mean), f"{z:+.2f}", f"{t.change_pct:+.1f}%", fmt_gp(up), r.label() if r else "?"]
                for z, item, low, t, up, r in rows[: args.top]
            ],
            right={1, 2, 3, 4, 5},
        )
    )
    print("\nDips are mean-reversion bets. '60d regime' compares the last week with the prior two months; 'shift' means the")
    print("price has re-levelled (likely a game update) and the 24h mean is not a target. Those rows are hidden by default.")


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
        improve=not args.no_improve,
        min_stake=parse_gp(args.min_stake),
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

    events_file = None
    if args.events:
        events_path = Path(args.events).expanduser()
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_file = events_path.open("a", encoding="utf-8")
        sim.events = lambda ev: (events_file.write(json.dumps(ev) + "\n"), events_file.flush())

    n = 0
    failures = 0
    while args.ticks == 0 or n < args.ticks:
        now = int(time.time())
        try:
            latest, five, hour = client.latest(), client.window("5m"), client.hourly()
            snap = snapshot_from_live(latest, five)
            timed = _timed_stats(args, client, items, now)
        except (OSError, ValueError, KeyError) as e:
            # Wiki hiccup (closed connection, timeout, bad JSON): skip this tick, keep state.
            failures += 1
            wait = min(args.interval * failures, 600)
            print(f"--- {time.strftime('%H:%M:%S', time.localtime(now))} fetch failed ({type(e).__name__}: {e}); retrying in {wait}s", flush=True)
            time.sleep(wait)
            continue
        failures = 0
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
    # One hour of warm-up windows populates hourly volume before the first decision.
    return replay(store.windows(start - 3600, end + 1), items, cfg, budget, start, stats_at)


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


def advice_path() -> Path:
    return Path.home() / ".osrs-ge-analyser" / "advice.json"


def _agent_payload(state: State, items: dict[int, ItemInfo], ledger: Ledger, now: int, snap: Snapshot) -> dict:
    """Mirror of the agent's open orders: what to have placed in the GE right now."""
    rows = []
    for o in state.orders:
        item = items.get(o.item_id)
        tick = snap.get(o.item_id)
        if o.kind == "buy":
            margin = o.target - ge_tax(o.target, o.name) - o.price if o.target else 0
            rows.append({
                "id": o.item_id, "name": o.name, "side": "buy", "buy": o.price, "sell": o.target, "margin": margin,
                "qty": o.remaining, "filled": o.filled, "limit": item.limit if item else 0,
                "limit_left": remaining_limit(item, ledger.used(now).get(o.item_id, 0)) if item else 0,
                "market": tick.low if tick else None, "age": now - o.placed, "expect": margin * o.remaining,
            })
        else:
            margin = o.price - ge_tax(o.price, o.name) - o.cost_basis
            rows.append({
                "id": o.item_id, "name": o.name, "side": "sell", "buy": o.cost_basis, "sell": o.price, "margin": margin,
                "qty": o.remaining, "filled": o.filled, "limit": item.limit if item else 0, "limit_left": 0,
                "market": tick.high if tick else None, "age": now - o.placed, "expect": margin * o.remaining,
            })
    rows.sort(key=lambda r: (r["side"] != "sell", -r["expect"]))  # sells first: they free slots
    events = []
    for line in state.log[-20:]:
        ts, msg = line.split("\t", 1)
        events.append({"t": int(ts), "msg": msg})
    return {
        "ts": now,
        "budget": state.equity(snap),
        "agent": {"equity": state.equity(snap), "cash": state.cash, "realised": state.realised, "ticks": state.ticks, "last_tick": state.last_tick},
        "flips": rows,
        "events": events,
        "limits": {},
    }


def cmd_advise(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    """Write the current top flips (or the agent's open orders) and remaining limits as JSON for the RuneLite plugin."""
    out = Path(args.out).expanduser() if args.out else advice_path()
    n = 0
    while True:
        now = int(time.time())
        try:
            items = client.mapping()
            latest = client.latest()
            if args.from_agent:
                sim_path = _sim_path()
                if not sim_path.exists():
                    sys.exit(f"No agent state at {sim_path}; start `ge-analyser simulate --ticks 0` first.")
                state = State.load(sim_path)
                payload = _agent_payload(state, items, ledger, now, snapshot_from_live(latest, client.window("5m")))
                flips = payload["flips"]
            else:
                hour = client.hourly()
                stats = _stats(args, client, items, now)
                used = _used(args, ledger, now)
                flips = find_flips(
                    items, latest, hour, budget=parse_gp(args.budget), now=now,
                    min_volume=args.min_volume, min_margin=args.min_margin, min_roi=args.min_roi / 100,
                    max_age_s=args.max_age * 60, members=_members_filter(args), used=used,
                    stats=stats,
                )[: args.top]
                payload = {
                    "ts": now,
                    "budget": parse_gp(args.budget),
                    "flips": [
                        {
                            "id": f.item.id, "name": f.item.name, "side": "flip",
                            "buy": f.buy + (1 if args.improve and f.margin > 2 else 0),
                            "sell": f.sell - (1 if args.improve and f.margin > 2 else 0),
                            "margin": f.margin - (2 if args.improve and f.margin > 2 else 0),
                            "roi": round(f.roi, 4), "qty": f.quantity, "limit": f.item.limit, "limit_left": f.limit_left,
                            "volume": f.liquidity, "fill": round(f.stats.p_fill, 3) if f.stats else None,
                            "expect": f.expected_profit, "age": f.price_age_s,
                        }
                        for f in flips
                    ],
                    "limits": {
                        str(i): {"used": q, "limit": items[i].limit, "resets": ledger.resets_at(i, now)}
                        for i, q in used.items() if i in items
                    },
                }
        except (OSError, ValueError, KeyError) as e:
            print(f"{time.strftime('%H:%M:%S')} fetch failed ({type(e).__name__}: {e}); retrying in {args.interval}s", flush=True)
            time.sleep(args.interval)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(out)
        n += 1
        print(f"{time.strftime('%H:%M:%S')} wrote {len(flips)} flips to {out}", flush=True)
        if args.ticks and n >= args.ticks:
            break
        time.sleep(args.interval)


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
        if runelite.plugin_log_path().exists():
            paths.append(runelite.plugin_log_path())
    if not paths:
        sys.exit(
            f"No trade data: neither {runelite.plugin_log_path()} (GE Analyser RuneLite plugin, see runelite-plugin/) nor "
            f"{runelite.default_dir()} (Flipping Utilities from the Plugin Hub). Trade with one of them installed, then re-run."
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

    a = sub.add_parser("advise", help="publish the current top flips + your remaining limits as JSON for the RuneLite plugin")
    common(a)
    a.add_argument("--min-margin", type=int, default=2, help="min per-unit profit after tax")
    a.add_argument("--min-roi", type=float, default=2.0, help="min ROI percent")
    a.add_argument("--max-age", type=int, default=60, help="ignore prices older than N minutes")
    a.add_argument("--no-improve", dest="improve", action="store_false", help="quote exact instant prices instead of 1 gp better")
    a.add_argument("--interval", type=int, default=60, help="seconds between refreshes (default 60)")
    a.add_argument("--ticks", type=int, default=0, help="refreshes to run (0 = forever)")
    a.add_argument("--out", help="output file (default ~/.osrs-ge-analyser/advice.json)")
    a.add_argument("--from-agent", action="store_true", help="mirror the live agent's open orders (GE_ANALYSER_SIM) instead of the generic ranking")
    a.set_defaults(func=cmd_advise)

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
    d.add_argument("--include-shifts", action="store_true", help="also show items whose price has structurally re-levelled in the last week")
    d.set_defaults(func=cmd_dips)

    def agent_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--budget", default="10k", help="starting cash (default 10k)")
        sp.add_argument("--slots", type=int, default=None, help="GE slots (default 8, or 3 with --f2p)")
        sp.add_argument("--min-volume", type=int, default=50, help="min two-sided units traded per hour")
        sp.add_argument("--max-age", type=int, default=60, help="ignore prices older than N minutes (live mode)")
        sp.add_argument("--fill-share", type=int, default=10, help="assume we capture 1/N of each window's volume (higher = more conservative)")
        sp.add_argument("--loose", action="store_true", help="fill when the market touches our price instead of crossing it (optimistic)")
        sp.add_argument("--min-margin", type=int, default=2, help="min per-unit profit after tax to enter a flip")
        sp.add_argument("--min-roi", type=float, default=2.0, help="min ROI percent to enter a flip")
        sp.add_argument("--no-improve", action="store_true", help="bid/ask exactly at the instant prices instead of 1 gp better (4x worse in backtests)")
        sp.add_argument("--min-stake", default="0", help="gp per slot; use fewer slots until equity supports this many, e.g. 2500 (default off)")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--f2p", action="store_true", help="free-to-play items only")
        g.add_argument("--members", action="store_true", help="members items only")

    s = sub.add_parser("simulate", help="paper-trading agent: places and fills simulated offers from the flip ranking")
    agent_args(s)
    s.add_argument("--interval", type=int, default=300, help="seconds between live ticks")
    s.add_argument("--ticks", type=int, default=1, help="live ticks to run (0 = forever)")
    s.add_argument("--backtest", type=float, metavar="HOURS", help="replay the last N hours of 5m history instead of trading live")
    s.add_argument("--compare", action="store_true", help="backtest: run with and without history features and compare")
    s.add_argument("--status", action="store_true", help="show the live simulation portfolio and exit")
    s.add_argument("--reset", action="store_true", help="delete the live simulation state")
    s.add_argument("--events", metavar="PATH", help="append one JSON line per decision (offer/fill/cancel/reprice/dump) to this file")
    s.set_defaults(func=cmd_simulate)

    x = sub.add_parser("execute", help="execution loop: reads your order status from status.json, writes what to do next (BUY / SELL / HOLD / CANCEL / COLLECT) to orders.json")
    agent_args(x)
    x.set_defaults(min_volume=200)
    x.add_argument("--dir", help="folder with status.json / orders.json / state.json (default ~/.osrs-ge-analyser/exec)")
    x.add_argument("--init", action="store_true", help="start a session: write a first status.json")
    x.add_argument("--cash", help="coins for --init, e.g. 250k (default: --budget)")
    x.add_argument("--reset", action="store_true", help="with --init: discard the existing session")
    x.add_argument("--once", action="store_true", help="answer the current status once and exit")
    x.add_argument("--interval", type=int, default=60, help="seconds between answers when status.json has not changed (default 60)")
    x.add_argument("--inventory-slots", type=int, default=26, help="item stacks you can hold; collect as notes so each item is one stack (default 26; a value in status.json wins)")
    x.add_argument("--max-status-age", type=int, default=300, help="warn when status.json is older than this many seconds (default 300)")
    x.add_argument("--margin-cap", type=float, default=MARGIN_CAP, help="plan asks at most this multiple of an item's typical margin over cost; 0 = off (default 2)")
    x.add_argument("--no-margin-cancel", action="store_true", help="keep a buy offer even when the market has moved so it would lose after tax")
    x.add_argument("--volume-frac", type=float, default=None, help="size each order to this share of hourly volume (default: buy timeout / fill share = 0.05)")
    x.add_argument("--backtest", type=float, metavar="HOURS", help="replay the last N hours through the same engine with a paper broker")
    x.add_argument("--compare", action="store_true", help="backtest: show how the result depends on the share of volume you get, the inventory limit and the old simulator")
    x.add_argument("--play", metavar="START-END", help="backtest: local hours you are at the GE each day, e.g. 19-23 (default: around the clock)")
    x.add_argument("--report", action="store_true", help="session so far: profit, orders, stock, real fills vs the model")
    x.set_defaults(func=cmd_execute)

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

    u = sub.add_parser("tune", help="walk-forward sweep of the scoring constants over several past windows")
    agent_args(u)
    u.add_argument("--folds", type=int, default=4, help="non-overlapping test windows, newest first (default 4)")
    u.add_argument("--hours", type=float, default=72, help="length of each test window (default 72)")
    u.set_defaults(func=cmd_tune)

    y = sub.add_parser("sync", help="import real trades from RuneLite Flipping Utilities: buy limits, P&L, fill times, model calibration")
    y.add_argument("paths", nargs="*", help="account .json files or directories (default ~/.runelite/flipping)")
    y.add_argument("--days", type=float, default=7, help="report window in days (default 7)")
    y.add_argument("--no-ledger", action="store_true", help="report only; don't update the buy-limit ledger")
    y.add_argument("--calibrate", action="store_true", help="fetch wiki history for each completed trade and classify fills (network heavy)")
    y.add_argument("--max-items", type=int, default=40, help="calibrate: cap on distinct items to fetch history for")
    y.set_defaults(func=cmd_sync)
    return p


def cmd_tune(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    store = HistoryStore()
    need_days = (args.folds * args.hours) / 24 + args.history_days + 0.2
    _top_up(store, client, need_days)
    items = client.mapping()
    cfg = _sim_config(args)
    cfg.max_age = 10 * 60
    cands = tune.grid()
    budget = parse_gp(args.budget)
    print(f"Walk-forward: {args.folds} folds x {args.hours:g}h, features from the {args.history_days:g} days before each fold, {len(cands)} candidates, start {fmt_gp(budget)}")
    t0 = time.time()
    results = tune.run(
        store, items, cfg, budget, args.folds, args.hours, args.history_days, cands,
        progress=lambda f, n, r: print(f"  fold {f} {n:<16} {r * 100:+8.1f}%   ({time.time() - t0:.0f}s)", file=sys.stderr, flush=True),
    )
    rows, best = tune.summarise(results, cands)
    print()
    print(
        _table(
            ["Candidate", "Older folds (median)", "Rank", "Newest fold"],
            [[n, f"{tr * 100:+.1f}%", str(rk), f"{te * 100:+.1f}%"] for n, tr, te, rk in rows],
            right={1, 2, 3},
        )
    )
    print("\nPer fold:")
    print(
        _table(
            ["Fold", "Window", *[c.name for c in cands]],
            [
                [str(r.fold), f"{time.strftime('%m-%d %H:%M', time.localtime(r.start))} .. {time.strftime('%m-%d %H:%M', time.localtime(r.end))}", *[f"{r.returns[c.name] * 100:+.0f}%" for c in cands]]
                for r in results
            ],
            right=set(range(2, 2 + len(cands))),
        )
    )
    chosen = next(c for c in cands if c.name == best)
    if best == "default":
        print("\nRecommendation: keep the defaults; no candidate beats them out of sample.")
    else:
        print(f"\nRecommendation: {chosen.describe()} — best on the older folds and better than default on the newest one.")
        print("Apply by editing ScoreParams defaults in ge_analyser/analysis.py (and SHRINK_WINDOWS in features.py).")


# ---- execution loop: you trade, the files talk -------------------------------

HOW_TO_REPLY = (
    "Press Collect at the GE, do the actions top to bottom, then rewrite status.json: for every order still in the GE "
    "(or just finished) its order_id, status (open | filling | executed | cancelled) and filled. "
    "reply_template is that file pre-filled; edit status and filled. Coins are counted for you from what you report: "
    'add "cash": <your coins> only when account.cash is not what you hold.'
)


def _exec_dir(args: argparse.Namespace) -> Path:
    if args.dir:
        return Path(args.dir).expanduser()
    override = os.environ.get("GE_ANALYSER_EXEC")
    return Path(override) if override else Path.home() / ".osrs-ge-analyser" / "exec"


def _exec_config(args: argparse.Namespace) -> ExecConfig:
    members = _members_filter(args)
    return ExecConfig(
        slots=args.slots if args.slots is not None else (3 if members is False else 8),
        inventory_slots=args.inventory_slots,
        min_volume=args.min_volume,
        max_age=args.max_age * 60,
        members=members,
        min_margin=args.min_margin,
        min_roi=args.min_roi / 100,
        improve=not args.no_improve,
        fill_share=args.fill_share,
        volume_frac=args.volume_frac,
        cancel_on_margin=not args.no_margin_cancel,
        margin_cap=args.margin_cap,
    )


def _read_json(path: Path):
    """JSON written by hand or by PowerShell: tolerate a UTF-8 BOM and UTF-16."""
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return json.loads(data.decode("utf-16"))
    return json.loads(data.decode("utf-8-sig"))


def _write_json(path: Path, obj, indent: int | None = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=indent), encoding="utf-8")
    tmp.replace(path)


def _print_plan(out: dict) -> None:
    a = out["account"]
    after = f" (-> {a['cash_after_actions']:,} after these actions)" if a["cash_after_actions"] != a["cash"] else ""
    print(
        f"--- {out['time'][11:]}  #{out['seq']}  coins {a['cash']:,}{after}  equity ~{fmt_gp(a['equity_est'])}  realised {a['realised']:+,}  "
        f"GE slots {a['ge_slots_used']}/{a['ge_slots']}  stacks {a['inventory_stacks']}/{a['inventory_capacity']}  ({a['items_allowed']}) ---"
    )
    if a["cash_correction"]:
        print(f"  coins corrected by {a['cash_correction']:+,}: the engine had counted {a['cash'] - a['cash_correction']:,}")
    rows = []
    for x in out["actions"]:
        rows.append([
            str(x["step"]),
            x["action"],
            f"#{x['order_id']}" if x.get("order_id") else "",
            str(x.get("item", "")),
            f"{x['qty']:,}" if x.get("qty") is not None else "",
            f"{x['price']:,}" if x.get("price") is not None else "",
            x.get("why", ""),
        ])
    if rows:
        print(_table(["#", "Do", "Order", "Item", "Qty", "Price", "Why"], rows, right={0, 4, 5}))
    else:
        print("Nothing to do: no order, no stock, no flip that passes the filters.")
    for w in out["warnings"]:
        print(f"  ! {w}")
    sys.stdout.flush()


def _exec_calibration(args: argparse.Namespace, state: ExecState, cfg: ExecConfig) -> FillReality | None:
    """Compare finished real orders with what the paper fill model gives for the same
    order over the same windows. Needs the local 5m history."""
    if args.no_stats:
        return None
    store = HistoryStore()
    _, last, _ = store.coverage()
    if last is None:
        return None
    for o in state.done:
        if o.model_fill is None and o.seen and o.outcome in ("executed", "cancelled") and o.closed <= last + 300:
            rows = store.db.execute(
                "SELECT ts, high, hvol, low, lvol FROM w5 WHERE item=? AND ts > ? AND ts < ? ORDER BY ts", (o.item_id, o.seen - 300, o.closed)
            ).fetchall()
            o.model_fill = model_fill(rows, o.side, o.price, o.qty, o.seen, o.closed, cfg.fill_share)
    return fill_reality(state.done, side="buy")


def _exec_cycle(args: argparse.Namespace, client: GEClient, ledger: Ledger, engine: Executor, items: dict[int, ItemInfo], d: Path) -> None:
    now = int(time.time())
    status_path, orders_path, state_path = d / "status.json", d / "orders.json", d / "state.json"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    def fail(message: str) -> None:
        _write_json(orders_path, {"time": stamp, "ts": now, "error": message, "actions": []})
        print(f"--- {stamp[11:]}  {message}", flush=True)

    if not status_path.exists():
        return fail(f"no {status_path}: run `ge-analyser execute --init --cash <coins>` or write the file yourself")
    try:
        status = parse_status(_read_json(status_path), items, int(status_path.stat().st_mtime))
    except (json.JSONDecodeError, UnicodeDecodeError, StatusError) as e:
        return fail(f"status.json not understood: {e}")
    try:
        latest, hour = client.latest(), client.hourly()
        timed = _timed_stats(args, client, items, now)
    except (OSError, ValueError, KeyError) as e:
        print(f"--- {stamp[11:]}  market data fetch failed ({type(e).__name__}: {e}); orders.json left as it was", flush=True)
        return
    reality = _exec_calibration(args, engine.state, engine.cfg)
    default_scale, item_scales = sizing_scales(reality) if reality else (1.0, {})
    market = Market(latest=latest, hour=hour, stats=timed.at(now) if timed else None, used=ledger.used(now), volume_scale=item_scales, default_scale=default_scale)
    try:
        plan = engine.step(status, market, now)
    except StatusError as e:
        return fail(f"status.json not understood: {e}")
    for f in plan.fills:
        if f.side == "buy":
            ledger.record(f.item_id, f.qty, now, source="exec")
    engine.state.save(state_path)
    out = plan.to_json()
    out = {"time": stamp, **out}
    if out["status_age_s"] > args.max_status_age:
        out["warnings"].insert(0, f"status.json is {out['status_age_s'] // 60} min old: update it before acting on these orders")
    out["calibration"] = {
        "finished_buy_orders_measured": reality.orders if reality else 0,
        "real_fill_vs_model": round(reality.ratio, 2) if reality and reality.ratio is not None else None,
        "order_size_scale": round(default_scale, 2),
    }
    out["how_to_reply"] = HOW_TO_REPLY
    _write_json(orders_path, out)
    _print_plan(out)


def _exec_report(d: Path) -> None:
    state_path = d / "state.json"
    if not state_path.exists():
        sys.exit(f"No session in {d}. Start one with `ge-analyser execute --init --cash <coins>`.")
    st = ExecState.load(state_path)
    hours = (time.time() - st.start_ts) / 3600 if st.start_ts else 0
    print(f"Session in {d}: {st.seq} cycles over {hours:.1f}h, started with ~{fmt_gp(st.start_equity)}")
    print(f"Realised {st.realised:+,} gp after {fmt_gp(st.tax_paid)} tax; {st.sells} sell fills, {st.wins} profitable")
    if st.cash_anchor is not None:
        locked = st.locked()
        print(f"Coins by the engine's count: {st.cash_anchor + st.flow - locked:,}, plus {locked:,} locked in buy offers")
    if st.orders:
        print("\nOrders on the book:")
        print(_table(
            ["Order", "Side", "Item", "Qty", "Filled", "Price", "State"],
            [[f"#{o.id}", o.side, o.name, f"{o.qty:,}", f"{o.filled:,}", f"{o.price:,}", "not placed yet" if o.state == "pending" else ("cancel asked" if o.cancel_requested else "in the GE")] for o in st.orders],
            right={3, 4, 5},
        ))
    if st.positions:
        print("\nStock held:")
        print(_table(["Item", "Units", "Avg cost", "Stage"], [[p.name, f"{p.qty:,}", f"{p.cost:,.1f}", p.stage] for p in st.positions.values()], right={1, 2}))
    closed = [o for o in st.done if o.seen and o.outcome in ("executed", "cancelled")]
    buys = [o for o in closed if o.side == "buy"]
    if buys:
        full = sum(1 for o in buys if o.outcome == "executed")
        some = sum(1 for o in buys if o.outcome == "cancelled" and o.filled)
        print(f"\nBuy orders finished: {len(buys)} - {full} filled completely, {some} partly, {len(buys) - full - some} not at all")
    reality = fill_reality(st.done, side="buy")
    if reality.orders:
        ratio = f"{reality.ratio:.2f}" if reality.ratio is not None else "n/a"
        print(f"Real fills vs the paper model on {reality.orders} measured buy orders: {ratio} (1.00 = backtests are right; below = they overstate)")


def _play_schedule(spec: str | None):
    """--play START-END in local hours -> (predicate or None, description)."""
    if not spec:
        return None, "at the GE around the clock"
    try:
        a, b = (int(x) for x in spec.split("-"))
        if not (0 <= a < 24 and 0 < b <= 24) or a == b:
            raise ValueError
    except ValueError:
        sys.exit("--play takes local hours as START-END, e.g. 19-23")

    def active(ts: int) -> bool:
        h = time.localtime(ts).tm_hour
        return a <= h < b if a < b else (h >= a or h < b)

    return active, f"at the GE {a:02d}:00-{b % 24:02d}:00 local ({(b - a) % 24} h a day)"


def _exec_backtest(args: argparse.Namespace, client: GEClient) -> None:
    items = client.mapping()
    cfg = _exec_config(args)
    cfg.max_age = 10 * 60  # history buckets are fresh relative to their own timestamp
    budget = parse_gp(args.budget)
    store = HistoryStore()
    _top_up(store, client, args.backtest / 24 + args.history_days + 0.1)
    _, last, _ = store.coverage()
    if last is None:
        sys.exit("No history available.")
    end, start = last, last - int(args.backtest * 3600)
    names = {i: it.name for i, it in items.items()}
    # Features come only from windows before the replay starts: no look-ahead.
    timed = None if args.no_stats else compute_timed_stats(store, names, start - int(args.history_days * 86400), start)
    stats_at = timed.at if timed else None
    windows = list(store.windows(start - 3600, end + 1))
    span = f"{time.strftime('%m-%d %H:%M', time.localtime(start))} to {time.strftime('%m-%d %H:%M', time.localtime(end))}"
    who = "free-to-play items" if cfg.members is False else "all items"
    active, presence = _play_schedule(args.play)
    print(f"Execution backtest: {args.backtest:g}h ({span}), {who}, start {fmt_gp(budget)}, {cfg.slots} GE slots, {cfg.inventory_slots} inventory stacks, {presence}")

    def top_share(pnl: dict[str, int]) -> str:
        gains = sum(v for v in pnl.values() if v > 0)
        if not gains:
            return "-"
        name, best = max(pnl.items(), key=lambda kv: kv[1])
        return f"{best / gains * 100:.0f}% {name}"

    def run(c: ExecConfig, share: int):
        r = run_backtest(windows, items, c, budget, start, stats_at, fill_share=share, strict=not args.loose, active=active)
        if r.coin_drift:  # the engine counts coins itself, as in the live loop; the paper account knows the truth
            print(f"  ! the engine's coin count was off by up to {r.coin_drift:,} gp against the paper account in this run")
        return r

    def row(label: str, r) -> list[str]:
        st = r.state
        return [
            label, fmt_gp(r.equity), f"{r.ret * 100:+.0f}%", fmt_gp(r.profit_per_hour), str(st.sells), f"{st.wins / st.sells * 100:.0f}%" if st.sells else "-",
            f"{r.actions_per_hour:.0f}", str(r.broker.max_stacks), top_share(r.pnl_by_item),
        ]

    headers = ["Model", "Equity", "Return", "gp/h at GE", "Sells", "Win", "Clicks/h", "Max stacks", "Biggest winner"]
    right = {1, 2, 3, 4, 5, 6, 7}
    if args.compare:
        rows = []
        for share in (10, 30, 100):
            rows.append(row(f"engine, you get 1/{share} of crossing volume", run(replace(cfg, fill_share=share), share)))
        rows.append(row("engine, 1/10, unlimited inventory", run(replace(cfg, inventory_slots=10**6), 10)))
        if active is None:
            legacy = Config(slots=cfg.slots, min_volume=cfg.min_volume, max_age=cfg.max_age, members=cfg.members, strict=not args.loose, min_margin=cfg.min_margin, min_roi=cfg.min_roi, improve=cfg.improve)
            state, snap, _ = replay(windows, items, legacy, budget, start, stats_at)
            by: dict[str, int] = {}
            for t in state.trades:
                if t.kind == "sell":
                    by[t.name] = by.get(t.name, 0) + t.profit
            sells = [t for t in state.trades if t.kind == "sell"]
            eq = state.equity(snap)
            rows.append([
                "old simulator, 1/10 (sells need no slot)", fmt_gp(eq), f"{(eq / budget - 1) * 100:+.0f}%", fmt_gp((eq - budget) / args.backtest), str(len(sells)),
                f"{sum(t.profit > 0 for t in sells) / len(sells) * 100:.0f}%" if sells else "-", "-", "-", top_share(by),
            ])
        print()
        print(_table(headers, rows, right=right))
        print("\nThe share of volume you really get is the unknown; `execute --report` measures it from your own fills.")
        return

    r = run(cfg, cfg.fill_share)
    print()
    print(_table(headers, [row(f"engine, 1/{cfg.fill_share} of crossing volume", r)], right=right))
    b = r.broker
    print(f"\nPlaced {b.placed['buy']} buys, {b.placed['sell']} sells, {b.placed['cancel']} cancels; never more than {b.max_offers} offers and {b.max_stacks} stacks at once; "
          f"{b.rejected} instructions could not be carried out, {b.blocked} collections blocked by a full inventory.")
    if r.cancel_reasons:
        print("Cancels: " + ", ".join(f"{n} {k}" for k, n in sorted(r.cancel_reasons.items(), key=lambda kv: -kv[1])))
    ranked = sorted(r.pnl_by_item.items(), key=lambda kv: kv[1], reverse=True)
    if ranked:
        print("\nRealised P&L by item (best and worst):")
        shown = ranked[:12] + ([("...", 0)] if len(ranked) > 15 else []) + (ranked[-3:] if len(ranked) > 15 else ranked[12:])
        print(_table(["Item", "Profit"], [[n, f"{p:+,}" if n != "..." else ""] for n, p in shown], right={1}))


def cmd_execute(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    if args.backtest:
        _exec_backtest(args, client)
        return
    d = _exec_dir(args)
    status_path, orders_path, state_path = d / "status.json", d / "orders.json", d / "state.json"
    if args.report:
        _exec_report(d)
        return
    if args.init:
        if state_path.exists() and not args.reset:
            sys.exit(f"A session already exists in {d}. Keep feeding status.json, or add --reset to start over.")
        for p in (state_path, orders_path):
            p.unlink(missing_ok=True)
        members = _members_filter(args)
        start = {
            "cash": parse_gp(args.cash or args.budget),
            "members": members is not False,
            "slots": args.slots if args.slots is not None else (3 if members is False else 8),
            "inventory_capacity": args.inventory_slots,
            "orders": [],
        }
        _write_json(status_path, start)
        print(f"Session started in {d}\n  status.json  <- you write this (cash, orders, optionally inventory)\n  orders.json  <- the engine answers here")
        print("Now run `ge-analyser execute` and leave it running.")
        return

    items = client.mapping()
    engine = Executor(ExecState.load(state_path) if state_path.exists() else ExecState(), items, _exec_config(args))
    if not args.once:
        print(f"Execution loop: reading {status_path}, writing {orders_path}; answers every {args.interval}s and whenever status.json changes. Ctrl-C stops it.", flush=True)
    seen = None
    last_run = 0.0
    while True:
        sig = status_path.stat().st_mtime_ns if status_path.exists() else None
        if sig != seen or time.time() - last_run >= args.interval:
            seen, last_run = sig, time.time()
            _exec_cycle(args, client, ledger, engine, items, d)
            if args.once:
                return
        time.sleep(2)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    client = GEClient(cache_ttl=0 if args.no_cache else 60)
    args.func(args, client, Ledger())


if __name__ == "__main__":
    main()
