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

from .analysis import (
    Flip,
    find_flips,
    fmt_gp,
    ge_tax,
    parse_gp,
    remaining_limit,
    trend_from_timeseries,
)
from .api import GEClient, ItemInfo
from .calibrate import calibrate, fifo_pnl, fill_stats, step_for_age
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
    rows = [
        [
            f.item.name,
            fmt_gp(f.buy),
            fmt_gp(f.sell),
            fmt_gp(f.margin),
            f"{f.roi * 100:.1f}%",
            (f"{f.limit_left:,}/{f.item.limit:,}" if f.limit_left != f.item.limit else f"{f.item.limit:,}") if f.item.limit else "-",
            f"{f.hourly_volume:,}",
            f"{f.quantity:,}",
            fmt_gp(f.profit),
            _age(f.price_age_s),
        ]
        for f in flips
    ]
    print(
        _table(
            ["Item", "Buy", "Sell", "Margin", "ROI", "Limit", "Vol/h", "Qty", "Profit", "Age"],
            rows,
            right={1, 2, 3, 4, 5, 6, 7, 8, 9},
        )
    )


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


def cmd_flips(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    now = int(time.time())
    items = client.mapping()
    latest = client.latest()
    hour = client.window("1h")
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
    hour = client.window("1h").get(item.id)
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

    for step, label in (("1h", "24h (1h candles)"), ("6h", "~3 months (6h candles)")):
        trend = trend_from_timeseries(client.timeseries(item.id, step))
        if trend is None:
            continue
        signal = "BUY (below average)" if trend.z_score < -1 else "SELL (above average)" if trend.z_score > 1 else "neutral"
        print(f"  Trend {label}: mean {fmt_gp(trend.mean)}, now {fmt_gp(trend.latest)}, z={trend.z_score:+.2f}, change {trend.change_pct:+.1f}%  -> {signal}")


def cmd_dips(args: argparse.Namespace, client: GEClient, ledger: Ledger) -> None:
    """Among liquid items, find those currently priced well below their 24h average."""
    now = int(time.time())
    items = client.mapping()
    latest = client.latest()
    hour = client.window("1h")
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
        trend = trend_from_timeseries(client.timeseries(item.id, "1h"))
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
        latest, five, hour = client.latest(), client.window("5m"), client.window("1h")
        snap = snapshot_from_live(latest, five)
        sim.step(snap, latest, hour, now)
        state.save(path)
        print(f"--- {time.strftime('%H:%M:%S', time.localtime(now))} ---")
        _print_sim(state, snap, now, tail=8)
        n += 1
        if args.ticks == 0 or n < args.ticks:
            time.sleep(args.interval)
    print(f"\nState saved to {path}. Resume with `ge-analyser simulate --ticks N`, inspect with --status.")


def _backtest(args: argparse.Namespace, client: GEClient, items: dict[int, ItemInfo], cfg: Config) -> None:
    budget = parse_gp(args.budget)
    hour = client.window("1h")
    liquid = sorted(
        (i for i, w in hour.items() if i in items and (cfg.members is None or items[i].members == cfg.members)),
        key=lambda i: min(hour[i].high_volume, hour[i].low_volume),
        reverse=True,
    )[: args.scan]
    print(f"Backtest: {args.backtest:g}h of 5m history, {len(liquid)} most liquid items, start {fmt_gp(budget)}, {cfg.slots} slots")

    by_ts: dict[int, dict[int, dict]] = {}
    for item_id in liquid:
        for b in client.timeseries(item_id, "5m"):
            by_ts.setdefault(b["timestamp"], {})[item_id] = b
    timestamps = sorted(by_ts)
    if not timestamps:
        sys.exit("No history returned.")
    start_ts = timestamps[-1] - int(args.backtest * 3600)
    # Keep one hour of warm-up buckets so hourly volume is populated at the first decision.
    warmup = [ts for ts in timestamps if ts < start_ts][-12:]
    run = [ts for ts in timestamps if ts >= start_ts]

    state = State(start_cash=budget, cash=budget)
    cfg.max_age = 10 * 60  # buckets are always fresh relative to their own timestamp
    sim = Simulator(state, items, cfg)
    history: list[Snapshot] = [snapshot_from_buckets(by_ts[ts], ts) for ts in warmup]
    snap: Snapshot = {}
    for ts in run:
        snap = snapshot_from_buckets(by_ts[ts], ts)
        history.append(snap)
        del history[:-12]
        sim.step(snap, latest_from_snapshot(snap), hour_from_history(history), ts)
    # Mark-to-market any leftovers at the final print.
    print(f"Replayed {len(run)} buckets from {time.strftime('%m-%d %H:%M', time.localtime(run[0]))} to {time.strftime('%m-%d %H:%M', time.localtime(run[-1]))}\n")
    _print_sim(state, snap, run[-1], tail=25)
    sells = [t for t in state.trades if t.kind == "sell"]
    if sells:
        by_item: dict[str, int] = {}
        for t in sells:
            by_item[t.name] = by_item.get(t.name, 0) + t.profit
        ranked = sorted(by_item.items(), key=lambda kv: kv[1], reverse=True)
        print("\nRealised P&L by item:")
        print(_table(["Item", "Profit"], [[n, f"{p:+,}"] for n, p in ranked], right={1}))


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
        items = sorted({(t.item_id, step_for_age(now - t.ts)) for t in completed})
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
    s.add_argument("--scan", type=int, default=40, help="backtest: number of most-liquid items to replay")
    s.add_argument("--status", action="store_true", help="show the live simulation portfolio and exit")
    s.add_argument("--reset", action="store_true", help="delete the live simulation state")
    s.set_defaults(func=cmd_simulate)

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
