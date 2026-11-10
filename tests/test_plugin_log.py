import json

from ge_analyser.runelite import load_trades, parse_plugin_log

NOW = 1_700_000_000


def ev(t, slot, buy, item, qty, total, price, st, placed, acc="Me"):
    return {"t": t, "acc": acc, "slot": slot, "b": buy, "id": item, "name": f"item{item}", "qty": qty, "total": total, "price": price, "listed": price, "st": st, "placed": placed}


def test_plugin_log_last_state_per_trade_and_login_replay_dedupe(tmp_path):
    lines = [
        ev(NOW - 7000, 0, True, 4151, 0, 2, 650_000, "BUYING", NOW - 7000),
        ev(NOW - 6800, 0, True, 4151, 2, 2, 650_000, "BOUGHT", NOW - 7000),
        ev(NOW - 6800, 0, True, 4151, 2, 2, 650_000, "BOUGHT", NOW - 7000),  # same session repeat
        ev(NOW - 5000, 0, True, 4151, 2, 2, 650_000, "BOUGHT", NOW - 5000),  # login replay, new 'placed'
        ev(NOW - 3000, 0, False, 4151, 1, 2, 676_000, "SELLING", NOW - 3500),
        ev(NOW - 2900, 0, False, 4151, 2, 2, 676_000, "SELLING", NOW - 3500),  # supersedes previous
        ev(NOW - 100, 1, True, 560, 120, 500, 204, "CANCELLED_BUY", NOW - 2000),
        ev(NOW - 50, 2, True, 0, 0, 0, 0, "EMPTY", NOW - 50),  # ignored
    ]
    p = tmp_path / "runelite-trades.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    trades = parse_plugin_log(p)
    assert [(t.item_id, t.buy, t.qty, t.state) for t in trades] == [
        (4151, True, 2, "BOUGHT"),
        (4151, False, 2, "SELLING"),
        (560, True, 120, "CANCELLED_BUY"),
    ]
    assert trades[0].fill_seconds == 200.0 and trades[0].complete
    assert trades[1].fill_seconds == 600.0 and not trades[1].complete
    assert trades[2].offered_qty == 500 and trades[2].account == "Me"
    assert load_trades([p]) == trades  # .jsonl is routed to the plugin parser
