"""SA1 dispatch prices and AEMO's published price forecasts, cached by month."""

from __future__ import annotations

import io
import os
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import numpy as np
import pandas as pd

AEST = "Etc/GMT-10"  # NEM time: UTC+10 all year
REGION = "SA1"
PRICE_FLOOR = -1_000.0
PRICE_CAP = {"2022-23": 15_500.0, "2023-24": 16_600.0}
# Hornsdale 275 kV marginal loss factors (generation, load) from AEMO's MLF reports.
LOSS_FACTORS = {"2022-23": (0.9652, 0.9681), "2023-24": (0.9653, 0.9682)}

MMSDM = "https://nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/"
FORECAST_TABLES = {
    # name: (archive folder, file stem, run column, target column)
    "p5min": ("DATA", "PUBLIC_DVD_P5MIN_REGIONSOLUTION", "RUN_DATETIME", "INTERVAL_DATETIME"),
    "predispatch": (
        "PREDISP_ALL_DATA",
        "PUBLIC_DVD_PREDISPATCHPRICE",
        "PREDISPATCHSEQNO",
        "DATETIME",
    ),
}


def financial_year(start: pd.Timestamp) -> str:
    """The NEM financial year an interval starting at ``start`` falls in, e.g. '2023-24'."""
    year = start.year if start.month >= 7 else start.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


def for_year(table: dict, fy: str, name: str):
    """Look up a per-financial-year constant, with a clear error for years not yet entered."""
    try:
        return table[fy]
    except KeyError:
        known = ", ".join(sorted(table))
        raise ValueError(f"no {name} for {fy} (have {known}); add it in battery/data.py") from None


def months(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """The months whose partitions hold the intervals ending in (start, end]."""
    first = start.tz_localize(None).to_period("M").start_time
    last = (end - pd.Timedelta(minutes=5)).tz_localize(None).to_period("M").start_time
    return list(pd.date_range(first, last, freq="MS"))


def _partition(cache: Path, table: str, month: pd.Timestamp) -> Path:
    return (
        cache
        / "partitions"
        / table
        / f"region={REGION}"
        / f"year={month.year}"
        / f"month={month.month:02d}.parquet"
    )


def load_prices(start: pd.Timestamp, end: pd.Timestamp, cache: Path) -> pd.DataFrame:
    """Five-minute prices with interval ends in (start, end]."""
    frames = []
    for month in months(start, end):
        path = _partition(cache, "dispatchprice", month)
        if not path.exists():
            _fetch_prices(month, path, cache)
        frames.append(read_partition(path, ["settlementdate", "rrp"], ["settlementdate"]))
    prices = pd.concat(frames)
    prices = prices[(prices.settlementdate > start) & (prices.settlementdate <= end)]
    prices = prices.sort_values("settlementdate").reset_index(drop=True)
    expected = pd.date_range(start + pd.Timedelta(minutes=5), end, freq="5min")
    same = len(prices) == len(expected)
    if not same or (to_utc_ns(prices.settlementdate) != to_utc_ns(expected)).any():
        raise ValueError("price data has gaps or duplicates")
    return prices


def read_partition(path: Path, columns: list[str], times: list[str]) -> pd.DataFrame:
    """Read a cache partition with its time columns in NEM time at nanosecond resolution.

    Partitions written by different versions label the same UTC+10 clock
    differently ('UTC+10:00' or 'Etc/GMT-10') and at different resolutions.
    pandas won't concatenate those as datetimes, so normalise each one first.
    """
    frame = pd.read_parquet(path, columns=columns)
    for column in times:
        frame[column] = frame[column].dt.tz_convert(AEST).dt.as_unit("ns")
    return frame


def _fetch_prices(month: pd.Timestamp, path: Path, cache: Path) -> None:
    from nemosis import dynamic_data_compiler

    raw_dir = cache / "nemosis_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    end = month + pd.offsets.MonthBegin(1)
    raw = dynamic_data_compiler(
        month.strftime("%Y/%m/%d 00:00:00"),
        end.strftime("%Y/%m/%d 00:00:00"),
        "DISPATCHPRICE",
        str(raw_dir),
        select_columns=["SETTLEMENTDATE", "REGIONID", "RRP", "INTERVENTION"],
        filter_cols=["REGIONID"],
        filter_values=([REGION],),
        keep_csv=False,
    )
    raw = raw[raw["INTERVENTION"].astype(int) == 0]
    when = pd.to_datetime(raw["SETTLEMENTDATE"]).dt.tz_localize(AEST)
    keep = (when > month.tz_localize(AEST)) & (when <= end.tz_localize(AEST))
    frame = pd.DataFrame({"settlementdate": when[keep], "rrp": raw.loc[keep, "RRP"].astype(float)})
    write_parquet(frame, path)


def write_parquet(frame: pd.DataFrame, path: Path) -> None:
    """Write a cache partition so that a killed run never leaves a half-written file.

    Partitions are only fetched when missing, so a truncated file would be
    trusted on every later run. Writing beside it and renaming is atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    frame.to_parquet(partial, index=False)
    os.replace(partial, path)


def download(url: str, attempts: int = 3, timeout: int = 900) -> bytes:
    """Fetch a URL, retrying transient network failures with a growing pause."""
    request = urllib.request.Request(url, headers={"User-Agent": "nem-battery-forecasts"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == attempts:  # a 404 won't fix itself
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts:
                raise
        time.sleep(30 * attempt)


@dataclass(frozen=True)
class Runs:
    """Every run of one AEMO forecast, sorted by the time it was published."""

    published: np.ndarray  # one per run, UTC nanoseconds
    starts: np.ndarray  # row where each run begins, plus a final end marker
    targets: np.ndarray  # UTC nanoseconds of each forecast interval or period end
    rrp: np.ndarray

    def latest(self, at: np.int64) -> tuple[np.ndarray, np.ndarray, np.int64 | None]:
        """Targets, prices, and publish time of the newest run published by ``at``."""
        i = int(np.searchsorted(self.published, at, side="right")) - 1
        if i < 0:
            return self.targets[:0], self.rrp[:0], None
        rows = slice(self.starts[i], self.starts[i + 1])
        return self.targets[rows], self.rrp[rows], self.published[i]


def load_forecasts(table: str, start: pd.Timestamp, end: pd.Timestamp, cache: Path) -> Runs:
    frames = []
    for month in months(start, end):
        path = _partition(cache, f"{table}_forecast", month)
        if not path.exists():
            _fetch_forecasts(table, month, path)
        columns = ["run_id", "target", "rrp", "published"]
        frames.append(read_partition(path, columns, ["target", "published"]))
    # A run near a month boundary can appear in both months' files, each with
    # the publish time of only its own rows. Take the latest across all files,
    # so every row of a run shares one publish time and the run stays in one piece.
    frame = pd.concat(frames).drop_duplicates(["run_id", "target"])
    frame["published"] = frame.groupby("run_id")["published"].transform("max")
    frame = frame.sort_values(["published", "run_id", "target"]).reset_index(drop=True)
    first_rows = np.flatnonzero(frame["run_id"].ne(frame["run_id"].shift()).to_numpy())
    return Runs(
        published=to_utc_ns(frame["published"].iloc[first_rows]),
        starts=np.append(first_rows, len(frame)),
        targets=to_utc_ns(frame["target"]),
        rrp=frame["rrp"].to_numpy(dtype=float),
    )


def _fetch_forecasts(table: str, month: pd.Timestamp, path: Path) -> None:
    folder, stem, _, _ = FORECAST_TABLES[table]
    url = (
        f"{MMSDM}{month.year}/MMSDM_{month.year}_{month.month:02d}/"
        f"MMSDM_Historical_Data_SQLLoader/{folder}/{stem}_{month:%Y%m}010000.zip"
    )
    archive = zipfile.ZipFile(io.BytesIO(download(url)))
    with archive.open(archive.namelist()[0]) as handle:
        frame = parse_forecasts(table, handle)
    write_parquet(frame, path)


def parse_forecasts(table: str, handle: IO[bytes] | IO[str]) -> pd.DataFrame:
    """Turn one MMSDM forecast CSV into rows of (run_id, target, rrp, published)."""
    _, _, run_col, target_col = FORECAST_TABLES[table]
    columns = ["I", run_col, "INTERVENTION", "REGIONID", target_col, "RRP", "LASTCHANGED"]
    chunks = pd.read_csv(handle, skiprows=1, usecols=columns, dtype=str, chunksize=500_000)
    raw = pd.concat(
        c[(c["I"] == "D") & (c["REGIONID"] == REGION) & (c["INTERVENTION"] == "0")] for c in chunks
    )

    def aest(column: str) -> pd.Series:
        return pd.to_datetime(raw[column], format="%Y/%m/%d %H:%M:%S").dt.tz_localize(AEST)

    frame = pd.DataFrame(
        {"run_id": raw[run_col], "target": aest(target_col), "rrp": raw["RRP"].astype(float)}
    )
    # A run can be used once its last row has been published.
    frame["published"] = aest("LASTCHANGED").groupby(raw[run_col]).transform("max")
    return frame.reset_index(drop=True)


def to_utc_ns(values: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    return pd.DatetimeIndex(values).tz_convert("UTC").as_unit("ns").asi8
