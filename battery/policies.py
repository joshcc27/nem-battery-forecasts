"""Rolling dispatch: every hour, plan 24 hours ahead from a forecast and commit the first hour."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from battery.data import FLOOR
from battery.model import DT, TIE_BREAK, Battery, Plan, plan

HORIZON = 288  # 24 hours of 5-minute intervals
COMMIT = 12  # one hour

Forecaster = Callable[[np.ndarray, int], np.ndarray]


def run(
    price: np.ndarray,
    gen_lf: np.ndarray,
    load_lf: np.ndarray,
    cap: np.ndarray,
    start: int,
    forecaster: Forecaster,
    battery: Battery,
    rule: bool = False,
) -> dict[str, np.ndarray]:
    """Dispatch ``price[start:]``. Everything before ``start`` is history.

    Each decision sees only the prices settled before it. The committed hour is
    then offered at the plan's price limits and cleared against what actually
    happened. The rule-based policy (``rule=True``) uses the same forecast but
    no optimiser.
    """
    n = len(price)
    charge, discharge, forecast = np.zeros(n), np.zeros(n), np.zeros(n)
    soc = battery.soc_min
    for pos in range(start, n, COMMIT):
        window = slice(pos, min(pos + HORIZON, n))
        steps = window.stop - pos
        hour = slice(pos, min(pos + COMMIT, n))
        f = np.clip(forecaster(price[:pos], steps), FLOOR, cap[window])
        if rule:
            c, d = _rule(f, price[hour], gen_lf[pos], load_lf[pos], battery)
        else:
            p = plan(f, gen_lf[window], load_lf[window], battery, soc, hour.stop - pos)
            c, d = clear(price[hour], p, gen_lf[hour], load_lf[hour], battery)
        c, d = fit_to_soc(c, d, soc, battery)
        charge[hour], discharge[hour], forecast[hour] = c, d, f[: hour.stop - pos]
        soc += DT * (battery.eta * c.sum() - d.sum() / battery.eta)
    return {"charge": charge[start:], "discharge": discharge[start:], "forecast": forecast[start:]}


def clear(
    price: np.ndarray, p: Plan, gen_lf: np.ndarray, load_lf: np.ndarray, b: Battery
) -> tuple[np.ndarray, np.ndarray]:
    """Clear the committed hour against the real prices.

    The plan values stored energy at ``p.value``, which sets a price limit in
    each direction. Past the limit the battery runs at full power, at the limit
    it runs the planned amount, and short of it it stays idle. If the forecast
    was right this reproduces the plan.
    """
    charge, discharge = np.zeros(len(price)), np.zeros(len(price))
    for i, rrp in enumerate(price):
        value = p.value[i]
        buy = b.eta * value - TIE_BREAK - load_lf[i] * rrp  # gain per MWh charged
        sell = gen_lf[i] * rrp - b.deg_cost - TIE_BREAK - value / b.eta  # gain per MWh sold
        tol = 1e-6 * max(1.0, abs(value), abs(rrp))
        c = b.power_mw if buy > tol else p.charge[i] if buy >= -tol else 0.0
        d = b.power_mw if sell > tol else p.discharge[i] if sell >= -tol else 0.0
        if buy >= -tol and sell >= -tol:
            # Both directions are worth offering, so follow the plan.
            if p.discharge[i] > 0 or p.mode[i] == 0:
                c = 0.0
            elif p.charge[i] > 0 or p.mode[i] == 1:
                d = 0.0
            elif buy >= sell:
                d = 0.0
            else:
                c = 0.0
        charge[i], discharge[i] = c, d
    return charge, discharge


def _rule(
    forecast: np.ndarray,
    price: np.ndarray,
    gen_lf: float,
    load_lf: float,
    b: Battery,
    hours: int = 2,
) -> tuple[np.ndarray, np.ndarray]:
    """Charge in the two cheapest forecast hours and discharge in the two dearest.

    It only trades if the spread between those hours pays for losses and
    degradation.
    """
    n = len(price)
    hourly = forecast[: len(forecast) // COMMIT * COMMIT].reshape(-1, COMMIT).mean(axis=1)
    if len(hourly) < 2 * hours:
        return np.zeros(n), np.zeros(n)
    order = np.argsort(hourly, kind="stable")
    cheap, dear = order[:hours], order[-hours:]
    buy, sell = hourly[cheap].max(), hourly[dear].min()
    if (gen_lf * sell - b.deg_cost) * b.round_trip <= load_lf * buy:
        return np.zeros(n), np.zeros(n)
    return np.full(n, b.power_mw * (0 in cheap)), np.full(n, b.power_mw * (0 in dear))


def fit_to_soc(
    charge: np.ndarray, discharge: np.ndarray, soc: float, b: Battery
) -> tuple[np.ndarray, np.ndarray]:
    """Cut power back where the battery would otherwise go past full or empty."""
    charge, discharge = charge.copy(), discharge.copy()
    for i in range(len(charge)):
        charge[i] = min(charge[i], max(0.0, (b.soc_max - soc) / (DT * b.eta)))
        discharge[i] = min(discharge[i], max(0.0, (soc - b.soc_min) * b.eta / DT))
        soc += DT * (b.eta * charge[i] - discharge[i] / b.eta)
    return charge, discharge
