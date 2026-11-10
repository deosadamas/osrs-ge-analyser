# GE Analyser RuneLite plugin

A side panel that shows the flips suggested by the `ge-analyser` CLI while you play,
and a recorder that logs your real GE offers for `ge-analyser sync`.

It only **reads** game state (`GrandExchangeOfferChanged`) and **draws** a panel. It never
places, changes or cancels offers — that line is what separates a tool from a bot under
Jagex's rules, and this plugin stays on the right side of it.

## What you see

Each row: item, `buy X → sell Y  +margin/ea`, then `qty · limit left/limit · fill % · exp`.
Items whose 4-hour buy limit you have exhausted are shown in red. The header shows the
budget the advice was computed for and how old it is (red when > 10 min).

## Run it

Two processes:

```sh
# 1. keep the advice file fresh (every 60 s)
ge-analyser advise --budget 10m --top 15

# 2. RuneLite with the plugin (sideloaded; no Plugin Hub review needed for personal use)
cd runelite-plugin
./gradlew test --tests GeAnalyserPluginTest     # dev launcher, or run that class from an IDE
```

The advice file lives at `~/.osrs-ge-analyser/advice.json` (configurable in the plugin
settings). Your offers are appended to `~/.osrs-ge-analyser/runelite-trades.jsonl`, which
`ge-analyser sync` reads automatically — Flipping Utilities is no longer required.

## Build

```sh
cd runelite-plugin
./gradlew jar          # build/libs/ge-analyser-0.1.0.jar
```

Needs JDK 11+ to compile (the build targets Java 11 bytecode) and network access for
`net.runelite:client:latest.release` from `repo.runelite.net`. If you keep several JDKs,
set `JAVA_HOME` to one of them; the toolchain block in `build.gradle` names 17.

To load the built jar into a normal RuneLite install instead of the dev launcher,
enable developer mode (`--developer-mode`) and drop the jar in `~/.runelite/sideloaded-plugins/`.

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
