"""Run every policy over the holdout year and write the results."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from battery.data import (
    AEST,
    LOSS_FACTORS,
    PRICE_CAP,
    financial_year,
    for_year,
    load_forecasts,
    load_prices,
    to_utc_ns,
)
from battery.forecasts import Aemo, Perfect, Profile
from battery.model import DT, Battery, plan
from battery.policies import run

START = pd.Timestamp("2023-06-01", tz=AEST)
END = pd.Timestamp("2024-06-01", tz=AEST)
WARM_UP = pd.Timedelta(days=7)  # history the profile needs before the first decision

POLICIES = {
    "oracle": "Rolling-horizon optimiser with perfect foresight (oracle)",
    "aemo": "Rolling-horizon optimiser with AEMO forecasts",
    "profile": "Rolling-horizon optimiser with a 7-day price profile",
    "rule": "Rule-based strategy with AEMO forecasts",
}


def load(cache: Path) -> dict:
    prices = load_prices(START - WARM_UP, END, cache)
    times = prices["settlementdate"]
    fy = (times - pd.Timedelta(minutes=5)).map(financial_year)
    loss = fy.map(lambda y: for_year(LOSS_FACTORS, y, "loss factors"))
    return {
        "times": times,
        "price": prices["rrp"].to_numpy(),
        "gen_lf": loss.str[0].to_numpy(dtype=float),
        "load_lf": loss.str[1].to_numpy(dtype=float),
        "cap": fy.map(lambda y: for_year(PRICE_CAP, y, "price cap")).to_numpy(dtype=float),
        "start": int((times <= START).sum()),
        "p5min": load_forecasts("p5min", START - WARM_UP, END, cache),
        "predispatch": load_forecasts("predispatch", START - WARM_UP, END, cache),
    }


def run_policy(name: str, d: dict, battery: Battery) -> dict[str, np.ndarray]:
    aemo = Aemo(to_utc_ns(d["times"]), d["p5min"], d["predispatch"])
    forecaster = {"oracle": Perfect(d["price"]), "profile": Profile()}.get(name, aemo)
    return run(
        d["price"],
        d["gen_lf"],
        d["load_lf"],
        d["cap"],
        d["start"],
        forecaster,
        battery,
        rule=name == "rule",
    )


def settle(dispatch: dict, d: dict, battery: Battery) -> np.ndarray:
    """Revenue in each interval, after loss factors and degradation."""
    s = slice(d["start"], None)
    price, gen, load = d["price"][s], d["gen_lf"][s], d["load_lf"][s]
    return DT * (
        gen * price * dispatch["discharge"]
        - load * price * dispatch["charge"]
        - battery.deg_cost * dispatch["discharge"]
    )


def block_bootstrap(diff: np.ndarray, block: int = 7, draws: int = 10_000) -> tuple[float, float]:
    """95% interval for the mean daily difference.

    Days are resampled in week-long blocks because consecutive days aren't
    independent: what one day leaves in the battery, the next day sells.
    """
    rng = np.random.default_rng(1)
    n = len(diff)
    starts = rng.integers(0, n, size=(draws, -(-n // block)))
    index = ((starts[:, :, None] + np.arange(block)) % n).reshape(draws, -1)[:, :n]
    low, high = np.percentile(diff[index].mean(axis=1), [2.5, 97.5])
    return float(low), float(high)


def main(cache: Path = Path("data/cache"), out: Path = Path("results"), jobs: int = 4) -> None:
    battery = Battery()
    d = load(cache)
    s = slice(d["start"], None)

    ceiling = plan(d["price"][s], d["gen_lf"][s], d["load_lf"][s], battery, battery.soc_min, None)
    with ProcessPoolExecutor(jobs) as pool:
        futures = {name: pool.submit(run_policy, name, d, battery) for name in POLICIES}
        dispatch = {name: future.result() for name, future in futures.items()}

    times = d["times"][s].reset_index(drop=True)
    day = (times - pd.Timedelta(minutes=5)).dt.date
    revenue = pd.DataFrame({name: settle(dispatch[name], d, battery) for name in POLICIES})
    daily = revenue.groupby(day).sum()

    summary = pd.DataFrame(
        {
            "policy": ["ceiling", *POLICIES],
            "description": ["Full-year perfect-foresight ceiling", *POLICIES.values()],
            "revenue_aud": [ceiling.revenue, *revenue.sum()],
            "full_cycles": [
                np.nan,
                *(DT * dispatch[n]["discharge"].sum() / battery.energy_mwh for n in POLICIES),
            ],
        }
    )
    summary["share_of_ceiling"] = summary["revenue_aud"] / ceiling.revenue

    rows = []
    for a, b in [("oracle", "aemo"), ("aemo", "profile"), ("aemo", "rule")]:
        diff = (daily[a] - daily[b]).to_numpy()
        low, high = block_bootstrap(diff)
        rows.append({"comparison": f"{a} - {b}", "per_day": diff.mean(), "low": low, "high": high})

    out.mkdir(exist_ok=True)
    # Unix line endings, so a rerun on Windows doesn't show every CSV as changed.
    csv = {"lineterminator": "\n"}
    summary.to_csv(out / "summary.csv", index=False, float_format="%.4f", **csv)
    pd.DataFrame(rows).to_csv(out / "comparisons.csv", index=False, float_format="%.0f", **csv)
    daily.to_csv(out / "daily.csv", float_format="%.2f", **csv)
    trace = pd.DataFrame({"time": times, "price": d["price"][s]})
    for name in ("oracle", "aemo"):
        for key in ("charge", "discharge", "forecast"):
            trace[f"{name}_{key}"] = dispatch[name][key]
    trace.to_parquet(cache.parent / "trace.parquet", index=False)
    print(summary.to_string(index=False))
    print(pd.DataFrame(rows).to_string(index=False))
