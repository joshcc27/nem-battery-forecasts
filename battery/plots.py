"""The two figures in the README."""

from __future__ import annotations

from pathlib import Path

import matplotlib
import matplotlib.dates
import matplotlib.pyplot as plt
import pandas as pd

from battery.model import DT, Battery

matplotlib.use("Agg")

INK, GREY, BLUE = "#222222", "#8a8a8a", "#2a78d6"


def _style(ax: plt.Axes) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GREY)
    ax.spines["bottom"].set_color(GREY)
    ax.tick_params(colors=INK, labelsize=9)


def share_of_ceiling(summary: pd.DataFrame, path: Path) -> None:
    rows = summary[summary["policy"] != "ceiling"].iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 3.2))
    colours = [BLUE if p == "aemo" else GREY for p in rows["policy"]]
    bars = ax.barh(rows["description"], 100 * rows["share_of_ceiling"], color=colours, height=0.6)
    for bar, share in zip(bars, rows["share_of_ceiling"], strict=True):
        ax.text(
            bar.get_width() + 1,
            bar.get_y() + 0.3,
            f"{100 * share:.2f}%" if share > 0.995 else f"{100 * share:.1f}%",
            va="center",
        )
    ax.set_xlim(0, 110)
    ax.set_xlabel("Share of the perfect-foresight ceiling (%)")
    ax.set_title("Revenue kept, June 2023 to May 2024", loc="left", color=INK)
    _style(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def worst_day(trace: pd.DataFrame, daily: pd.DataFrame, path: Path, battery: Battery) -> None:
    """The day the AEMO-based optimiser fell furthest behind the oracle."""
    day = (daily["oracle"] - daily["aemo"]).idxmax()
    soc = {}
    for name in ("oracle", "aemo"):
        charge, discharge = trace[f"{name}_charge"], trace[f"{name}_discharge"]
        step = DT * (battery.eta * charge - discharge / battery.eta)
        soc[name] = battery.soc_min + step.cumsum()
    on_day = (trace["time"] - pd.Timedelta(minutes=5)).dt.date.astype(str) == str(day)
    t = trace.loc[on_day, "time"]

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(8, 5.5), sharex=True)
    top.plot(t, trace.loc[on_day, "price"], color=INK, lw=1.4, label="Real price")
    top.plot(
        t,
        trace.loc[on_day, "aemo_forecast"],
        color=BLUE,
        lw=1.2,
        ls="--",
        label="AEMO forecast when the hour was committed",
    )
    top.set_ylabel("A$/MWh")
    top.legend(frameon=False, fontsize=9, loc="upper left")
    oracle_aud, aemo_aud = daily.loc[day, "oracle"], daily.loc[day, "aemo"]
    top.set_title(
        f"{pd.Timestamp(day):%d %B %Y}: oracle earned A\\${oracle_aud / 1e6:.2f}M, "
        f"AEMO-based optimiser A\\${aemo_aud / 1e6:.2f}M",
        loc="left",
        color=INK,
    )
    bottom.plot(t, soc["oracle"][on_day], color=GREY, lw=2, label="Oracle")
    bottom.plot(t, soc["aemo"][on_day], color=BLUE, lw=2, label="AEMO-based optimiser")
    bottom.set_ylabel("Stored energy (MWh)")
    bottom.legend(frameon=False, fontsize=9, loc="upper left")
    bottom.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M", tz=t.dt.tz))
    for ax in (top, bottom):
        _style(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main(results: Path = Path("results"), data: Path = Path("data")) -> None:
    summary = pd.read_csv(results / "summary.csv")
    daily = pd.read_csv(results / "daily.csv", index_col=0)
    trace = pd.read_parquet(data / "trace.parquet")
    share_of_ceiling(summary, results / "share_of_ceiling.png")
    worst_day(trace, daily, results / "worst_day.png", Battery())
