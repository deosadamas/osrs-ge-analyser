"""Synthetic Flipping Utilities account file, shaped per the plugin's Gson field names
(see ge_analyser/runelite.py docstring). No real export was available when this was
written; if a real file breaks parse_account, add it as a fixture here."""

import json

from ge_analyser.calibrate import bucket_at, calibrate, fifo_pnl, fill_stats, lookback_for_age
from ge_analyser.ledger import Ledger, Purchase
from ge_analyser.runelite import account_files, parse_account, parse_instant

NOW = 1_700_000_000


def ev(buy, item_id, qty, price, ts, state, slot=0, total=None, ticks=100):
    return {
        "b": buy,
        "id": item_id,
        "cQIT": qty,
        "p": price,
        "t": {"seconds": ts, "nanos": 0},
        "s": slot,
        "st": state,
        "tAA": 0,
        "tSFO": ticks,
        "tQIT": total if total is not None else qty,
        "vSQ": True,
    }


FIXTURE = {
    "lastOffers": {},
    "trades": [
        {
            "id": 4151,
            "name": "Abyssal whip",
            "tGL": 70,
            "h": {
                "sO": [
                    ev(True, 4151, 2, 1_000_000, NOW - 7200, "BOUGHT", ticks=600),  # bought 2 @ 1m, 6 min
                    ev(False, 4151, 2, 1_100_000, NOW - 3600, "SOLD", ticks=1000),  # sold 2 @ 1.1m
                    ev(True, 4151, 1, 990_000, NOW - 600, "BUYING", total=5, ticks=0),  # in progress
                ],
                "nGLR": {"seconds": NOW + 7200, "nanos": 0},
                "iBTLW": 3,
                "pIB": 2,
            },
            "fB": "Tester",
            "vFPI": True,
            "favorite": False,
            "favoriteCode": "1",
        },
        {
            "id": 560,
            "name": "Death rune",
            "tGL": 25000,
            "h": {
                "sO": [
                    ev(True, 560, 300, 200, NOW - 6 * 3600, "CANCELLED_BUY", total=1000),  # partial then cancelled
                    ev(True, 560, 0, 0, NOW - 100, "BUYING", total=10),  # nothing traded yet -> skipped
                    ev(False, 560, 0, 0, NOW - 50, "EMPTY"),  # skipped
                ],
                "nGLR": None,
                "iBTLW": 300,
                "pIB": 300,
            },
            "fB": "Tester",
            "vFPI": True,
            "favorite": False,
            "favoriteCode": "1",
        },
    ],
    "sessionStartTime": {"seconds": NOW - 10_000, "nanos": 0},
    "accumulatedSessionTime": {"seconds": 0, "nanos": 0},
}


def write_fixture(tmp_path):
    d = tmp_path / "flipping"
    d.mkdir()
    (d / "Tester.json").write_text(json.dumps(FIXTURE), encoding="utf-8")
    (d / "accountwide.json").write_text("{}", encoding="utf-8")
    return d


def test_parse_instant_variants():
    assert parse_instant({"seconds": 5, "nanos": 9}) == 5
    assert parse_instant(1_700_000_000) == 1_700_000_000
    assert parse_instant(1_700_000_000_000) == 1_700_000_000
    assert parse_instant("2023-11-14T22:13:20Z") == 1_700_000_000
    assert parse_instant(None) is None


def test_parse_account_skips_empty_and_untraded(tmp_path):
    d = write_fixture(tmp_path)
    assert [p.name for p in account_files(d)] == ["Tester.json"]
    trades = parse_account(d / "Tester.json")
    assert [(t.name, t.buy, t.qty, t.state) for t in trades] == [
        ("Death rune", True, 300, "CANCELLED_BUY"),
        ("Abyssal whip", True, 2, "BOUGHT"),
        ("Abyssal whip", False, 2, "SOLD"),
        ("Abyssal whip", True, 1, "BUYING"),
    ]
    assert trades[1].fill_seconds == 360.0
    assert trades[3].fill_seconds is None
    assert trades[0].offered_qty == 1000 and trades[0].account == "Tester"


def test_fifo_pnl_applies_tax_and_tracks_unsold(tmp_path):
    trades = parse_account(write_fixture(tmp_path) / "Tester.json")
    pnl = fifo_pnl(trades)
    whip = pnl[4151]
    # sold 2 @ 1.1m, tax 22k each, cost 1m -> +78k each
    assert whip.realised == 2 * (1_100_000 - 22_000 - 1_000_000)
    assert whip.tax == 44_000
    assert whip.bought == 3 and whip.sold == 2 and whip.unmatched_qty == 1
    assert pnl[560].realised == 0 and pnl[560].unmatched_qty == 300


def test_fill_stats(tmp_path):
    trades = parse_account(write_fixture(tmp_path) / "Tester.json")
    s = fill_stats(trades)
    assert s.completed_buys == 1 and s.completed_sells == 1
    assert s.cancelled_buys == 1 and s.cancelled_fill_ratio == [0.3]
    assert s.median(s.buy_fill_seconds) == 360.0


def test_ledger_import_is_idempotent_and_keeps_manual(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.record(4151, 10, NOW - 60)  # manual
    rows = [Purchase(4151, 2, NOW - 7200, "runelite:Tester"), Purchase(560, 300, NOW - 6 * 3600, "runelite:Tester")]
    assert ledger.replace_source("runelite:Tester", rows, NOW) == 1  # death rune buy is outside the 4h window
    assert ledger.used(NOW) == {4151: 12}
    assert ledger.replace_source("runelite:Tester", rows, NOW) == 1
    assert ledger.used(NOW) == {4151: 12}  # no double counting


def test_calibration_classifies_against_wiki_print():
    from ge_analyser.runelite import RealTrade

    def rt(buy, price, ts, state):
        return RealTrade("a", 1, "x", buy, 1, 1, price, ts, state, True, None)

    series = [{"timestamp": NOW - 600, "avgLowPrice": 100, "avgHighPrice": 110}, {"timestamp": NOW - 300, "avgLowPrice": 98, "avgHighPrice": 112}]
    assert bucket_at(series, NOW - 400)["avgLowPrice"] == 100
    assert lookback_for_age(3600) == "24h" and lookback_for_age(3 * 86400) == "7d" and lookback_for_age(20 * 86400) == "30d"
    history = {(1, "24h"): series}
    trades = [
        rt(True, 97, NOW - 200, "BOUGHT"),  # paid below instant-sell 98 -> strict fill
        rt(True, 100, NOW - 200, "BOUGHT"),  # at/above -> loose fill
        rt(False, 115, NOW - 200, "SOLD"),  # above instant-buy 112 -> strict
        rt(False, 105, NOW - 200, "SOLD"),  # below -> loose
        rt(True, 1, NOW - 10_000, "BOUGHT"),  # before any bucket -> unknown
    ]
    c = calibrate(trades, history, NOW)
    assert c.classified == 4 and c.unknown == 1
    assert c.strict_fills == 2 and c.loose_fills == 2
    assert "mixed" in c.suggestion()
