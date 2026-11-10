"""Walk-forward evaluation of the scoring constants.

The score has three hand-set knobs: the liquidity weight, the volatility penalty and
the shrinkage prior for time-of-week buckets. `tune` sweeps each one around its default
over several non-overlapping test windows, computing features only from data before
each window. Configs are ranked on the older folds and the best one is confirmed on the
most recent fold, so the recommendation is out-of-sample.
"""

from __future__ import annotations

import dataclasses
import statistics
from dataclasses import dataclass, replace

from .analysis import DEFAULT_PARAMS, ScoreParams
from .api import ItemInfo
from .features import SHRINK_WINDOWS, compute_timed_stats
from .history import HistoryStore
from .sim import Config, replay


@dataclass(frozen=True)
class Candidate:
    name: str
    params: ScoreParams
    shrink_k: int

    def describe(self) -> str:
        return f"liq_weight={self.params.liq_weight:g} vol_penalty={self.params.vol_penalty:g} shrink_k={self.shrink_k}"


def grid() -> list[Candidate]:
    base = Candidate("default", DEFAULT_PARAMS, SHRINK_WINDOWS)
    out = [base]
    for lw in (5.0, 20.0):
        out.append(Candidate(f"liq_weight={lw:g}", replace(DEFAULT_PARAMS, liq_weight=lw), SHRINK_WINDOWS))
    for vp in (0.5, 2.0):
        out.append(Candidate(f"vol_penalty={vp:g}", replace(DEFAULT_PARAMS, vol_penalty=vp), SHRINK_WINDOWS))
    for k in (144, 576):
        out.append(Candidate(f"shrink_k={k}", DEFAULT_PARAMS, k))
    return out


@dataclass
class FoldResult:
    fold: int
    start: int
    end: int
    returns: dict[str, float]  # candidate name -> return fraction


def run(
    store: HistoryStore,
    items: dict[int, ItemInfo],
    cfg: Config,
    budget: int,
    folds: int,
    hours: float,
    history_days: float,
    candidates: list[Candidate],
    progress=None,
) -> list[FoldResult]:
    _, last, _ = store.coverage()
    if last is None:
        raise RuntimeError("no history")
    names = {i: it.name for i, it in items.items()}
    span = int(hours * 3600)
    results: list[FoldResult] = []
    for f in range(folds):
        end = last - f * span
        start = end - span
        since = start - int(history_days * 86400)
        # Features per distinct shrink_k, each computed strictly before the fold.
        timed_by_k = {}
        for k in {c.shrink_k for c in candidates}:
            timed_by_k[k] = compute_timed_stats(store, names, since, start, shrink_k=k)
        windows = list(store.windows(start - 3600, end + 1))
        returns = {}
        for c in candidates:
            state, snap, _ = replay(windows, items, replace(cfg, score=c.params), budget, start, timed_by_k[c.shrink_k].at)
            returns[c.name] = state.equity(snap) / budget - 1
            if progress:
                progress(f, c.name, returns[c.name])
        results.append(FoldResult(f, start, end, returns))
    return results


def summarise(results: list[FoldResult], candidates: list[Candidate]) -> tuple[list[tuple[str, float, float, float]], str]:
    """Per candidate: mean return on older folds, rank there, return on the newest fold.
    Returns rows sorted by older-fold mean, and the recommended candidate name."""
    older = [r for r in results if r.fold > 0]
    newest = results[0] if results else None
    rows = []
    for c in candidates:
        train = statistics.fmean(r.returns[c.name] for r in older) if older else float("nan")
        test = newest.returns[c.name] if newest else float("nan")
        rows.append((c.name, train, test, 0.0))
    rows.sort(key=lambda r: r[1], reverse=True)
    rows = [(n, tr, te, i + 1) for i, (n, tr, te, _) in enumerate(rows)]
    best = rows[0][0] if rows else "default"
    default_test = next(te for n, _, te, _ in rows if n == "default")
    best_test = rows[0][2]
    if best == "default" or best_test <= default_test:
        return rows, "default"
    return rows, best
