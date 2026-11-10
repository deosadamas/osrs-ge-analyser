"""Regime-shift detection from the daily history table.

A "dip" is only a mean-reversion opportunity if the price is expected to come back.
After a game update (new drop source, nerf, new consumer) prices re-level and stay
there. We call a move structural when the last week sits persistently outside the
prior two months' band — then the 24h mean is no longer a target.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .history import Row

RECENT_DAYS = 7
PRIOR_DAYS = 60
SHIFT_PCT = 15.0  # recent mean vs prior mean
PERSIST_DAYS = 5  # of the last 7 must sit beyond the threshold


@dataclass(frozen=True)
class Regime:
    days: int
    prior_mean: float
    recent_mean: float
    change_pct: float
    persistent_days: int

    @property
    def structural(self) -> bool:
        return abs(self.change_pct) >= SHIFT_PCT and self.persistent_days >= PERSIST_DAYS

    def label(self) -> str:
        if self.structural:
            return f"shift {self.change_pct:+.0f}% ({self.persistent_days}/{RECENT_DAYS}d)"
        return f"stable {self.change_pct:+.0f}%"


def _mid(r: Row) -> float | None:
    if r.high is not None and r.low is not None:
        return (r.high + r.low) / 2
    return float(r.high if r.high is not None else r.low) if (r.high is not None or r.low is not None) else None


def detect(daily: list[Row]) -> Regime | None:
    """daily: rows in time order (most recent last). Needs ~a month of prior data."""
    mids = [(r.ts, m) for r in daily if (m := _mid(r)) is not None]
    if len(mids) < RECENT_DAYS + 20:
        return None
    recent = [m for _, m in mids[-RECENT_DAYS:]]
    prior = [m for _, m in mids[-(RECENT_DAYS + PRIOR_DAYS) : -RECENT_DAYS]]
    prior_mean = statistics.fmean(prior)
    if prior_mean <= 0:
        return None
    recent_mean = statistics.fmean(recent)
    change = (recent_mean / prior_mean - 1) * 100
    thr = SHIFT_PCT / 100
    if change < 0:
        persistent = sum(1 for m in recent if m < prior_mean * (1 - thr))
    else:
        persistent = sum(1 for m in recent if m > prior_mean * (1 + thr))
    return Regime(len(mids), prior_mean, recent_mean, change, persistent)
