from ge_analyser.analysis import evaluate_flip, flip_quantity, remaining_limit
from ge_analyser.api import ItemInfo, LatestPrice, WindowPrice
from ge_analyser.ledger import LIMIT_WINDOW_S, Ledger

WHIP = ItemInfo(id=4151, name="Abyssal whip", members=True, limit=70, value=0, highalch=0)
NOW = 1_700_000_000


def test_purchases_expire_after_four_hours(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.record(4151, 30, NOW - LIMIT_WINDOW_S - 1)  # just outside the window
    ledger.record(4151, 20, NOW - 60)
    ledger.record(4151, 10, NOW)
    assert ledger.used(NOW) == {4151: 30}
    assert ledger.resets_at(4151, NOW) == NOW - 60 + LIMIT_WINDOW_S
    assert ledger.used(NOW + LIMIT_WINDOW_S) == {}


def test_ledger_persists_and_clears(tmp_path):
    path = tmp_path / "ledger.json"
    Ledger(path).record(4151, 5, NOW)
    Ledger(path).record(560, 100, NOW)
    reloaded = Ledger(path)
    assert reloaded.used(NOW) == {4151: 5, 560: 100}
    assert reloaded.clear(4151) == 1
    assert Ledger(path).used(NOW) == {560: 100}
    assert Ledger(path).clear() == 1
    assert Ledger(path).used(NOW) == {}


def test_used_limit_reduces_quantity_and_excludes_exhausted():
    assert remaining_limit(WHIP, 50) == 20
    assert remaining_limit(WHIP, 90) == 0
    assert flip_quantity(WHIP, 1_000_000, hourly_volume=10_000, budget=10**9, used=50) == 20
    full = evaluate_flip(WHIP, LatestPrice(2_000_000, NOW, 1_900_000, NOW), WindowPrice(None, 500, None, 500), 10**9, NOW, used=70)
    assert full is None
    partial = evaluate_flip(WHIP, LatestPrice(2_000_000, NOW, 1_900_000, NOW), WindowPrice(None, 500, None, 500), 10**9, NOW, used=60)
    assert partial is not None and partial.quantity == 10 and partial.limit_left == 10
