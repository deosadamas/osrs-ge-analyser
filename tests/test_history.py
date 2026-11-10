from ge_analyser.analysis import UNKNOWN_STATS, ItemStats, evaluate_flip, find_flips
from ge_analyser.api import ItemInfo, LatestPrice, WindowPrice
from ge_analyser.features import TimedStats, bucket_of, compute_stats, compute_timed_stats, item_stats
from ge_analyser.history import STEP, HistoryStore, Row, align

T0 = 1_700_000_000 // STEP * STEP


class FakeClient:
    """Serves a deterministic window for any timestamp: price drifts down 1 gp per step."""

    def __init__(self):
        self.calls = []

    def window_at(self, step, ts):
        self.calls.append(ts)
        k = (ts - T0) // STEP
        return {440: {"avgHighPrice": 120.4 - k, "highPriceVolume": 50, "avgLowPrice": 100.6 - k, "lowPriceVolume": 40}}


def test_store_update_is_incremental_and_rounds_prices(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    client = FakeClient()
    now = T0 + 10 * STEP
    # The window starting at now-STEP may still be aggregating, so it is skipped: 9 of 10.
    assert store.update(client, days=10 * STEP / 86400, now=now, workers=1) == 9
    assert store.update(client, days=10 * STEP / 86400, now=now, workers=1) == 0  # nothing new
    assert len(client.calls) == 9
    assert store.coverage() == (T0, T0 + 8 * STEP, 9)
    rows = store.series(T0)[440]
    assert rows[0] == Row(T0, 120, 50, 101, 40)  # 120.4 -> 120, 100.6 -> 101
    assert [ts for ts, _ in store.windows(T0, T0 + 3 * STEP)] == [T0, T0 + STEP, T0 + 2 * STEP]
    assert align(T0 + 17) == T0


def rows(lows, highs, vol=10):
    return [(T0 + i * STEP, h, vol, lo, vol) for i, (lo, h) in enumerate(zip(lows, highs))]


def test_item_stats_fill_rates_and_margin():
    # Low falls 1 gp per window -> every judged window is crossed below within 30 min.
    lows = [100 - i for i in range(20)]
    highs = [110] * 20  # never exceeded -> sells never cross
    s = item_stats(rows(lows, highs), "Iron ore")
    assert s is not None and s.windows == 20
    assert s.fill_buy == 1.0 and s.fill_sell == 0.0
    assert s.median_margin == 17  # margins 8..27 (tax 2 on 110); median 17.5 -> int
    assert s.volatility_pct > 0
    assert item_stats(rows([100] * 5, [110] * 5), "x") is None  # too few windows
    # Alternating 100/99: from a 99 window the next lows (100, 99) never go strictly lower.
    assert item_stats(rows([100, 99] * 10, [110] * 20), "x").fill_buy == 0.5


def test_tail_windows_without_future_are_not_counted():
    # Flat series: nothing ever crosses; the last 6 windows have < 30 min of future and are skipped.
    s = item_stats(rows([100] * 30, [110] * 30), "x")
    assert s.fill_buy == 0.0 and s.fill_sell == 0.0 and s.volatility_pct == 0.0


def test_timed_stats_buckets_and_liquidity(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    # Two weeks of 5m windows: quiet on weekdays (vol 5), busy at weekends (vol 50); a
    # bid is crossed only at weekends (low dips every other window there).
    for i in range(14 * 288):
        ts = T0 + i * STEP
        weekend = bucket_of(ts) >= 6
        low = 100 - (i % 2 if weekend else 0)
        vol = 50 if weekend else 5
        store.store_window(ts, {440: {"avgHighPrice": 110, "highPriceVolume": vol, "avgLowPrice": low, "lowPriceVolume": vol}})
    timed = compute_timed_stats(store, {440: "Iron ore"}, T0)
    overall = timed.overall[440]
    assert 0.1 < overall.fill_buy < 0.5
    weekend_ts = next(T0 + i * STEP for i in range(14 * 288) if bucket_of(T0 + i * STEP) >= 6)
    weekday_ts = next(T0 + i * STEP for i in range(14 * 288) if bucket_of(T0 + i * STEP) < 6)
    wk, wd = timed.at(weekend_ts)[440], timed.at(weekday_ts)[440]
    assert wk.fill_buy == 0.5 and wd.fill_buy < 0.05  # weekday windows just before Saturday see the first dip
    assert wk.typical_volume == 50 * 12 and wd.typical_volume == 5 * 12
    assert timed.flat_at(weekend_ts)[440] is overall
    # Liquidity planned on is min(live volume, typical for this moment).
    item = ItemInfo(440, "Iron ore", False, 13000, 0, 0)
    live = WindowPrice(None, 10_000, None, 10_000)
    f = evaluate_flip(item, LatestPrice(110, weekday_ts, 100, weekday_ts), live, 10**7, weekday_ts, stats=wd)
    assert f.liquidity == 60 and f.quantity == 15  # 60/4
    f = evaluate_flip(item, LatestPrice(110, weekend_ts, 100, weekend_ts), WindowPrice(None, 100, None, 100), 10**7, weekend_ts, stats=wk)
    assert f.liquidity == 100
    assert TimedStats.decode(timed.encode()).at(weekend_ts)[440] == wk


def test_compute_stats_from_store(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    for i in range(20):
        store.store_window(T0 + i * STEP, {440: {"avgHighPrice": 110, "highPriceVolume": 1, "avgLowPrice": 100 - i, "lowPriceVolume": 1}})
    stats = compute_stats(store, {440: "Iron ore"}, T0)
    assert stats[440].fill_buy == 1.0


def test_stats_change_ranking_and_unknown_items_sink():
    now = T0
    items = {1: ItemInfo(1, "Spiky", False, 1000, 0, 0), 2: ItemInfo(2, "Steady", False, 1000, 0, 0), 3: ItemInfo(3, "Ghost", False, 1000, 0, 0)}
    latest = {
        1: LatestPrice(200, now, 100, now),  # huge margin today...
        2: LatestPrice(112, now, 100, now),  # modest margin
        3: LatestPrice(200, now, 100, now),  # huge margin, no history at all
    }
    hour = {i: WindowPrice(None, 1000, None, 1000) for i in items}
    stats = {
        1: ItemStats(100, 0.1, 0.1, 5, 8.0),  # ...but typically 5 gp, rarely fills, volatile
        2: ItemStats(100, 0.8, 0.8, 10, 0.5),
    }
    legacy = [f.item.name for f in find_flips(items, latest, hour, 10_000, now, min_volume=0)]
    informed = [f.item.name for f in find_flips(items, latest, hour, 10_000, now, min_volume=0, stats=stats)]
    assert legacy[0] in ("Spiky", "Ghost")
    assert informed[0] == "Steady"
    ghost = next(f for f in find_flips(items, latest, hour, 10_000, now, min_volume=0, stats=stats) if f.item.id == 3)
    assert ghost.stats is UNKNOWN_STATS and ghost.expected_profit == int(ghost.profit * 0.01)
    steady = evaluate_flip(items[2], latest[2], hour[2], 10_000, now, stats=stats[2])
    assert steady.expected_profit == int(min(steady.margin, 10) * steady.quantity * 0.64)
