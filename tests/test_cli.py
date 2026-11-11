import json
from pathlib import Path

from ge_analyser import cli
from ge_analyser.api import ItemInfo, LatestPrice, WindowPrice
from ge_analyser.calibrate import FILL_RATIO_CAP, FillReality
from ge_analyser.executor import ExecConfig, ExecState, Executor, Market, parse_status


def test_a_file_the_plugin_is_looking_at_is_written_a_moment_later(tmp_path, monkeypatch):
    target = tmp_path / "orders.json"
    target.write_text('{"seq": 1}', encoding="utf-8")
    swap = Path.replace
    refused = []

    def replace(self, dest):
        if len(refused) < 3:  # Windows: no swap while another program has the target open
            refused.append(dest)
            raise PermissionError(13, "Access is denied")
        return swap(self, dest)

    monkeypatch.setattr(Path, "replace", replace)
    cli._write_json(target, {"seq": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"seq": 2}
    assert len(refused) == 3


def test_a_buy_timeout_said_outright_holds_whatever_the_stack():
    def config(*flags):
        return cli._exec_config(cli.build_parser().parse_args(["execute", *flags]))

    by_stack = config()
    assert by_stack.big_stack > 0 and by_stack.buy_timeout_big > by_stack.buy_timeout  # more time once the stack is big
    fixed = config("--buy-timeout", "45")
    assert (fixed.buy_timeout, fixed.big_stack) == (45 * 60, 0)


def test_the_report_turns_the_fill_ratio_into_the_share_of_volume_you_get():
    assert "about 1/91 of the volume" in cli._share_measured(FillReality(0.33, 12, {}), 30)  # a third of what 1/30 would fill
    assert "about 1/15 or more of the volume" in cli._share_measured(FillReality(FILL_RATIO_CAP, 12, {}), 30)  # the ratio stops at its cap
    assert "nothing to compare yet" in cli._share_measured(FillReality(None, 3, {}), 30)


# ---- what the loop shows and says, and when ---------------------------------------

ORE, SALMON = 440, 329
ITEMS = {ORE: ItemInfo(ORE, "Iron ore", False, 13000, 0, 0), SALMON: ItemInfo(SALMON, "Salmon", False, 6000, 0, 0)}
T0 = 1_700_000_000
ORE_ONLY = {ORE: (100, 110)}  # instant-sell 100, instant-buy 110
BOTH = {ORE: (100, 110), SALMON: (50, 56)}


class Loop:
    """cmd_execute without the files: a status in, the answer out, and what it says aloud."""

    def __init__(self, **cfg):
        self.engine = Executor(ExecState(), ITEMS, ExecConfig(**cfg))
        self.voice = cli.Announcer()
        self.now = T0

    def answer(self, orders, prices, wait=60, **status):
        self.now += wait
        latest = {i: LatestPrice(high, self.now, low, self.now) for i, (low, high) in prices.items()}
        market = Market(latest=latest, hour={i: WindowPrice(None, 10_000, None, 10_000) for i in prices}, stats=None)
        plan = self.engine.step(parse_status({"orders": orders, **status}, ITEMS, self.now), market, self.now)
        out = {"time": "2026-01-01 00:00:00", **plan.to_json()}
        said = self.voice.line(out, {o.id for o in self.engine.state.done if o.outcome == "withdrawn"})
        return out, said


def kinds(out):
    return [a["action"] for a in out["actions"]]


def test_the_screen_is_redrawn_for_a_new_instruction_and_not_for_a_fill():
    loop = Loop()
    out, _ = loop.answer([], ORE_ONLY, cash=10_000, slots=2)
    (buy,) = out["actions"]
    oid = buy["order_id"]
    placed, _ = loop.answer([{"order_id": oid, "status": "open"}], ORE_ONLY)
    assert cli._todo_signature(placed) != cli._todo_signature(out)  # the BUY became an offer to leave alone
    waiting, _ = loop.answer([{"order_id": oid, "status": "open"}], ORE_ONLY)
    assert placed["actions"][0]["why"] != waiting["actions"][0]["why"]  # "open 0m" -> "open 1m"
    assert cli._todo_signature(waiting) == cli._todo_signature(placed)

    few, _ = loop.answer([{"order_id": oid, "status": "filling", "filled": 5}], ORE_ONLY)
    more, _ = loop.answer([{"order_id": oid, "status": "filling", "filled": 7}], ORE_ONLY)
    assert kinds(few) == kinds(more) == ["HOLD", "HOLD"]  # the offer, and the few units bought so far
    assert cli._todo_signature(more) == cli._todo_signature(few)  # only the counts moved
    enough, said = loop.answer([{"order_id": oid, "status": "filling", "filled": 30}], ORE_ONLY)
    assert kinds(enough) == ["SELL", "HOLD"] and cli._todo_signature(enough) != cli._todo_signature(more)
    assert said == "Grand Exchange: 1 thing to do. Take the items out of the buy offer, then sell 30 Iron ore at 109 each."

    stale = {**enough, "warnings": ["status.json is 6 min old: update it before acting on these orders"]}
    assert cli._todo_signature(stale) == cli._todo_signature(enough)  # a warning that changes every minute by itself
    assert cli._todo_signature({**enough, "warnings": ["order #9 disappeared"]}) != cli._todo_signature(enough)
    assert cli._todo_signature({"time": "", "error": "no status.json yet", "actions": []}) == ("error", "no status.json yet")


def test_the_next_thing_to_do_is_said_when_you_have_done_one_and_not_when_it_is_only_replanned():
    loop = Loop(pending_ttl=180)
    out, said = loop.answer([], BOTH, cash=10_000, slots=2)
    first, second = out["actions"]
    assert kinds(out) == ["BUY", "BUY"]
    assert said == f"Grand Exchange: 2 things to do. Buy {first['qty']:,} {first['item']} at {first['price']:,} each."

    one = {"order_id": first["order_id"], "status": "open"}
    out, said = loop.answer([one], BOTH)  # the first is placed: on to the second
    assert kinds(out) == ["BUY", "HOLD"] and said == f"Buy {second['qty']:,} {second['item']} at {second['price']:,} each."
    quiet, said = loop.answer([one], BOTH)
    assert said is None  # nothing happened

    low, high = BOTH[second["item_id"]]
    moved = {**BOTH, second["item_id"]: (low + 1, high + 1)}
    out, said = loop.answer([one], moved, wait=200)  # you are away; the instruction is re-planned at the new price
    again = out["actions"][0]
    assert kinds(out) == ["BUY", "HOLD"] and again["order_id"] != second["order_id"] and again["price"] != second["price"]
    assert said is None  # not read out again...
    assert cli._todo_signature(out) != cli._todo_signature(quiet)  # ...though the screen shows the new price

    out, said = loop.answer([{"order_id": first["order_id"], "status": "bought"}], moved)  # something more urgent turns up
    assert kinds(out)[:2] == ["COLLECT", "SELL"] and said == f"Collect {first['item']}: {first['qty']:,} bought."
    sell = out["actions"][1]
    out, said = loop.answer([], moved)  # collected
    assert kinds(out)[0] == "SELL" and said == f"Sell {sell['qty']:,} {sell['item']} at {sell['price']:,} each."

    buy = next(a for a in out["actions"] if a["action"] == "BUY")
    both = [{"order_id": sell["order_id"], "status": "open"}, {"order_id": buy["order_id"], "status": "open"}]
    out, said = loop.answer(both, moved)
    assert set(kinds(out)) == {"HOLD"} and said == "Grand Exchange: nothing left to do."
    out, said = loop.answer(both, moved)
    assert said is None
    out, said = loop.answer([{"order_id": sell["order_id"], "status": "sold"}, both[1]], moved)
    assert said == f"Grand Exchange: {sum(k != 'HOLD' for k in kinds(out))} things to do. Collect {sell['item']}: {sell['qty']:,} sold."


def test_an_offer_to_abort_is_announced_and_an_answer_that_is_an_error_says_nothing():
    loop = Loop(buy_timeout=1800)
    out, _ = loop.answer([], ORE_ONLY, cash=10_000, slots=1)
    oid = out["actions"][0]["order_id"]
    working = [{"order_id": oid, "status": "open"}]
    out, said = loop.answer(working, ORE_ONLY)
    assert said == "Grand Exchange: nothing left to do."
    assert loop.voice.line({"time": "", "error": "status.json not understood", "actions": []}, set()) is None
    out, said = loop.answer(working, ORE_ONLY, wait=1800)
    assert kinds(out)[0] == "CANCEL" and said.endswith("Abort the buy offer for Iron ore.") and said.startswith("Grand Exchange: ")
