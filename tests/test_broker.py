from types import SimpleNamespace

from ge_analyser.api import ItemInfo
from ge_analyser.broker import PaperBroker, run_backtest
from ge_analyser.calibrate import fill_reality, model_fill, sizing_scales
from ge_analyser.executor import ExecConfig, Plan
from ge_analyser.sim import Tick

ORE = 440
ITEMS = {ORE: ItemInfo(ORE, "Iron ore", False, 13000, 0, 0), 329: ItemInfo(329, "Salmon", False, 6000, 0, 0)}
T0 = 1_700_000_100 // 300 * 300


def plan(*actions):
    return Plan(1, T0, T0, list(actions), [], [], {}, {})


def tick(ts, low=None, high=None, low_vol=0, high_vol=0):
    return {ORE: Tick(high, ts, low, ts, high_vol, low_vol, window=ts)}


def test_buy_reserves_coins_fills_only_when_crossed_and_in_proportion_to_flow():
    b = PaperBroker(ITEMS, cash=10_000, slots=2, fill_share=10)
    b.apply(plan({"action": "BUY", "order_id": 1, "item_id": ORE, "qty": 10, "price": 100}), {}, T0)
    assert b.cash == 9_000  # reserved up front, as the GE does
    b.process_fills(tick(T0, low=90, low_vol=1000), T0)  # the print that priced the order cannot fill it
    b.process_fills(tick(T0 + 300, low=100, low_vol=1000), T0 + 300)  # touched, not crossed
    assert b.offers[0].filled == 0
    b.process_fills(tick(T0 + 600, low=99, low_vol=5), T0 + 600)  # 5 units of flow -> half a unit of credit
    assert b.offers[0].filled == 0
    b.process_fills(tick(T0 + 900, low=99, low_vol=5), T0 + 900)
    assert b.offers[0].filled == 1  # thin flow fills eventually, not one unit every window
    b.process_fills(tick(T0 + 1200, low=99, low_vol=1000), T0 + 1200)
    assert b.offers[0].state == "executed" and b.used(T0 + 1200) == {ORE: 10}

    s = b.status(T0 + 1200)  # Collect, then report
    assert s.inventory == {ORE: 10} and not b.offers and s.cash == 9_000
    assert [(r.order_id, r.status, r.filled) for r in s.orders] == [(1, "executed", 10)]
    assert b.status(T0 + 1260).orders == []  # a finished order is reported once


def test_sell_pays_tax_on_collect_and_touch_fills_only_when_loose():
    for strict, sold in ((True, 0), (False, 10)):
        b = PaperBroker(ITEMS, cash=0, slots=2, fill_share=10, strict=strict)
        b.inventory[ORE] = 10
        b.apply(plan({"action": "SELL", "order_id": 2, "item_id": ORE, "qty": 10, "price": 110}), tick(T0, low=100, high=110), T0)
        assert ORE not in b.inventory
        b.process_fills(tick(T0 + 300, high=110, high_vol=1000), T0 + 300)
        assert b.offers[0].filled == sold
    b.process_fills(tick(T0 + 600, high=111, high_vol=1000), T0 + 600)
    assert b.cash == 0  # proceeds sit in the offer until collected
    assert b.status(T0 + 600).cash == 10 * (110 - 2)


def test_slots_cancel_refund_and_marketable_dump():
    b = PaperBroker(ITEMS, cash=10_000, slots=1, fill_share=10)
    b.apply(plan({"action": "BUY", "order_id": 1, "item_id": ORE, "qty": 50, "price": 100}, {"action": "BUY", "order_id": 2, "item_id": 329, "qty": 10, "price": 50}), {}, T0)
    assert len(b.offers) == 1 and b.rejected == 1  # one slot: the second offer cannot be placed
    b.process_fills(tick(T0 + 300, low=99, low_vol=200), T0 + 300)
    assert b.offers[0].filled == 20
    b.status(T0 + 300)
    # Abort, then use the freed slot and the returned coins in the same batch.
    b.apply(plan({"action": "CANCEL", "order_id": 1}, {"action": "SELL", "order_id": 3, "item_id": ORE, "qty": 20, "price": 98}), tick(T0 + 300, low=99, low_vol=200), T0 + 300)
    assert b.cash == 10_000 - 20 * 100 and b.placed == {"buy": 1, "sell": 1, "cancel": 1}
    assert b.offers[0].order_id == 3 and b.offers[0].filled == 20  # ask at/below the bid: sold at once, within the flow
    s = b.status(T0 + 360)
    assert sorted((r.order_id, r.status, r.filled) for r in s.orders) == [(1, "cancelled", 20), (3, "executed", 20)]
    assert s.cash == 8_000 + 20 * (98 - 1) and s.inventory == {}


def test_inventory_capacity_blocks_collection():
    b = PaperBroker(ITEMS, cash=10_000, slots=2, capacity=1, fill_share=1)
    b.inventory[329] = 5  # the only stack is taken
    b.apply(plan({"action": "BUY", "order_id": 1, "item_id": ORE, "qty": 5, "price": 100}), {}, T0)
    b.process_fills(tick(T0 + 300, low=99, low_vol=100), T0 + 300)
    s = b.status(T0 + 300)
    assert b.blocked == 1 and ORE not in s.inventory and len(b.offers) == 1  # bought, but stuck in the offer


def test_engine_and_broker_round_trip_respects_every_limit():
    # Iron ore oscillates: instant-sell 100/99, instant-buy 110/111, plenty of flow.
    windows = [
        (T0 + k * 300, {ORE: {"avgHighPrice": 110 + k % 2, "highPriceVolume": 4000, "avgLowPrice": 100 - k % 2, "lowPriceVolume": 4000}})
        for k in range(120)
    ]
    cfg = ExecConfig(slots=3, inventory_slots=2, max_age=600)
    r = run_backtest(windows, ITEMS, cfg, 10_000, start=T0 + 12 * 300, fill_share=10)
    assert r.steps == 108
    assert r.state.realised > 0 and r.state.wins == r.state.sells > 0  # buy ~100, sell ~110, 2 gp tax
    assert r.equity > 10_000 and r.broker.cash >= 0
    assert r.broker.max_offers <= 3 and r.broker.max_stacks <= 2
    assert r.broker.blocked == 0 and r.broker.rejected == 0  # the engine never asks for the impossible
    assert set(r.pnl_by_item) == {"Iron ore"}
    # Nothing is created: equity = start + realised profit + stock marked at the last instant-sell.
    stock = sum(p.qty * (99 - p.cost) for p in r.state.positions.values())
    assert abs(r.equity - (10_000 + r.state.realised + stock)) <= 110 * len(r.state.positions) + 1


def test_offers_keep_filling_while_you_are_away_but_nothing_is_managed():
    windows = [
        (T0 + k * 300, {ORE: {"avgHighPrice": 110 + k % 2, "highPriceVolume": 4000, "avgLowPrice": 100 - k % 2, "lowPriceVolume": 4000}})
        for k in range(120)
    ]
    cfg = ExecConfig(slots=3, inventory_slots=2, max_age=600)
    always = run_backtest(windows, ITEMS, cfg, 10_000, start=T0 + 12 * 300)
    hourly = run_backtest(windows, ITEMS, cfg, 10_000, start=T0 + 12 * 300, active=lambda ts: ((ts - T0) // 300) % 12 == 0)
    assert (hourly.steps, hourly.hours) == (9, 0.75) and hourly.span_hours == always.span_hours == 9.0
    assert 0 < hourly.state.realised < always.state.realised  # a look once an hour still trades, just less
    assert hourly.broker.rejected == 0 and hourly.broker.blocked == 0
    assert hourly.profit_per_hour == (hourly.equity - 10_000) / 0.75  # measured per hour actually spent at the GE


def test_engine_counts_the_coins_exactly_through_timeouts_reprices_and_dumps():
    def window(k):
        salmon = 80 - k // 6  # falls steadily: stock goes under water and has to be repriced, then dumped
        return {
            # Iron ore: a wide spread, but sellers cross the bid only every 4th window -> part fills, then timeouts
            ORE: {"avgHighPrice": 110 + k % 2, "highPriceVolume": 300, "avgLowPrice": 99 if k % 4 == 0 else 102, "lowPriceVolume": 300},
            329: {"avgHighPrice": salmon + 6, "highPriceVolume": 400, "avgLowPrice": salmon - (k % 3 == 0), "lowPriceVolume": 400},
        }

    cfg = ExecConfig(slots=3, inventory_slots=2, max_age=600, buy_timeout=900, sell_reprice=900, sell_dump=1800)
    r = run_backtest([(T0 + k * 300, window(k)) for k in range(200)], ITEMS, cfg, 10_000, start=T0 + 12 * 300)
    assert {"buy timeout", "reprice", "dump"} <= set(r.cancel_reasons) and r.state.wins < r.state.sells  # every path was taken
    # The paper account states its coins once; after that the engine's own count has to be right to the coin.
    assert r.coin_drift == 0 and r.broker.rejected == 0


def test_model_fill_weights_windows_by_how_long_the_order_was_live():
    rows = [
        (T0, 110, 500, 99, 1000),  # live for the last 150 s of this window
        (T0 + 300, 110, 500, 101, 1000),  # instant-sell above the bid: no cross
        (T0 + 600, 110, 500, 98, 400),
    ]
    assert model_fill(rows, "buy", 100, 1000, T0 + 150, T0 + 900, fill_share=10) == 0.5 * 100 + 40
    assert model_fill(rows, "buy", 100, 60, T0 + 150, T0 + 900, fill_share=10) == 60  # never more than the order
    assert model_fill(rows, "sell", 109, 1000, T0, T0 + 900, fill_share=10) == 150  # buyers paid 110 in all three
    assert model_fill(rows, "sell", 110, 1000, T0, T0 + 900, fill_share=10) == 0
    assert model_fill(rows, "sell", 110, 1000, T0, T0 + 900, fill_share=10, strict=False) == 150


def test_fill_reality_and_sizing_scales():
    def order(item, filled, price, model, side="buy"):
        return SimpleNamespace(item_id=item, filled=filled, price=price, model_fill=model, side=side)

    few = [order(1, 50, 10, 100.0), order(2, 0, 10, None)]
    r = fill_reality(few, side="buy")
    assert (r.ratio, r.orders) == (0.5, 1)
    assert sizing_scales(r) == (1.0, {})  # one order proves nothing: sizes stay as they are

    many = [order(1, 30, 10, 100.0) for _ in range(6)] + [order(2, 100, 10, 100.0) for _ in range(6)] + [order(3, 5, 10, 0.0)]
    r = fill_reality(many, side="buy")
    assert r.orders == 13 and abs(r.ratio - (6 * 30 + 6 * 100 + 5) / 1200) < 1e-9
    default, per = sizing_scales(r)
    assert abs(default - r.ratio) < 1e-9
    assert per == {1: 0.3, 2: 1.0}  # item 3 has a single order: not enough to single it out
    assert fill_reality([order(1, 0, 10, 100.0)] * 12).ratio == 0.0 and sizing_scales(fill_reality([order(1, 0, 10, 100.0)] * 12))[0] == 0.1
    assert fill_reality(many, side="sell").orders == 0
