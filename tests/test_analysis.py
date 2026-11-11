from ge_analyser.analysis import (
    TAX_CAP,
    evaluate_flip,
    find_flips,
    flip_quantity,
    ge_tax,
    parse_gp,
    trend_from_timeseries,
)
from ge_analyser.api import ItemInfo, LatestPrice, WindowPrice

WHIP = ItemInfo(id=4151, name="Abyssal whip", members=True, limit=70, value=120_001, highalch=72_000)
NOW = 1_700_000_000


def test_tax_rules():
    assert ge_tax(49) == 0
    assert ge_tax(50) == 1
    assert ge_tax(1_000_000) == 20_000
    assert ge_tax(1_000_000_000) == TAX_CAP
    assert ge_tax(10_000_000, "Old school bond") == 0
    assert ge_tax(12_000, "Varrock teleport (tablet)") == 0
    assert ge_tax(12_000, "Varrock teleport") == 240  # non-tablet names are not exempt


def test_margin_is_after_tax():
    flip = evaluate_flip(WHIP, LatestPrice(2_000_000, NOW, 1_900_000, NOW), WindowPrice(None, 500, None, 400), 100_000_000, NOW)
    assert flip is not None
    assert flip.tax == 40_000
    assert flip.margin == 60_000


def test_unprofitable_after_tax_is_rejected():
    # 1% raw spread, 2% tax -> negative
    assert evaluate_flip(WHIP, LatestPrice(1_010_000, NOW, 1_000_000, NOW), None, 10_000_000, NOW) is None


def test_quantity_bounded_by_limit_budget_and_volume():
    assert flip_quantity(WHIP, 1_000_000, hourly_volume=10_000, budget=1_000_000_000) == 70  # limit
    assert flip_quantity(WHIP, 1_000_000, hourly_volume=10_000, budget=5_000_000) == 5  # budget
    assert flip_quantity(WHIP, 1_000_000, hourly_volume=8, budget=1_000_000_000) == 2  # volume / 4
    assert flip_quantity(WHIP, 1_000_000, hourly_volume=0, budget=1_000_000_000) == 1  # floor of 1


def test_find_flips_filters_and_sorts():
    items = {
        1: ItemInfo(1, "Fresh", False, 1000, 0, 0),
        2: ItemInfo(2, "Stale", False, 1000, 0, 0),
        3: ItemInfo(3, "Illiquid", False, 1000, 0, 0),
        4: ItemInfo(4, "Better", False, 1000, 0, 0),
        5: ItemInfo(5, "Old school bond", False, 100, 0, 0),
    }
    latest = {
        1: LatestPrice(1100, NOW, 1000, NOW),
        2: LatestPrice(1100, NOW - 7200, 1000, NOW),
        3: LatestPrice(1100, NOW, 1000, NOW),
        4: LatestPrice(1300, NOW, 1000, NOW),
        5: LatestPrice(2000, NOW, 1000, NOW),  # the widest spread of all, and untaxed
    }
    hour = {
        1: WindowPrice(None, 400, None, 400),
        2: WindowPrice(None, 400, None, 400),
        3: WindowPrice(None, 10, None, 10),
        4: WindowPrice(None, 400, None, 400),
        5: WindowPrice(None, 400, None, 400),
    }
    flips = find_flips(items, latest, hour, budget=10_000_000, now=NOW, min_volume=50, max_age_s=3600)
    # A bond bought on the GE comes back untradeable: it is never a flip, whatever its spread.
    assert [f.item.name for f in flips] == ["Better", "Fresh"]


def test_trend_z_score_and_change():
    series = [{"avgHighPrice": 100, "avgLowPrice": 100, "highPriceVolume": 1, "lowPriceVolume": 1}] * 9
    series = series + [{"avgHighPrice": 80, "avgLowPrice": 80, "highPriceVolume": 1, "lowPriceVolume": 1}]
    t = trend_from_timeseries(series)
    assert t is not None
    assert t.points == 10
    assert t.z_score < -2
    assert t.change_pct == -20.0
    assert t.total_volume == 20
    assert trend_from_timeseries([{"avgHighPrice": None, "avgLowPrice": None}]) is None


def test_parse_gp():
    assert parse_gp("10m") == 10_000_000
    assert parse_gp("2.5k") == 2500
    assert parse_gp("1b") == 1_000_000_000
    assert parse_gp("1,234gp") == 1234
