"""Price forecasters.

Each one is called with the prices settled so far and the number of intervals to forecast.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from battery.data import Runs

PER_DAY = 288
MINUTE = 60 * 10**9  # nanoseconds


@dataclass(frozen=True)
class Perfect:
    """The prices that actually happened."""

    actual: np.ndarray

    def __call__(self, seen: np.ndarray, steps: int) -> np.ndarray:
        return self.actual[len(seen) : len(seen) + steps].copy()


@dataclass(frozen=True)
class Profile:
    """Median price at the same time of day over the last week."""

    days: int = 7

    def __call__(self, seen: np.ndarray, steps: int) -> np.ndarray:
        recent = seen[-self.days * PER_DAY :]
        if len(recent) < self.days * PER_DAY:
            raise ValueError(f"the profile needs {self.days} days of history")
        profile = np.median(recent.reshape(self.days, PER_DAY), axis=0)
        return profile[np.arange(steps) % PER_DAY]


@dataclass(frozen=True)
class Aemo:
    """AEMO's forecasts as they stood when the decision was made.

    Each interval takes the newest P5MIN run if it covers it, otherwise the
    newest pre-dispatch run if the interval is within four hours, otherwise
    the profile. Pre-dispatch is good for a few hours and over-forecasts spikes
    after that. A run older than 15 minutes (P5MIN) or an hour (pre-dispatch)
    counts as missing.
    """

    times: np.ndarray  # interval ends of the whole price series, UTC nanoseconds
    p5min: Runs
    predispatch: Runs
    profile: Profile = Profile()
    predispatch_hours: float = 4.0

    def __call__(self, seen: np.ndarray, steps: int) -> np.ndarray:
        now = self.times[len(seen) - 1]
        targets = self.times[len(seen) : len(seen) + steps]
        values = self.profile(seen, steps)
        # Pre-dispatch prices 30-minute periods; an interval belongs to the period it ends in.
        half_hour = 30 * MINUTE
        periods = -(-targets // half_hour) * half_hour
        reach = int(np.count_nonzero(targets <= now + int(self.predispatch_hours * 60) * MINUTE))
        _overlay(values[:reach], self.predispatch, now, periods[:reach], 60 * MINUTE)
        _overlay(values, self.p5min, now, targets, 15 * MINUTE)
        return values


def _overlay(values: np.ndarray, runs: Runs, now: int, keys: np.ndarray, max_age: int) -> None:
    run_targets, run_rrp, published = runs.latest(now)
    if published is None or now - published > max_age:
        return
    index = np.minimum(np.searchsorted(run_targets, keys), len(run_targets) - 1)
    hit = run_targets[index] == keys
    values[hit] = run_rrp[index[hit]]
