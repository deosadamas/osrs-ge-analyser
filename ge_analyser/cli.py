"""Command-line interface.

    ge-analyser flips --budget 10m --top 20
    ge-analyser item "Abyssal whip"
    ge-analyser dips --budget 10m --top 15
"""

from __future__ import annotations

import argparse
import sys
import time

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
from .ledger import Ledger


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
        print("No flips matched the filters.")
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
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    client = GEClient(cache_ttl=0 if args.no_cache else 60)
    args.func(args, client, Ledger())


if __name__ == "__main__":
    main()
