import pytest

from ge_analyser.analysis import ItemStats, ask_for_margin, capped_ask, ge_tax
from ge_analyser.api import ItemInfo, LatestPrice, WindowPrice
from ge_analyser.executor import ExecConfig, ExecOrder, ExecState, Executor, Market, StatusError, easy_quantity, parse_status

ORE, SALMON, WHIP, DEATH = 440, 329, 4151, 560
ITEMS = {
    ORE: ItemInfo(ORE, "Iron ore", False, 13000, 0, 0),
    SALMON: ItemInfo(SALMON, "Salmon", False, 6000, 0, 0),
    WHIP: ItemInfo(WHIP, "Abyssal whip", True, 70, 0, 0),
    DEATH: ItemInfo(DEATH, "Death rune", True, 25000, 0, 0),
}
T0 = 1_700_000_000
ORE_ONLY = {ORE: (100, 110)}  # instant-sell 100, instant-buy 110 -> bid 101, ask 109, +6/ea after tax
BOTH = {ORE: (100, 110), SALMON: (50, 56)}


def market(prices, now, stats=None, vol=10_000):
    latest = {i: LatestPrice(high, now, low, now) for i, (low, high) in prices.items()}
    return Market(latest=latest, hour={i: WindowPrice(None, vol, None, vol) for i in prices}, stats=stats)


def engine(**cfg):
    # Exact order sizes unless a test asks for rounding: what is checked here is the book, not the arithmetic.
    return Executor(ExecState(), ITEMS, ExecConfig(**{"round_quantities": False, **cfg}))


def status(cash, orders=(), now=T0, **extra):
    return parse_status({"cash": cash, "orders": list(orders), **extra}, ITEMS, now)


def acts(plan, kind):
    return [a for a in plan.actions if a["action"] == kind]


# ---- status parsing ----------------------------------------------------------


def test_parse_status_accepts_ids_names_and_synonyms():
    s = parse_status(
        {
            "cash": "10k",
            "f2p": True,
            "slots": 3,
            "orders": [
                {"order_id": 4, "status": "Executed"},
                {"order": 5, "state": "partially filled", "filled": 12},
                {"item": "iron ore", "state": "SELLING", "filled": 3, "price": 109, "qty": 10},
                {"item_id": SALMON, "side": "buy"},
                {"item": "Salmon", "state": "EMPTY"},
            ],
            "inventory": [{"item": "Coins", "qty": 5000}, {"item": "Iron ore", "qty": 7}, {"item": "Lucky charm", "qty": 1}],
        },
        ITEMS,
        T0,
    )
    assert s.cash == 10_000 and s.members is False and s.slots == 3
    assert [(r.order_id, r.side, r.item_id, r.status, r.filled) for r in s.orders] == [
        (4, None, None, "executed", None),
        (5, None, None, "filling", 12),
        (None, "sell", ORE, "filling", 3),
        (None, "buy", SALMON, "open", None),
    ]
    assert s.inventory[ORE] == 7 and len(s.inventory) == 2  # coins skipped; the untradeable charm still takes a stack
    assert parse_status({"cash": 5, "inventory": {"Iron ore": 3}}, ITEMS, T0).inventory == {ORE: 3}


@pytest.mark.parametrize(
    "raw, needle",
    [
        ({"cash": "lots", "orders": []}, "cash must be a number"),
        ({"cash": 1, "orders": [{"order_id": 1, "status": "maybe"}]}, "unknown status"),
        ({"cash": 1, "orders": [{"item": "Iron oar", "side": "buy"}]}, "unknown item name"),
        ({"cash": 1, "orders": [{"status": "open"}]}, "order_id"),
        ([1, 2], "JSON object"),
    ],
)
def test_parse_status_says_what_to_fix(raw, needle):
    with pytest.raises(StatusError, match=needle):
        parse_status(raw, ITEMS, T0)


# ---- planning ------------------------------------------------------------------


def test_first_cycle_buys_within_slots_cash_and_improves_prices():
    eng = engine()
    plan = eng.step(status(10_000), market(BOTH, T0), T0)
    buys = acts(plan, "BUY")
    assert {b["item"] for b in buys} == {"Iron ore", "Salmon"}
    ore = next(b for b in buys if b["item_id"] == ORE)
    assert (ore["price"], ore["then_sell_at"], ore["profit_each"]) == (101, 109, 6)  # 1 gp better on both legs
    assert sum(b["price"] * b["qty"] for b in buys) <= 10_000
    assert [a["step"] for a in plan.actions] == list(range(1, len(plan.actions) + 1))
    assert {o["order_id"] for o in plan.reply_template["orders"]} == {b["order_id"] for b in buys}
    assert plan.account["ge_slots_used"] == 2 and plan.account["inventory_stacks"] == 2


def test_fills_come_only_from_status_and_sell_follows_buy():
    eng = engine()
    plan = eng.step(status(10_000, slots=1), market(ORE_ONLY, T0), T0)
    (buy,) = acts(plan, "BUY")
    assert (buy["qty"], buy["price"]) == (99, 101)  # all the cash, one slot
    assert not eng.state.positions  # nothing is owned until the status says so

    t = T0 + 60
    plan = eng.step(status(1, [{"order_id": buy["order_id"], "status": "executed"}], t, slots=1), market(ORE_ONLY, t), t)
    assert [a["action"] for a in plan.actions] == ["COLLECT", "SELL"]
    sell = acts(plan, "SELL")[0]
    assert (sell["qty"], sell["price"], sell["cost_each"]) == (99, 109, 101.0)
    assert [f.side for f in plan.fills] == ["buy"] and plan.fills[0].qty == 99

    t += 60
    plan = eng.step(status(1, [{"order_id": sell["order_id"], "status": "filling", "filled": 40}], t, slots=1), market(ORE_ONLY, t), t)
    assert [a["action"] for a in plan.actions] == ["HOLD"]
    assert eng.state.realised == 40 * (109 - ge_tax(109) - 101) == 240

    t += 60
    cash = 1 + 99 * (109 - 2)
    plan = eng.step(status(cash, [{"order_id": sell["order_id"], "status": "sold"}], t, slots=1), market(ORE_ONLY, t), t)
    assert eng.state.realised == 99 * 6 and not eng.state.positions
    assert [a["action"] for a in plan.actions] == ["COLLECT", "BUY"]  # the freed slot is put back to work at once
    assert eng.state.sells == 2 and eng.state.wins == 2


def test_coins_are_counted_from_reports_and_cash_is_only_a_correction():
    eng = engine()
    with pytest.raises(StatusError, match="first status needs"):
        eng.step(status(None), market(ORE_ONLY, T0), T0)
    assert eng.state.seq == 0  # a status that cannot be used changes nothing

    (buy,) = acts(eng.step(status(10_000, slots=2), market(ORE_ONLY, T0), T0), "BUY")
    assert (buy["qty"], buy["price"]) == (50, 101)
    oid, t = buy["order_id"], T0

    def coins(orders, cash=None):
        nonlocal t
        t += 60
        plan = eng.step(status(cash, orders, t), market(ORE_ONLY, t), t)
        return plan.account["cash"], plan

    assert coins([])[0] == 10_000  # instructed, not placed: nothing is locked yet
    assert coins([{"order_id": oid, "status": "open"}])[0] == 4_950  # 5,050 went into the offer
    assert coins([{"order_id": oid, "status": "filling", "filled": 20}])[0] == 4_950  # bought with coins already locked
    got, plan = coins([{"order_id": oid, "status": "cancelled", "filled": 20}])
    assert got == 4_950 + 30 * 101  # the unspent part of an aborted buy comes back
    (sell,) = acts(plan, "SELL")
    assert (sell["qty"], sell["price"]) == (20, 109)
    # The same stale "cash" line left in the file says nothing new: the count stands.
    assert coins([{"order_id": sell["order_id"], "status": "filling", "filled": 5}], cash=10_000)[0] == 7_980 + 5 * 107
    got, plan = coins([{"order_id": sell["order_id"], "status": "sold"}])
    assert got == 7_980 + 20 * 107 == 10_000 + eng.state.realised  # nothing in offers or stock: start + profit
    assert plan.account["cash_after_actions"] == got - sum(a["price"] * a["qty"] for a in acts(plan, "BUY"))
    assert "cash" not in plan.reply_template  # copying the template back can never feed in a stale count
    # A changed figure is a correction, and the answer says by how much.
    got, plan = coins([], cash=9_000)
    assert (got, plan.account["cash_correction"]) == (9_000, 9_000 - 10_120)


def test_executed_without_a_count_is_a_full_fill():
    for report in ({"status": "executed"}, {"status": "executed", "filled": 0}):
        eng = engine()
        (buy,) = acts(eng.step(status(10_000, slots=1), market(ORE_ONLY, T0), T0), "BUY")
        eng.step(status(1, [{"order_id": buy["order_id"], **report}], T0 + 60, slots=1), market(ORE_ONLY, T0 + 60), T0 + 60)
        assert eng.state.positions[ORE].qty == 99
    # ...while a real smaller count means the offer placed was smaller
    eng = engine()
    (buy,) = acts(eng.step(status(10_000, slots=1), market(ORE_ONLY, T0), T0), "BUY")
    eng.step(status(1, [{"order_id": buy["order_id"], "status": "executed", "filled": 60}], T0 + 60, slots=1), market(ORE_ONLY, T0 + 60), T0 + 60)
    assert eng.state.positions[ORE].qty == 60


def test_instruction_repeats_unchanged_until_seen_then_is_replanned():
    eng = engine(pending_ttl=180)
    (first,) = acts(eng.step(status(10_000, slots=1), market(ORE_ONLY, T0), T0), "BUY")
    moved = {ORE: (101, 111)}  # the market ticks up; the instruction you are typing in must not change under you
    (again,) = acts(eng.step(status(10_000, slots=1, now=T0 + 60), market(moved, T0 + 60), T0 + 60), "BUY")
    assert (again["order_id"], again["price"], again["qty"]) == (first["order_id"], first["price"], first["qty"])
    assert "not in your status yet" in again["why"]
    (later,) = acts(eng.step(status(10_000, slots=1, now=T0 + 200), market(moved, T0 + 200), T0 + 200), "BUY")
    assert later["order_id"] != first["order_id"] and later["price"] == 102  # re-planned at today's price
    # A withdrawn instruction that was placed after all is taken back on the book.
    plan = eng.step(status(20_000, [{"order_id": first["order_id"], "status": "open"}], T0 + 260, slots=2), market(moved, T0 + 260), T0 + 260)
    assert [o.id for o in eng.state.orders if o.state == "working"] == [first["order_id"]]
    assert not acts(plan, "BUY")  # ...and the replacement instruction for the same item is dropped, not doubled up


def test_buy_timeout_cancels_and_reuses_the_coins_in_the_same_batch():
    eng = engine(buy_timeout=1800)
    (first,) = acts(eng.step(status(10_000, slots=1), market(BOTH, T0), T0), "BUY")
    left = 10_000 - first["price"] * first["qty"]
    open_report = [{"order_id": first["order_id"], "status": "open"}]
    t = T0 + 60
    plan = eng.step(status(left, open_report, t, slots=1), market(BOTH, t), t)
    assert [a["action"] for a in plan.actions] == ["HOLD"]
    t += 1800
    plan = eng.step(status(left, open_report, t, slots=1), market(BOTH, t), t)
    assert [a["action"] for a in plan.actions] == ["CANCEL", "BUY"]
    cancel, new = plan.actions
    assert cancel["order_id"] == first["order_id"] and "no fill" in cancel["why"]
    assert new["item_id"] != first["item_id"]  # the stale item is not re-bid in the same breath
    assert left < new["price"] * new["qty"] <= 10_000  # sized with the refund, not just the loose coins
    # Still open next time: the cancel is repeated, not forgotten.
    t += 60
    plan = eng.step(status(left, open_report, t, slots=1), market(BOTH, t), t)
    assert acts(plan, "CANCEL")[0]["order_id"] == first["order_id"]
    # Reported cancelled: it leaves the book and nothing was bought.
    t += 60
    eng.step(status(10_000, [{"order_id": first["order_id"], "status": "cancelled"}], t, slots=1), market(BOTH, t), t)
    assert all(o.id != first["order_id"] for o in eng.state.orders) and not eng.state.positions


def test_a_big_stack_gives_an_unfilled_buy_more_time():
    def first_abort(cash, **cfg):
        eng = engine(buy_timeout=1800, buy_timeout_big=3600, **cfg)
        (buy,) = acts(eng.step(status(cash, slots=1), market(ORE_ONLY, T0), T0), "BUY")
        report = [{"order_id": buy["order_id"], "status": "open"}]
        eng.step(status(None, report, T0 + 60, slots=1), market(ORE_ONLY, T0 + 60), T0 + 60)
        for minutes in range(5, 125, 5):
            t = T0 + 60 + minutes * 60
            if acts(eng.step(status(None, report, t, slots=1), market(ORE_ONLY, t), t), "CANCEL"):
                return minutes

    assert first_abort(10_000, big_stack=100_000) == 30
    assert first_abort(100_000, big_stack=100_000) == 60  # equity counts the coins locked in the offer too
    assert first_abort(100_000, big_stack=0) == 30  # 0: the same timeout whatever you have


@pytest.mark.parametrize("cancel_on_margin", [True, False])
def test_buy_is_cancelled_when_the_margin_is_gone(cancel_on_margin):
    eng = engine(cancel_on_margin=cancel_on_margin)
    (buy,) = acts(eng.step(status(10_000, slots=1), market(ORE_ONLY, T0), T0), "BUY")
    report = [{"order_id": buy["order_id"], "status": "open"}]
    t = T0 + 60
    assert acts(eng.step(status(1, report, t, slots=1), market(ORE_ONLY, t), t), "HOLD")
    t += 60
    plan = eng.step(status(1, report, t, slots=1), market({ORE: (95, 102)}, t), t)  # buyers now pay 102: 102 - 2 - 101 < 0
    if cancel_on_margin:
        assert "margin gone" in acts(plan, "CANCEL")[0]["why"]
    else:
        assert [a["action"] for a in plan.actions] == ["HOLD"]


def test_sell_is_repriced_then_dumped():
    eng = engine(sell_reprice=1800, sell_dump=5400, dump_retry=600)
    (buy,) = acts(eng.step(status(10_000, slots=1), market(ORE_ONLY, T0), T0), "BUY")
    t = T0 + 60
    (sell,) = acts(eng.step(status(1, [{"order_id": buy["order_id"], "status": "bought"}], t, slots=1), market(ORE_ONLY, t), t), "SELL")
    listed = t + 60
    working = [{"order_id": sell["order_id"], "status": "open"}]
    eng.step(status(1, working, listed, slots=1), market(ORE_ONLY, listed), listed)

    t = listed + 1800
    plan = eng.step(status(1, working, t, slots=1), market({ORE: (99, 105)}, t), t)  # no sale for 30 min, buyers pay 105
    assert [a["action"] for a in plan.actions] == ["CANCEL", "SELL"]
    relist = acts(plan, "SELL")[0]
    assert relist["price"] == 104 and relist["qty"] == 99 and relist["order_id"] != sell["order_id"]

    t += 60
    reports = [{"order_id": sell["order_id"], "status": "cancelled", "filled": 0}, {"order_id": relist["order_id"], "status": "open"}]
    plan = eng.step(status(1, reports, t, slots=1), market({ORE: (99, 105)}, t), t)
    assert [a["action"] for a in plan.actions] == ["COLLECT", "HOLD"]  # repriced once; now it waits

    t = listed + 5400
    plan = eng.step(status(1, reports[1:], t, slots=1), market({ORE: (98, 104)}, t), t)
    dump = acts(plan, "SELL")[0]
    assert "instant-sell" in acts(plan, "CANCEL")[0]["why"] and dump["price"] == 98
    assert dump["profit_each"] < 0  # cutting a loser is a loss, and is reported as one


def test_relist_is_dropped_when_the_old_offer_sold_before_it_was_aborted():
    eng = engine(sell_reprice=1800)
    (buy,) = acts(eng.step(status(10_000, slots=1), market(ORE_ONLY, T0), T0), "BUY")
    t = T0 + 60
    (sell,) = acts(eng.step(status(1, [{"order_id": buy["order_id"], "status": "bought"}], t, slots=1), market(ORE_ONLY, t), t), "SELL")
    listed = t + 60
    working = [{"order_id": sell["order_id"], "status": "open"}]
    eng.step(status(1, working, listed, slots=1), market(ORE_ONLY, listed), listed)
    t = listed + 1800
    plan = eng.step(status(1, working, t, slots=1), market({ORE: (99, 105)}, t), t)
    assert [a["action"] for a in plan.actions] == ["CANCEL", "SELL"]

    t += 60  # ...but it sold at the old price before you got to it
    plan = eng.step(status(1, [{"order_id": sell["order_id"], "status": "sold"}], t, slots=1), market({ORE: (99, 105)}, t), t)
    assert not eng.state.positions and [o.side for o in eng.state.orders] == ["buy"]  # the relist instruction is gone with the stock
    assert [a["action"] for a in plan.actions] == ["COLLECT", "BUY"]  # and the item is free to trade again


def test_partial_fills_are_listed_only_once_they_matter():
    eng = engine(partial_sell_frac=0.2)
    (buy,) = acts(eng.step(status(10_000, slots=2), market(ORE_ONLY, T0), T0), "BUY")
    assert buy["qty"] == 50
    t = T0 + 60
    plan = eng.step(status(4950, [{"order_id": buy["order_id"], "status": "filling", "filled": 5}], t, slots=2), market(ORE_ONLY, t), t)
    assert not acts(plan, "SELL")
    assert any(a.get("side") == "inventory" and "bought so far" in a["why"] for a in acts(plan, "HOLD"))
    t += 60
    plan = eng.step(status(4950, [{"order_id": buy["order_id"], "status": "filling", "filled": 12}], t, slots=2), market(ORE_ONLY, t), t)
    (sell,) = acts(plan, "SELL")
    assert sell["qty"] == 12 and sell["collect_first"] == [buy["order_id"]]
    # More units arrive while that sell works: they wait, one sell offer per item.
    t += 60
    reports = [{"order_id": buy["order_id"], "status": "filling", "filled": 30}, {"order_id": sell["order_id"], "status": "open"}]
    plan = eng.step(status(4950, reports, t, slots=2), market(ORE_ONLY, t), t)
    assert not acts(plan, "SELL") and any("18 more wait" in a["why"] for a in acts(plan, "HOLD"))


# ---- constraints ------------------------------------------------------------------


def test_inventory_capacity_limits_new_item_stacks():
    junk = [{"item": f"Junk {i}", "qty": 1} for i in range(26)]
    eng = engine()
    plan = eng.step(status(10_000, inventory=junk), market(BOTH, T0), T0)
    assert not acts(plan, "BUY") and plan.account["inventory_stacks"] == 26
    eng = engine()
    plan = eng.step(status(10_000, inventory=junk, inventory_capacity=27), market(BOTH, T0), T0)
    assert len(acts(plan, "BUY")) == 1  # one free stack: one new item, not two


def test_slots_are_shared_with_offers_the_engine_did_not_place():
    eng = engine()
    foreign = [
        {"item": "Abyssal whip", "side": "buy", "status": "open", "price": 1, "qty": 1},
        {"item": "Death rune", "side": "sell", "status": "filling", "filled": 3},
    ]
    plan = eng.step(status(10_000, foreign, slots=3), market(BOTH, T0), T0)
    assert len(acts(plan, "BUY")) == 1
    assert sum("not placed by this engine" in a["why"] for a in acts(plan, "HOLD")) == 2
    assert plan.account["ge_slots_used"] == 3


def test_free_to_play_status_limits_items_and_slots():
    eng = engine()
    prices = {ORE: (100, 110), DEATH: (200, 220), SALMON: (50, 56), WHIP: (1000, 1100)}
    plan = eng.step(status(1_000_000, members=False), market(prices, T0), T0)
    assert {b["item"] for b in acts(plan, "BUY")} == {"Iron ore", "Salmon"}
    assert plan.account["ge_slots"] == 3 and plan.account["items_allowed"] == "free-to-play"


def test_account_settings_stick_and_the_reply_template_is_a_complete_status():
    eng = engine()
    prices = {ORE: (100, 110), DEATH: (200, 220), SALMON: (50, 56)}
    plan = eng.step(status(1_000_000, members=False, inventory_capacity=20), market(prices, T0), T0)
    tpl = plan.reply_template
    assert (tpl["members"], tpl["slots"], tpl["inventory_capacity"]) == (False, 3, 20)
    # Feed the template back exactly as given: nothing about the account is lost.
    t = T0 + 60
    plan = eng.step(parse_status(tpl, ITEMS, t), market(prices, t), t)
    assert (plan.account["ge_slots"], plan.account["items_allowed"], plan.account["inventory_capacity"]) == (3, "free-to-play", 20)
    # A bare status (cash + orders) keeps them too...
    t += 60
    plan = eng.step(status(1_000_000, tpl["orders"], t), market(prices, t), t)
    assert (plan.account["ge_slots"], plan.account["items_allowed"]) == (3, "free-to-play")
    assert not any(a["item"] == "Death rune" for a in acts(plan, "BUY"))
    # ...until a status changes them.
    t += 60
    plan = eng.step(status(1_000_000, tpl["orders"], t, members=True, slots=8), market(prices, t), t)
    assert (plan.account["ge_slots"], plan.account["items_allowed"]) == (8, "all")
    assert [a["item"] for a in acts(plan, "BUY")] == ["Death rune"]


def _with_97k_in_a_buy_offer(**cfg):
    """An engine whose book already has 97,000 gp locked in one working buy offer."""
    eng = engine(**cfg)
    eng.state.orders.append(ExecOrder(id=1, side="buy", item_id=WHIP, name="Abyssal whip", price=97_000, qty=1, issued=T0, state="working", seen=T0, last_progress=T0))
    eng.state.next_id = 2
    return eng, [{"order_id": 1, "status": "open"}]


def test_small_change_is_not_spread_over_dust_orders():
    # 3,000 gp loose on a 100k stack with 7 free slots: one order worth having, not seven worth 400 gp each.
    eng, book = _with_97k_in_a_buy_offer()
    (buy,) = acts(eng.step(status(3_000, book), market(BOTH, T0), T0), "BUY")
    assert buy["price"] * buy["qty"] >= 2_000  # at least 2% of equity
    eng, book = _with_97k_in_a_buy_offer(min_order_frac=0.0)
    assert len(acts(eng.step(status(3_000, book), market(BOTH, T0), T0), "BUY")) == 2  # the floor is what concentrates it
    # Too little to make one worthwhile order: wait for coins rather than nibble.
    eng, book = _with_97k_in_a_buy_offer()
    plan = eng.step(status(600, book), market(BOTH, T0), T0)
    assert [a["action"] for a in plan.actions] == ["HOLD"]
    # An order the market keeps small is not dust: a 100m stack still trades what the volume allows.
    buys = acts(engine(order_hours=0.5, fill_share=10).step(status(100_000_000), market(BOTH, T0), T0), "BUY")
    # Half an hour of a tenth of the 10,000 traded an hour: 500 each, ~0.05% of the stack.
    assert [(b["item"], b["qty"]) for b in buys] == [("Iron ore", 500), ("Salmon", 500)]


def test_no_buy_without_a_chance_of_filling():
    never = ItemStats(windows=500, fill_buy=0.0, fill_sell=0.5, median_margin=3, volatility_pct=0.5, typical_volume=10_000)
    plan = engine().step(status(10_000), market(ORE_ONLY, T0, stats={ORE: never}), T0)
    assert not plan.actions and plan.account["ge_slots_used"] == 0


# ---- statuses written by the RuneLite plugin: no order ids, offers named by `ref` ------


def rl(slot, ref, state, price, qty, filled=0, since=0, spent=None, collected=False, item=ORE):
    """One GE slot as the plugin describes it."""
    out = {"slot": slot, "ref": ref, "since": since, "status": state, "item_id": item, "price": price, "qty": qty, "filled": filled,
           "spent": price * filled if spent is None else spent}
    return {**out, "collected": True} if collected else out


def live(cash, offers=(), now=T0, inventory=(), slots=1):
    raw = {"source": "runelite", "cash": cash, "members": True, "slots": slots, "orders": list(offers), "inventory": list(inventory)}
    return parse_status(raw, ITEMS, now)


def test_plugin_status_is_read_with_refs_and_exact_coins():
    s = live(5, [rl(2, "2-9", "CANCELLED_SELL", 46, 20, filled=5, since=77, collected=True)])
    (r,) = s.orders
    assert s.live and (r.side, r.status, r.ref, r.since, r.slot, r.collected) == ("sell", "cancelled", "2-9", 77, 2, True)
    assert not status(5).live


def test_a_status_for_another_account_is_refused_and_changes_nothing():
    eng = engine()
    eng.step(status(10_000, account="Zezima"), market(ORE_ONLY, T0), T0)
    with pytest.raises(StatusError, match="session belongs to 'Zezima'"):
        eng.step(status(5, account="Someone else"), market(ORE_ONLY, T0 + 60), T0 + 60)
    assert eng.state.seq == 1 and eng.state.orders[0].state == "pending"
    eng.step(status(10_000), market(ORE_ONLY, T0 + 120), T0 + 120)  # a status that names nobody is the session's own
    assert eng.state.seq == 2


def test_a_whole_flip_from_plugin_statuses_without_order_ids():
    eng = engine()
    (buy,) = acts(eng.step(live(10_000), market(ORE_ONLY, T0), T0), "BUY")
    assert (buy["qty"], buy["price"]) == (99, 101)
    t = T0

    def step(cash, offers, inventory=()):
        nonlocal t
        t += 60
        return eng.step(live(cash, offers, t, inventory), market(ORE_ONLY, t), t)

    # The offer appears in slot 0 after the instruction was given: it is that instruction.
    plan = step(1, [rl(0, "0-a", "BUYING", 101, 99, since=T0 + 30)])
    assert [a["action"] for a in plan.actions] == ["HOLD"] and eng.state.orders[0].ref == "0-a"
    # 40 bought a coin under the bid: `spent` gives the real cost.
    step(1, [rl(0, "0-a", "BUYING", 101, 99, filled=40, spent=4_000, since=T0 + 30)])
    assert (eng.state.positions[ORE].qty, eng.state.positions[ORE].cost) == (40, 100.0)
    done = rl(0, "0-a", "BOUGHT", 101, 99, filled=99, spent=9_959, since=T0 + 30)
    plan = step(1, [done])
    (sell,) = acts(plan, "SELL")
    assert [a["action"] for a in plan.actions] == ["COLLECT", "SELL"] and (sell["qty"], sell["price"]) == (99, 109)
    # Still sitting in its slot a minute later: nothing is booked twice, the COLLECT is repeated.
    plan = step(1, [done])
    assert [(a["action"], a["order_id"]) for a in plan.actions] == [("COLLECT", buy["order_id"]), ("SELL", sell["order_id"])]
    assert eng.state.positions[ORE].qty == 99
    # Collected (the plugin keeps the finished offer listed as `collected`) and the sell placed in the freed slot.
    kept = {**done, "collected": True}
    plan = step(41, [rl(0, "0-b", "SELLING", 109, 99, since=t + 50), kept])
    assert [a["action"] for a in plan.actions] == ["HOLD"] and eng.state.orders[0].ref == "0-b"
    sold = rl(0, "0-b", "SOLD", 109, 99, filled=99, since=t - 10)
    plan = step(41, [sold, kept])
    assert [a["action"] for a in plan.actions] == ["COLLECT"]  # the coins are still in the offer: nothing to buy with yet
    assert eng.state.realised == 99 * 107 - 9_959 and not eng.state.positions
    assert (plan.account["cash"], plan.account["cash_correction"]) == (41, 0)  # the inventory's coins, taken as they are
    plan = step(41 + 99 * 107, [{**sold, "collected": True}, kept])
    assert [a["action"] for a in plan.actions] == ["BUY"] and plan.account["cash"] == 10_634


def test_a_sell_of_everything_you_hold_is_marked_all():
    eng = engine()
    eng.step(live(10_000), market(ORE_ONLY, T0), T0)  # asks for 99 @ 101
    done = rl(0, "0-a", "BOUGHT", 101, 99, filled=99, since=T0 + 30)
    t = T0 + 60
    (sell,) = acts(eng.step(live(1, [done], t), market(ORE_ONLY, t), t), "SELL")
    assert "all" not in sell  # still in the offer: none of it is in your inventory yet
    t += 60
    kept = {**done, "collected": True}
    (sell,) = acts(eng.step(live(1, [kept], t, [{"item_id": ORE, "qty": 99}]), market(ORE_ONLY, t), t), "SELL")
    assert sell["all"] is True and sell["qty"] == 99  # the GE's All button is the whole quantity
    t += 60
    (sell,) = acts(eng.step(live(1, [kept], t, [{"item_id": ORE, "qty": 150}]), market(ORE_ONLY, t), t), "SELL")
    assert "all" not in sell and sell["qty"] == 99  # 51 of your own on top: this one has to be entered
    t += 60
    (sell,) = acts(eng.step(status(None, [], t), market(ORE_ONLY, t), t), "SELL")
    assert "all" not in sell  # no inventory in the status: nothing is claimed about it


def test_a_finished_offer_left_in_its_slot_never_becomes_a_new_instructions_fill():
    for since in (0, T0 - 100):  # nobody saw it placed / it was placed before the instruction
        eng = engine()
        stale = rl(0, "0-old", "BOUGHT", 101, 50, filled=50, since=since)
        plan = eng.step(live(10_000, [stale], slots=2), market(ORE_ONLY, T0), T0)
        (buy,) = acts(plan, "BUY")
        assert (buy["qty"], buy["price"]) == (50, 101)  # the very same offer is asked for again
        assert acts(plan, "COLLECT")[0]["why"].startswith("a finished offer this engine did not place")
        t = T0 + 60
        plan = eng.step(live(10_000, [stale], t, slots=2), market(ORE_ONLY, t), t)
        assert not eng.state.positions and not plan.fills and eng.state.orders[0].state == "pending"
        assert [a["action"] for a in plan.actions] == ["COLLECT", "BUY"]
        # Collected, and the instruction carried out: now there is an offer to follow.
        t += 60
        eng.step(live(10_000 - 50 * 101, [rl(0, "0-new", "BUYING", 101, 50, since=t - 20)], t, slots=2), market(ORE_ONLY, t), t)
        assert eng.state.orders[0].state == "working" and eng.state.orders[0].ref == "0-new"


def test_an_offer_placed_with_one_unit_more_is_followed_as_it_was_placed():
    # The GE's + buttons can leave you one over a round number: 401 for 400.
    eng = engine()
    (buy,) = acts(eng.step(live(1_000_000), market(ORE_ONLY, T0), T0), "BUY")
    q, t = buy["qty"] + 1, T0 + 60
    placed = rl(0, "0-a", "BUYING", 101, q, since=T0 + 30)
    plan = eng.step(live(1_000_000 - 101 * q, [placed], t), market(ORE_ONLY, t), t)
    (o,) = eng.state.orders
    assert (o.id, o.state, o.qty, o.ref) == (buy["order_id"], "working", q, "0-a")
    assert [(a["action"], a["qty"]) for a in plan.actions] == [("HOLD", q)]
    t += 60
    done = {**placed, "status": "BOUGHT", "filled": q, "spent": 101 * q, "collected": True}
    (sell,) = acts(eng.step(live(1_000_000 - 101 * q, [done], t, [{"item_id": ORE, "qty": q}]), market(ORE_ONLY, t), t), "SELL")
    assert (sell["qty"], sell["all"]) == (q, True) and eng.state.positions[ORE].qty == q


def test_offers_you_placed_yourself_are_left_alone_and_their_items_avoided():
    eng = engine()
    mine = rl(3, "3-x", "BUYING", 90, 10)
    plan = eng.step(live(10_000, [mine], slots=8), market(BOTH, T0), T0)
    assert [a["item"] for a in acts(plan, "BUY")] == ["Salmon"]  # not a second Iron ore offer on top of yours
    assert acts(plan, "HOLD")[0]["why"].startswith("not placed by this engine") and plan.account["ge_slots_used"] == 2


def test_a_running_offer_with_exactly_the_instructed_price_and_size_is_adopted():
    for qty, adopted in ((99, True), (50, False)):
        eng = engine()
        eng.step(live(10_000), market(ORE_ONLY, T0), T0)  # asks for 99 @ 101
        t = T0 + 60
        plan = eng.step(live(1, [rl(0, "0-u", "BUYING", 101, qty)], t), market(ORE_ONLY, t), t)  # since unknown
        # Another size, placed nobody knows when: yours, not the engine's. It fills the only slot, so the instruction is dropped.
        assert [o.state for o in eng.state.orders] == (["working"] if adopted else [])
        assert plan.actions[0]["why"].startswith("not placed by this engine") is not adopted


def test_an_instruction_withdrawn_a_moment_ago_and_placed_after_all_is_taken_back():
    eng = engine(pending_ttl=180)
    (first,) = acts(eng.step(live(10_000), market(ORE_ONLY, T0), T0), "BUY")
    moved = {ORE: (101, 111)}
    t = T0 + 200
    (second,) = acts(eng.step(live(10_000, [], t), market(moved, t), t), "BUY")
    assert second["order_id"] != first["order_id"] and second["price"] == 102  # re-planned at the new price
    # ...but you had typed in the first one in the meantime.
    t += 60
    plan = eng.step(live(1, [rl(0, "0-late", "BUYING", 101, 99, since=T0 + 150)], t), market(moved, t), t)
    (o,) = eng.state.orders
    assert (o.id, o.state, o.ref) == (first["order_id"], "working", "0-late")
    assert [a["action"] for a in plan.actions] == ["HOLD"]


def test_missing_order_is_asked_about_then_assumed_cancelled():
    eng = engine(missing_grace=120)
    (buy,) = acts(eng.step(status(10_000, slots=1), market(BOTH, T0), T0), "BUY")
    left = 10_000 - buy["price"] * buy["qty"]
    t = T0 + 60
    eng.step(status(left, [{"order_id": buy["order_id"], "status": "filling", "filled": 10}], t, slots=1), market(BOTH, t), t)
    t += 60
    plan = eng.step(status(left, [], t, slots=1), market(BOTH, t), t)
    (ask,) = [a for a in acts(plan, "HOLD") if a.get("needs_status")]
    assert ask["order_id"] == buy["order_id"] and not acts(plan, "BUY")  # its slot is not given away on a guess
    t += 120
    plan = eng.step(status(left, [], t, slots=1), market(BOTH, t), t)
    assert any("assumed cancelled at 10/" in w for w in plan.warnings)
    assert eng.state.positions[buy["item_id"]].qty == 10  # what was known to be bought is kept and now sold
    assert acts(plan, "SELL")[0]["qty"] == 10


def test_missing_order_is_settled_from_the_inventory_when_reported():
    eng = engine()
    (buy,) = acts(eng.step(status(10_000, slots=1), market(ORE_ONLY, T0), T0), "BUY")
    t = T0 + 60
    eng.step(status(1, [{"order_id": buy["order_id"], "status": "filling", "filled": 10}], t, slots=1), market(ORE_ONLY, t), t)
    t += 60
    plan = eng.step(status(1, [], t, slots=1, inventory=[{"item": "Iron ore", "qty": 99}]), market(ORE_ONLY, t), t)
    assert eng.state.positions[ORE].qty == 99 and not plan.warnings  # the stack shows it filled completely
    assert acts(plan, "SELL")[0]["qty"] == 99


# ---- outlier guards ------------------------------------------------------------------


def test_quantities_are_rounded_to_one_or_two_digits_that_are_not_zero():
    plenty = 10**9
    # One digit where that is within 15% of the size wanted...
    assert [easy_quantity(q, plenty) for q in (7, 43, 62, 95, 387, 2_659, 22_600)] == [7, 40, 60, 100, 400, 3_000, 20_000]
    # ...two where it is not: 140 is neither 100 nor 200.
    assert [easy_quantity(q, plenty) for q in (14, 140, 150, 250, 1_499, 13_744)] == [14, 140, 150, 250, 1_500, 14_000]
    # Never more than your coins or the buy limit allow.
    assert [easy_quantity(q, most) for q, most in ((387, 399), (950, 950), (199, 199), (28, 28))] == [390, 900, 190, 28]
    assert easy_quantity(1, plenty) == 1 and easy_quantity(0, plenty) == 0


def test_rounded_buys_stay_within_your_coins_and_the_buy_limit():
    def buys(cash, m, slots, **cfg):
        eng = engine(order_hours=0.5, fill_share=10, **cfg)  # half an hour of a tenth of the volume: 5% of an hour's
        return [(b["item"], b["qty"]) for b in acts(eng.step(status(cash, slots=slots), m, T0), "BUY")]

    thin = market(ORE_ONLY, T0, vol=7_400)  # 5% of 7,400 units is 370
    assert buys(1_000_000, thin, 1, round_quantities=False) == [("Iron ore", 370)]
    assert buys(1_000_000, thin, 1, round_quantities=True) == [("Iron ore", 400)]  # coins to spare: up to the nearer round number
    assert buys(7_000, market(BOTH, T0), 2, round_quantities=False) == [("Salmon", 70), ("Iron ore", 33)]
    assert buys(7_000, market(BOTH, T0), 2, round_quantities=True) == [("Salmon", 70), ("Iron ore", 30)]  # 40 would cost more than is left
    whips = market({WHIP: (1000, 1100)}, T0)
    whips.used = {WHIP: 2}  # 68 of the 70 you may buy in four hours are left
    assert buys(1_000_000, whips, 1, round_quantities=False) == [("Abyssal whip", 68)]
    assert buys(1_000_000, whips, 1, round_quantities=True) == [("Abyssal whip", 60)]


def test_ask_for_margin_and_cap():
    assert ask_for_margin(100, 6) == 108  # 108 - 2 tax - 100
    assert ask_for_margin(10, 2) == 12  # under 50 gp: untaxed
    assert ask_for_margin(100, 6, "Old school bond") == 106  # exempt
    big = ask_for_margin(1_000_000, 20_000)
    assert big - ge_tax(big) - 1_000_000 >= 20_000 > (big - 1) - ge_tax(big - 1) - 1_000_000
    usual = ItemStats(windows=500, fill_buy=0.5, fill_sell=0.5, median_margin=3, volatility_pct=0.5, typical_volume=10_000)
    assert capped_ask(49_998, 501, usual, cap=2.0) == ask_for_margin(501, 6)
    assert capped_ask(505, 501, usual, cap=2.0) == 505  # already inside the usual margin
    assert capped_ask(49_998, 501, None) == 49_998 and capped_ask(49_998, 501, usual, cap=0) == 49_998


def test_engine_ignores_freak_prints_and_items_without_history():
    usual = ItemStats(windows=500, fill_buy=0.5, fill_sell=0.5, median_margin=3, volatility_pct=0.5, typical_volume=10_000)
    eng = engine()
    # Iron ore usually spreads 3 gp; today someone paid 49,999. Salmon has no history at all.
    plan = eng.step(status(10_000), market({ORE: (100, 49_999), SALMON: (50, 56)}, T0, stats={ORE: usual}), T0)
    (buy,) = acts(plan, "BUY")
    assert buy["item"] == "Iron ore" and buy["price"] == 101
    assert buy["then_sell_at"] == ask_for_margin(101, 6) == 109  # twice the usual margin, not 49,998
    # Holding it later, the listing is capped the same way whatever the last print says.
    t = T0 + 60
    plan = eng.step(status(1, [{"order_id": buy["order_id"], "status": "executed"}], t), market({ORE: (100, 49_999)}, t, stats={ORE: usual}), t)
    assert acts(plan, "SELL")[0]["price"] == 109


def test_state_roundtrip(tmp_path):
    eng = engine()
    (buy,) = acts(eng.step(status(10_000, slots=1), market(ORE_ONLY, T0), T0), "BUY")
    eng.step(status(1, [{"order_id": buy["order_id"], "status": "filling", "filled": 30}], T0 + 60, slots=1), market(ORE_ONLY, T0 + 60), T0 + 60)
    eng.state.save(tmp_path / "state.json")
    loaded = ExecState.load(tmp_path / "state.json")
    assert loaded.positions[ORE].qty == 30 and loaded.positions[ORE].cost == 101
    assert [(o.id, o.state, o.filled) for o in loaded.orders] == [(o.id, o.state, o.filled) for o in eng.state.orders]
    assert loaded.next_id == eng.state.next_id and loaded.seq == 2
