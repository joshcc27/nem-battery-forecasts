"""The battery, and the optimisation that plans its dispatch."""

from __future__ import annotations

from dataclasses import dataclass

import highspy
import numpy as np

DT = 5 / 60  # hours per interval
TIE_BREAK = 1e-4  # A$/MWh on throughput, so the solver doesn't cycle when it's indifferent


@dataclass(frozen=True)
class Battery:
    power_mw: float = 100.0
    energy_mwh: float = 200.0
    round_trip: float = 0.85
    deg_cost: float = 25.0  # A$ per MWh discharged

    @property
    def eta(self) -> float:
        """One-way efficiency, the same for charging and discharging."""
        return self.round_trip**0.5

    @property
    def soc_min(self) -> float:
        return 0.05 * self.energy_mwh

    @property
    def soc_max(self) -> float:
        return 0.95 * self.energy_mwh


@dataclass(frozen=True)
class Plan:
    charge: np.ndarray  # MW, for the committed intervals
    discharge: np.ndarray
    value: np.ndarray  # A$/MWh: what one more MWh in store is worth after each committed interval
    mode: np.ndarray  # 1 charge, 0 discharge, NaN where the mode was left free
    revenue: float  # planned revenue over the whole window


def plan(
    price: np.ndarray,
    gen_lf: np.ndarray,
    load_lf: np.ndarray,
    battery: Battery,
    soc0: float,
    commit: int | None,
) -> Plan:
    """Maximise revenue over the window at the given prices.

    With ``commit`` set this is the hourly plan: the first ``commit`` intervals
    may not charge and discharge at once. With ``commit=None`` it is the
    ceiling: an LP with no such rule and no tie-break, so no dispatch can beat it.
    """
    n, k = len(price), commit or 0
    b = battery
    tie = DT * TIE_BREAK if commit else 0.0
    charge_cost = DT * load_lf * price
    discharge_value = DT * (gen_lf * price - b.deg_cost)

    # Columns: charge[n], discharge[n], soc[n], mode[k].
    c, d, s, m = np.arange(n), n + np.arange(n), 2 * n + np.arange(n), 3 * n + np.arange(k)
    cost = np.concatenate([-charge_cost - tie, discharge_value - tie, np.zeros(n + k)])
    lower = np.concatenate([np.zeros(2 * n), np.full(n, b.soc_min), np.zeros(k)])
    upper = np.concatenate([np.full(2 * n, b.power_mw), np.full(n, b.soc_max), np.ones(k)])

    # Energy balance: soc[t] - soc[t-1] - DT*eta*charge[t] + DT/eta*discharge[t] = 0.
    t = np.arange(n)
    rows = [t, t[1:], t, t]
    cols = [s, s[:-1], c, d]
    vals = [np.ones(n), -np.ones(n - 1), np.full(n, -DT * b.eta), np.full(n, DT / b.eta)]
    # Mode: charge <= P*mode and discharge <= P*(1 - mode).
    j = np.arange(k)
    rows += [n + j, n + j, n + k + j, n + k + j]
    cols += [c[:k], m, d[:k], m]
    vals += [np.ones(k), np.full(k, -b.power_mw), np.ones(k), np.full(k, b.power_mw)]
    row_lower = np.concatenate([[soc0], np.zeros(n - 1), np.full(2 * k, -highspy.kHighsInf)])
    row_upper = np.concatenate([[soc0], np.zeros(n - 1), np.zeros(k), np.full(k, b.power_mw)])

    lp = highspy.HighsLp()
    lp.num_col_, lp.num_row_ = 3 * n + k, n + 2 * k
    lp.sense_ = highspy.ObjSense.kMaximize
    lp.col_cost_, lp.col_lower_, lp.col_upper_ = cost, lower, upper
    lp.row_lower_, lp.row_upper_ = row_lower, row_upper
    row, col, val = np.concatenate(rows), np.concatenate(cols), np.concatenate(vals)
    order = np.lexsort((row, col))
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.start_ = np.searchsorted(col[order], np.arange(lp.num_col_ + 1)).astype(np.int32)
    lp.a_matrix_.index_ = row[order].astype(np.int32)
    lp.a_matrix_.value_ = val[order]

    # Charging and discharging at once only pays when burning energy earns money,
    # i.e. at deeply negative prices. Only there does the mode need to be a binary.
    burn = -charge_cost[:k] + b.round_trip * discharge_value[:k]
    binary = burn >= -1e-9

    highs = highspy.Highs()
    highs.setOptionValue("output_flag", False)
    highs.setOptionValue("mip_rel_gap", 1e-6)
    highs.passModel(lp)
    if binary.any():
        modes = m[binary].astype(np.int32)
        highs.changeColsIntegrality(
            len(modes), modes, np.full(len(modes), highspy.HighsVarType.kInteger)
        )
    _solve(highs)

    mode = np.full(k, np.nan)
    if binary.any():
        # A MIP has no duals, so fix the modes it chose and solve the LP again.
        fixed = m[binary].astype(np.int32)
        mode[binary] = np.round(np.asarray(highs.getSolution().col_value)[fixed])
        highs.changeColsBounds(len(fixed), fixed, mode[binary], mode[binary])
        highs.changeColsIntegrality(
            len(fixed), fixed, np.full(len(fixed), highspy.HighsVarType.kContinuous)
        )
        _solve(highs)

    solution = highs.getSolution()
    x = np.asarray(solution.col_value)
    # The dual of a balance row is the value of one more MWh stored at that point.
    value = np.asarray(solution.row_dual)[:k]
    revenue = float(discharge_value @ x[d] - charge_cost @ x[c])

    def tidy(mw: np.ndarray) -> np.ndarray:
        return np.clip(np.where(np.abs(mw) < 1e-8, 0.0, mw), 0.0, b.power_mw)

    return Plan(
        charge=tidy(x[c[:k]]),
        discharge=tidy(x[d[:k]]),
        value=value,
        mode=mode,
        revenue=revenue,
    )


def _solve(highs: highspy.Highs) -> None:
    highs.run()
    if highs.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        highs.clearSolver()
        highs.run()
    status = highs.getModelStatus()
    if status != highspy.HighsModelStatus.kOptimal:
        raise RuntimeError(f"HiGHS: {highs.modelStatusToString(status)}")
