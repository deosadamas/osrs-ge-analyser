# GE Analyser RuneLite plugin

Three things next to the game:

- a side panel: what `ge-analyser execute` says to do right now, or the flips
  `ge-analyser advise` suggests;
- a recorder that logs your real GE offers for `ge-analyser sync`;
- a **live status file** for `ge-analyser execute`: your GE offers, coins and inventory,
  rewritten whenever one of them changes, so the execution loop needs no typing.

It only **reads** game state and **writes files / draws a panel**. It never places,
changes or cancels offers — that line is what separates a tool from a bot under Jagex's
rules, and this plugin stays on the right side of it.

## Live status for `ge-analyser execute`

```sh
ge-analyser execute        # in a terminal next to RuneLite; answers within ~2 s of every change
```

While you are logged in the plugin keeps `~/.osrs-ge-analyser/exec/status.json` (or the
folder in `GE_ANALYSER_EXEC`, or the *Status file* setting) describing what the client
shows:

```json
{
  "source": "runelite",
  "account": "Zezima",
  "cash": 4950,
  "members": true,
  "slots": 8,
  "orders": [
    {"slot": 0, "ref": "0-1794371900", "since": 1794371900, "status": "BUYING",
     "item_id": 440, "item": "Iron ore", "price": 44, "qty": 387, "filled": 120, "spent": 5280},
    {"slot": 2, "ref": "2-1794371200", "since": 1794371200, "status": "SOLD",
     "item_id": 554, "item": "Fire rune", "price": 5, "qty": 900, "filled": 900, "spent": 4500, "collected": true}
  ],
  "inventory": [{"item_id": 554, "item": "Fire rune", "qty": 120}]
}
```

| field | meaning |
|---|---|
| `cash` | coins in your inventory, plus 1,000 per platinum token (the GE takes both since 30 Sep 2026) |
| `members` | you are on a members world (which items can be traded) |
| `slots` | 8 for a member, 3 otherwise |
| `orders[].status` | the client's state: `BUYING`, `BOUGHT`, `CANCELLED_BUY`, `SELLING`, `SOLD`, `CANCELLED_SELL` |
| `orders[].qty`, `.filled`, `.spent` | size of the offer, units traded, coins paid / received before tax |
| `orders[].ref` | the plugin's own name for this offer; it keeps it until the offer leaves its slot, across client restarts |
| `orders[].since` | when the plugin saw the offer being placed; `0` = it was already there when it first looked |
| `orders[].collected` | a finished offer you have collected; it stays listed for 10 minutes so its final count cannot be missed |
| `inventory` | every stack except coins; noted and un-noted forms count as the item itself |

When it writes: one game tick after an offer, your coins or your inventory changed, and
once a minute otherwise so the file's age shows it is being kept. Nothing is written
while logged out, nor for the first few ticks after logging in or hopping: the client
empties every GE slot then and the server sends them again, and that must not read as
"everything was collected".

The file is replaced in one step, so the CLI never reads half of it. Turn it off with
the *Write status.json* setting.

How it was checked: the status writer's own output was fed to the engine over 48 hours
of real market history with a paper account behind it — every offer placed, filled,
aborted and collected. The books came out identical to the coin to the same run with
order ids, also across client restarts, and stayed right with a player who collects
late. It has not yet been watched through a real logged-in session.

## The panel

### Instructions, while `ge-analyser execute` is running

The panel shows the loop's answer (`orders.json`, in the folder of the status file)
within a second of it being written, so the terminal can stay out of sight. Do the rows
top to bottom:

| Row | What to do |
|---|---|
| `COLLECT` item | Collect that offer: it finished or was aborted. |
| `CANCEL` item | Abort that offer and collect what it holds. |
| `SELL` item, `qty × price gp each` | Place that sell offer. `all qty`: it is every unit of the item in your inventory, so there is no quantity to enter. "take the items out of its buy offer first": the units are still in a buy offer, which keeps running. |
| `BUY` item, `qty × price gp each` | Place that buy offer. |
| `HOLD` item, in grey | Nothing: an offer or a stack to leave alone. |

Quantities and prices are written out in full, as the GE shows them. Under each row is
the engine's reason, and warnings are in yellow above the rows. The header has your
coins, what you should hold once the batch is placed, equity and realised profit, GE
slots and inventory stacks in use, and the age of the answer. That turns red, with "no
new answer", when the loop has not answered for three minutes: it was stopped, or it
cannot fetch prices. Suggestions from `advise`, if there are any, then take its place.

The panel only displays. You place, abort and collect every offer yourself.

How it was checked: the panel was drawn, outside the client, from the answers the CLI
wrote for a scripted session (every kind of row, a stale answer, no status yet); and a
real client, by its log, read each new answer within a second of it being written. Like
the status file, it has not yet been watched through a logged-in session.

### Suggestions, from `ge-analyser advise`

Each row: item, `buy X → sell Y  +margin/ea`, then `qty · limit left/limit · fill % · exp`.
Items whose 4-hour buy limit you have exhausted are shown in red. The header shows the
budget the advice was computed for and how old it is (red when > 10 min).

```sh
# generic suggestions, refreshed every 60 s
ge-analyser advise --budget 10m --top 15
# ...or mirror the paper-trading agent: exactly the offers it has open right now
ge-analyser advise --from-agent
```

In `--from-agent` mode each row is an order to have in the GE: `BUY Gold ore — 24,182 × 111 gp
each — then sell @ 114 (+1/ea)` or `SELL Ruby bolts (e) — 2,282 × 195 gp each — cost 187`.
Sells are listed first because they free slots. The header shows the agent's equity and
realised P&L. Anything you have open that is not on the list is something the agent has
cancelled. For a machine-readable stream of the agent's decisions run it with
`simulate --ticks 0 --events ~/.osrs-ge-analyser/agent-events.jsonl`: one JSON line per
`offer` / `fill` / `cancel` / `reprice` / `dump` with item, qty, price and side. A `fill`
also gives `window`, the start of the 5-minute window whose volume it took its share of
(at most one fill per offer and window).

The advice file lives at `~/.osrs-ge-analyser/advice.json` (configurable in the plugin
settings). Your offers are appended to `~/.osrs-ge-analyser/runelite-trades.jsonl`, which
`ge-analyser sync` reads automatically — Flipping Utilities is no longer required.

## Build and run

```sh
cd runelite-plugin
./gradlew run                                  # RuneLite in developer mode with this plugin built in
./gradlew shadowJar                            # build/libs/ge-analyser-0.1.0-all.jar: the same client as one runnable jar
./gradlew test --tests StatusWriterTest        # the status writer's rules
```

The plugin is not on the Plugin Hub, and the official launcher does not pass
`--developer-mode`, so it cannot be dropped into a launcher-started client: it runs in
the client `gradlew run` starts (your profile and Plugin Hub plugins load as usual), or
from the jar: `java -ea -jar build/libs/ge-analyser-0.1.0-all.jar --developer-mode`.

Needs JDK 11–21 (the build targets Java 11 bytecode; set `JAVA_HOME` if your default JDK
is newer) and network access for `net.runelite:client:latest.release` from
`repo.runelite.net`.

## Trade log format

One JSON object per line, one per offer-change event:

| field | meaning |
|---|---|
| `t` | unix seconds of the event |
| `acc` | display name |
| `slot` | GE slot 0–7 |
| `b` | `true` for buy offers |
| `id`, `name` | item |
| `qty` | units traded so far in this offer |
| `total` | units the offer was placed for |
| `price` | average price per unit traded (`spent / qty`), or the listed price before any fill |
| `listed` | price the offer was placed at |
| `st` | `BUYING`, `BOUGHT`, `SELLING`, `SOLD`, `CANCELLED_BUY`, `CANCELLED_SELL` |
| `placed` | unix seconds the offer was first seen in this slot (fill time = `t - placed`) |

The client re-sends every slot's state on login; both the recorder and the parser
ignore exact repeats of an already-completed trade.
