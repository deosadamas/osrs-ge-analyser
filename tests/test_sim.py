from ge_analyser.api import ItemInfo, LatestPrice, WindowPrice
from ge_analyser.sim import Config, Simulator, State, Tick

ORE = ItemInfo(id=440, name="Iron ore", members=False, limit=13000, value=0, highalch=0)
ITEMS = {440: ORE}
T0 = 1_700_000_000


def make_sim(cash=10_000, **cfg):
    cfg.setdefault("improve", False)  # most tests pin exact instant-price offers
    state = State(start_cash=cash, cash=cash)
    return Simulator(state, ITEMS, Config(**cfg)), state


def market(low, high, vol=1000):
    return {440: LatestPrice(high, T0, low, T0)}, {440: WindowPrice(None, vol, None, vol)}


def tick(ts, low, high, vol=1000):
    """A print at `ts`, with the volumes of a 5-minute window that starts then."""
    return {440: Tick(high, ts, low, ts, vol, vol, window=ts)}


def test_decide_reserves_cash_and_respects_slots():
    sim, state = make_sim(slots=1)
    latest, hour = market(low=100, high=110)
    placed = sim.decide(latest, hour, T0)
    assert len(placed) == 1 and placed[0].kind == "buy" and placed[0].price == 100
    assert state.cash == 10_000 - 100 * placed[0].qty
    assert state.reserved() == 100 * placed[0].qty
    assert sim.decide(latest, hour, T0) == []  # slot full


def test_strict_buy_needs_cross_then_sell_pays_tax():
    sim, state = make_sim()
    latest, hour = market(low=100, high=110)
    (buy,) = sim.decide(latest, hour, T0)
    qty = buy.qty
    # Touching the bid does not fill in strict mode.
    sim.process_fills(tick(T0 + 60, low=100, high=110), T0 + 60)
    assert buy.filled == 0
    # Crossing below fills (volume share 1/10 of 1000 = 100 >= qty).
    sim.process_fills(tick(T0 + 120, low=99, high=110), T0 + 120)
    assert buy.filled == qty
    sells = [o for o in state.orders if o.kind == "sell"]
    assert len(sells) == 1 and sells[0].price == 110 and sells[0].cost_basis == 100
    # Sell fills only when instant-buy rises above our ask; tax is 2% of 110 = 2.
    sim.process_fills(tick(T0 + 180, low=99, high=110), T0 + 180)
    assert sells[0].filled == 0
    sim.process_fills(tick(T0 + 240, low=99, high=111), T0 + 240)
    assert state.orders == []
    assert state.cash == 10_000 + (110 - 2 - 100) * qty
    assert state.realised == 8 * qty


def test_loose_mode_fills_on_touch():
    sim, state = make_sim(strict=False)
    latest, hour = market(low=100, high=110)
    (buy,) = sim.decide(latest, hour, T0)
    sim.process_fills(tick(T0 + 60, low=100, high=110), T0 + 60)
    assert buy.filled == buy.qty


def test_partial_fills_merge_into_one_sell_order():
    sim, state = make_sim(slots=1)
    (buy,) = sim.decide(*market(low=100, high=110, vol=10_000), T0)
    assert buy.qty == 100
    sim.process_fills(tick(T0 + 60, low=99, high=110, vol=100), T0 + 60)  # fills 10
    sim.process_fills(tick(T0 + 120, low=99, high=110, vol=100), T0 + 120)  # fills 10 more
    sells = [o for o in state.orders if o.kind == "sell"]
    assert len(sells) == 1 and sells[0].remaining == 20
    assert buy.filled == 20


def test_buy_timeout_refunds_and_sell_dump_realises_loss():
    sim, state = make_sim(buy_timeout=600, sell_dump=1200)
    (buy,) = sim.decide(*market(low=100, high=110), T0)
    qty = buy.qty
    sim.expire({}, T0 + 600)
    assert state.orders == [] and state.cash == 10_000
    # Now take a position and let it rot until it is dumped at instant-sell.
    (buy,) = sim.decide(*market(low=100, high=110), T0 + 601)
    sim.process_fills(tick(T0 + 700, low=99, high=110), T0 + 700)
    sim.expire(tick(T0 + 2000, low=95, high=100), T0 + 2000)
    assert state.orders == []
    # Dumped at 95 with tax 1 against cost 100 -> -6 per unit.
    assert state.realised == -6 * qty
    assert state.cash == 10_000 - 6 * qty


def test_used_limits_include_open_buys_and_recent_fills():
    sim, state = make_sim(cash=10_000_000)
    (buy,) = sim.decide(*market(low=100, high=110, vol=200_000), T0)
    assert sim.used_limits(T0)[440] == buy.qty
    sim.process_fills(tick(T0 + 60, low=99, high=110, vol=200_000), T0 + 60)
    assert sim.used_limits(T0 + 60)[440] == buy.qty
    assert 440 not in sim.used_limits(T0 + 60 + 4 * 3600)


def test_state_roundtrip(tmp_path):
    sim, state = make_sim()
    sim.decide(*market(low=100, high=110), T0)
    state.save(tmp_path / "sim.json")
    loaded = State.load(tmp_path / "sim.json")
    assert loaded.cash == state.cash and len(loaded.orders) == 1 and loaded.orders[0].kind == "buy"


def test_improve_bids_one_better_and_fills_on_same_price_print():
    sim, state = make_sim(slots=1, improve=True)
    (buy,) = sim.decide(*market(low=100, high=110), T0)
    assert buy.price == 101  # instant-sell + 1: we are now the best bid
    # A later print at the old instant-sell (100) is below our 101 bid -> fills in strict mode.
    sim.process_fills(tick(T0 + 60, low=100, high=110), T0 + 60)
    assert buy.filled == buy.qty
    sell = next(o for o in state.orders if o.kind == "sell")
    assert sell.price == 109 and sell.cost_basis == 101
    # Improvement is skipped when it would eat the whole margin.
    sim2, _ = make_sim(slots=1, improve=True, min_margin=2)
    (tight,) = sim2.decide(*market(low=100, high=104), T0)  # raw margin 104-2-100=2; improved would be 1
    assert tight.price == 100


def test_min_stake_concentrates_small_stacks():
    sim, state = make_sim(cash=10_000, slots=8, min_stake=2_500)
    assert sim.usable_slots() == 4
    placed = sim.decide(*market(low=100, high=110), T0)
    assert len(placed) == 1  # one item in the market, but budget per slot was cash/4
    assert placed[0].qty == 25  # 2,500 / 100


def test_a_window_is_shared_out_once_however_often_it_is_looked_at():
    # A live loop looks every minute; the wiki's volume window changes every five.
    sim, state = make_sim(slots=1)
    events = []
    sim.events = events.append
    (buy,) = sim.decide(*market(low=100, high=110, vol=10_000), T0)
    assert buy.qty == 100
    w = T0 + 300
    for look in (w + 310, w + 370, w + 430):  # three looks at the same window, each with a newer print
        sim.step({440: Tick(110, look - 5, 99, look - 5, 100, 100, window=w)}, {}, {}, look)
    assert buy.filled == 10  # a tenth of the window's 100: not once per look, not twice per look
    sim.step({440: Tick(110, w + 615, 99, w + 615, 100, 100, window=w + 300)}, {}, {}, w + 620)
    assert buy.filled == 20  # the next window gives the next tenth
    # The event log says which window each fill took its share of, so a run can be audited.
    assert [(e["qty"], e["window"]) for e in events if e["kind"] == "fill"] == [(10, w), (10, w + 300)]


def test_only_the_part_of_a_window_after_the_offer_was_placed_counts():
    sim, state = make_sim(slots=1)
    (buy,) = sim.decide(*market(low=100, high=110, vol=10_000), T0)

    def crossing(window, seen):
        return {440: Tick(110, seen, 99, seen, 300, 300, window=window)}

    sim.process_fills(crossing(T0 - 300, T0 + 30), T0 + 30)  # that window had ended when the offer went in
    assert buy.filled == 0
    sim.process_fills(crossing(T0 - 200, T0 + 130), T0 + 130)  # the offer saw the last 100 s of this one
    assert buy.filled == 10  # a third of a tenth of 300
    sim.process_fills(crossing(T0 + 100, T0 + 430), T0 + 430)  # and all of this one
    assert buy.filled == 40
