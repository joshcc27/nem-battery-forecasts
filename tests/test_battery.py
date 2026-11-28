import io
import urllib.error

import numpy as np
import pandas as pd
import pytest

from battery import data
from battery.data import (
    AEST,
    Runs,
    _partition,
    download,
    financial_year,
    for_year,
    load_forecasts,
    load_prices,
    parse_forecasts,
    to_utc_ns,
    write_parquet,
)
from battery.forecasts import MINUTE, Aemo, Perfect, Profile
from battery.model import DT, Battery, plan
from battery.policies import _rule, clear, fit_to_soc, run
from battery.study import block_bootstrap, settle

SMALL = Battery(power_mw=1.0, energy_mwh=2.0, round_trip=0.81, deg_cost=1.0)


def random_prices(days: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    daily = 50 + 60 * np.sin(np.linspace(0, 2 * np.pi, 288, endpoint=False))
    return np.tile(daily, days) + rng.normal(0, 30, 288 * days)


def test_buys_cheap_and_sells_dear():
    price = np.r_[np.zeros(24), np.full(24, 100.0)]
    p = plan(price, np.ones(48), np.ones(48), SMALL, SMALL.soc_min, commit=48)
    assert p.charge[24:].sum() == 0 and p.discharge[:24].sum() == 0
    assert p.charge[:24].sum() > 0
    stored = SMALL.soc_min + DT * (SMALL.eta * p.charge.sum() - p.discharge.sum() / SMALL.eta)
    # It ends empty, because energy left in store is worth nothing.
    assert stored == pytest.approx(SMALL.soc_min, abs=1e-6)
    assert p.revenue == pytest.approx(DT * 99 * p.discharge.sum(), rel=1e-6)


def test_stored_energy_is_worth_the_best_later_sale():
    price = np.r_[np.zeros(12), np.full(12, 100.0)]
    p = plan(price, np.ones(24), np.ones(24), SMALL, SMALL.soc_min, commit=12)
    # A MWh in store after the cheap hour sells for 100 less degradation, after discharge losses.
    assert p.value[-1] == pytest.approx((100 - SMALL.deg_cost) * SMALL.eta, rel=1e-3)


def test_negative_prices_never_charge_and_discharge_at_once():
    # At -100 burning energy pays, so the plan must use the binary mode.
    price = np.full(12, -100.0)
    p = plan(price, np.ones(12), np.ones(12), SMALL, SMALL.soc_max, commit=12)
    assert not np.isnan(p.mode).any()
    assert set(np.unique(p.mode)) <= {0.0, 1.0}
    assert (np.minimum(p.charge, p.discharge) == 0).all()
    # The ceiling may burn energy, so it can only do better.
    ceiling = plan(price, np.ones(12), np.ones(12), SMALL, SMALL.soc_max, commit=None)
    assert ceiling.revenue >= p.revenue - 1e-9


def test_positive_prices_leave_the_mode_free():
    p = plan(random_prices(1) + 200, np.ones(288), np.ones(288), SMALL, 1.0, commit=12)
    assert np.isnan(p.mode).all()


def test_a_right_forecast_clears_exactly_as_planned():
    price = random_prices(1)
    p = plan(price, np.ones(288), np.ones(288), SMALL, 1.0, commit=12)
    charge, discharge = clear(price[:12], p, np.ones(12), np.ones(12), SMALL)
    np.testing.assert_allclose(charge, p.charge, atol=1e-9)
    np.testing.assert_allclose(discharge, p.discharge, atol=1e-9)


def test_clearing_skips_a_spike_the_plan_would_have_charged_into():
    price = np.r_[np.zeros(12), np.full(12, 100.0)]
    p = plan(price, np.ones(24), np.ones(24), SMALL, SMALL.soc_min, commit=12)
    charge, _ = clear(np.full(12, 5_000.0), p, np.ones(12), np.ones(12), SMALL)
    assert charge.sum() == 0


def test_no_policy_beats_the_ceiling():
    price = random_prices(4)
    cap = np.full(len(price), 15_000.0)
    ceiling = plan(price[288:], np.ones(864), np.ones(864), SMALL, SMALL.soc_min, commit=None)
    for forecaster in (Perfect(price), Profile(days=1)):
        out = run(price, np.ones(1152), np.ones(1152), cap, 288, forecaster, SMALL)
        revenue = DT * (price[288:] * (out["discharge"] - out["charge"]) - out["discharge"])
        assert revenue.sum() <= ceiling.revenue + 1e-6


def test_the_oracle_gets_close_to_the_ceiling():
    price = random_prices(4)
    cap = np.full(len(price), 15_000.0)
    ceiling = plan(price[288:], np.ones(864), np.ones(864), SMALL, SMALL.soc_min, commit=None)
    out = run(price, np.ones(1152), np.ones(1152), cap, 288, Perfect(price), SMALL)
    revenue = DT * (price[288:] * (out["discharge"] - out["charge"]) - out["discharge"])
    assert revenue.sum() > 0.98 * ceiling.revenue


def test_decisions_never_see_later_prices():
    price = random_prices(3)
    cap = np.full(len(price), 15_000.0)
    spoiled = price.copy()
    cut = 288 + 360
    spoiled[cut:] = 9_000.0
    a = run(price, np.ones(864), np.ones(864), cap, 288, Profile(days=1), SMALL)
    b = run(spoiled, np.ones(864), np.ones(864), cap, 288, Profile(days=1), SMALL)
    # Decisions are hourly, so everything before the hour containing the cut must match.
    keep = (cut - 288) // 12 * 12
    np.testing.assert_array_equal(a["charge"][:keep], b["charge"][:keep])
    np.testing.assert_array_equal(a["discharge"][:keep], b["discharge"][:keep])


def test_soc_limits_hold():
    charge, _ = fit_to_soc(np.ones(24), np.zeros(24), SMALL.soc_max - 0.1, SMALL)
    assert DT * SMALL.eta * charge.sum() == pytest.approx(0.1)


def test_rule_trades_the_cheapest_hour_only_when_the_spread_pays():
    # Hourly forecast: 0, 1, then 50 for most of the day, then two hours at 500.
    wide = np.r_[np.zeros(12), np.ones(12), np.full(240, 50.0), np.full(24, 500.0)]
    charge, discharge = _rule(wide, 12, 1.0, 1.0, SMALL)
    assert (charge == SMALL.power_mw).all() and discharge.sum() == 0
    # With the rest of the day at 1.5, the spread no longer covers losses and degradation.
    thin = np.r_[np.zeros(12), np.full(276, 1.5)]
    charge, discharge = _rule(thin, 12, 1.0, 1.0, SMALL)
    assert charge.sum() == 0 and discharge.sum() == 0


def runs(published: list[int], targets: list[list[int]], prices: list[list[float]]) -> Runs:
    starts = np.cumsum([0] + [len(t) for t in targets])
    return Runs(
        published=np.array(published, dtype=np.int64),
        starts=starts,
        targets=np.concatenate([np.array(t, dtype=np.int64) for t in targets]),
        rrp=np.concatenate([np.array(p, dtype=float) for p in prices]),
    )


def test_aemo_uses_only_runs_published_before_the_decision():
    times = np.arange(1, 7 * 288 + 13, dtype=np.int64) * 5 * MINUTE
    seen = np.full(7 * 288, 50.0)
    now = times[len(seen) - 1]
    nxt = times[len(seen)]
    early = runs([now - MINUTE], [[nxt]], [[111.0]])
    late = runs([now + MINUTE], [[nxt]], [[222.0]])
    stale = runs([now - 20 * MINUTE], [[nxt]], [[333.0]])
    empty = runs([0], [[0]], [[0.0]])
    assert Aemo(times, early, empty)(seen, 1)[0] == 111.0
    assert Aemo(times, late, empty)(seen, 1)[0] == 50.0  # not published yet: profile
    assert Aemo(times, stale, empty)(seen, 1)[0] == 50.0  # too old: profile


def test_aemo_stitches_p5min_then_predispatch_then_profile():
    times = np.arange(1, 7 * 288 + 61, dtype=np.int64) * 5 * MINUTE
    seen = np.full(7 * 288, 50.0)
    now = times[len(seen) - 1]  # on a half-hour boundary
    # Pre-dispatch prices every half hour out to five hours: 101, 102, ...
    periods = [now + 30 * MINUTE * (j + 1) for j in range(10)]
    predispatch = runs([now - 10 * MINUTE], [periods], [[101.0 + j for j in range(10)]])
    # P5MIN covers the next hour at 7.
    p5min_targets = [now + 5 * MINUTE * (j + 1) for j in range(12)]
    p5min = runs([now - MINUTE], [p5min_targets], [[7.0] * 12])

    f = Aemo(times, p5min, predispatch)(seen, 60)
    np.testing.assert_array_equal(f[:12], 7.0)  # P5MIN wins where it reaches
    # An interval takes the half-hour period it ends in: the 13th-18th end in the third.
    np.testing.assert_array_equal(f[12:18], 103.0)
    np.testing.assert_array_equal(f[42:48], 108.0)  # the last period inside four hours
    np.testing.assert_array_equal(f[48:], 50.0)  # beyond four hours: the profile


def test_aemo_ignores_stale_predispatch():
    times = np.arange(1, 7 * 288 + 13, dtype=np.int64) * 5 * MINUTE
    seen = np.full(7 * 288, 50.0)
    now = times[len(seen) - 1]
    old = runs([now - 61 * MINUTE], [[now + 30 * MINUTE]], [[999.0]])
    empty = runs([0], [[0]], [[0.0]])
    np.testing.assert_array_equal(Aemo(times, empty, old)(seen, 6), 50.0)


def test_bootstrap_of_a_constant_difference_has_no_width():
    low, high = block_bootstrap(np.full(30, 7.0))
    assert low == pytest.approx(7.0) and high == pytest.approx(7.0)


def test_settle_applies_loss_factors_and_degradation():
    d = {
        "start": 1,
        "price": np.array([999.0, 100.0, -50.0]),
        "gen_lf": np.array([1.0, 0.9, 0.9]),
        "load_lf": np.array([1.0, 0.95, 0.95]),
    }
    dispatch = {"charge": np.array([0.0, 10.0]), "discharge": np.array([20.0, 0.0])}
    revenue = settle(dispatch, d, SMALL)
    # Sell 20 MW at 100 x 0.9, less degradation; then charge 10 MW and get paid at -50.
    expected = [DT * (20 * 100 * 0.9 - 20 * SMALL.deg_cost), DT * 10 * 50 * 0.95]
    np.testing.assert_allclose(revenue, expected)


FORECAST_CSV = """C,NEMP.WORLD,PREDISPATCH,AEMO,PUBLIC,2023/07/01,00:00:00
I,PREDISPATCH,REGION_PRICES,1,PREDISPATCHSEQNO,REGIONID,INTERVENTION,DATETIME,RRP,LASTCHANGED
D,PREDISPATCH,REGION_PRICES,1,2023070101,SA1,0,2023/07/01 01:00:00,80.5,2023/07/01 00:31:00
D,PREDISPATCH,REGION_PRICES,1,2023070101,SA1,0,2023/07/01 01:30:00,90.0,2023/07/01 00:32:00
D,PREDISPATCH,REGION_PRICES,1,2023070101,VIC1,0,2023/07/01 01:00:00,40.0,2023/07/01 00:31:00
D,PREDISPATCH,REGION_PRICES,1,2023070101,SA1,1,2023/07/01 01:00:00,999.0,2023/07/01 00:31:00
C,"END OF REPORT",7
"""


def test_parse_forecasts_keeps_sa1_pricing_rows_and_the_last_publish_time():
    frame = parse_forecasts("predispatch", io.StringIO(FORECAST_CSV))
    assert list(frame["rrp"]) == [80.5, 90.0]
    assert (frame["run_id"] == "2023070101").all()
    assert frame["target"].iloc[0] == pd.Timestamp("2023-07-01 01:00", tz=AEST)
    # Usable only once its last row was published.
    assert (frame["published"] == pd.Timestamp("2023-07-01 00:32", tz=AEST)).all()


def test_a_run_split_across_months_stays_in_one_piece(tmp_path):
    def at(clock: str) -> pd.Timestamp:
        return pd.Timestamp(f"2023-06-30 {clock}", tz=AEST)

    def rows(run_id, published, targets):
        return pd.DataFrame(
            {
                "run_id": run_id,
                "target": [at(t) for t in targets],
                "rrp": 1.0,
                "published": at(published),
            }
        )

    june = pd.concat([rows("R", "10:00", ["10:05", "10:10"]), rows("A", "10:05", ["10:10"])])
    july = rows("R", "10:10", ["10:10", "10:15"])  # overlaps June on 10:10
    write_parquet(june, _partition(tmp_path, "p5min_forecast", pd.Timestamp("2023-06-01")))
    write_parquet(july, _partition(tmp_path, "p5min_forecast", pd.Timestamp("2023-07-01")))

    loaded = load_forecasts(
        "p5min", pd.Timestamp("2023-06-15", tz=AEST), pd.Timestamp("2023-07-15", tz=AEST), tmp_path
    )
    assert len(loaded.published) == 2  # A and R, not A and two halves of R
    targets, _, _ = loaded.latest(to_utc_ns(pd.DatetimeIndex([at("11:00")]))[0])
    assert len(targets) == 3


def test_write_parquet_replaces_without_leaving_a_partial_file(tmp_path):
    path = tmp_path / "a" / "month=01.parquet"
    write_parquet(pd.DataFrame({"x": [1]}), path)
    write_parquet(pd.DataFrame({"x": [2]}), path)
    assert pd.read_parquet(path)["x"].tolist() == [2]
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_download_retries_transient_failures(monkeypatch):
    calls = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.URLError("reset")
        return Response(b"ok")

    monkeypatch.setattr(data.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(data.time, "sleep", lambda s: None)
    assert download("https://example.invalid/x.zip") == b"ok"
    assert len(calls) == 2


def test_download_gives_up_on_a_missing_file(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(data.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(data.time, "sleep", lambda s: pytest.fail("should not retry a 404"))
    with pytest.raises(urllib.error.HTTPError):
        download("https://example.invalid/x.zip")


def test_financial_years_and_missing_constants():
    assert financial_year(pd.Timestamp("2023-06-30 23:55")) == "2022-23"
    assert financial_year(pd.Timestamp("2023-07-01 00:00")) == "2023-24"
    with pytest.raises(ValueError, match="no price cap for 2030-31"):
        for_year({"2023-24": 1.0}, "2030-31", "price cap")


def test_prices_load_from_partitions_with_different_time_zone_labels(tmp_path):
    # Older partitions say 'UTC+10:00', newer ones 'Etc/GMT-10': the same clock.
    def month_of(start: str, tz: str, unit: str) -> pd.DataFrame:
        ends = pd.date_range(start, periods=2, freq="5min", tz=AEST) + pd.Timedelta(minutes=5)
        return pd.DataFrame({"settlementdate": ends.tz_convert(tz).as_unit(unit), "rrp": 1.0})

    def path(month: str):
        return _partition(tmp_path, "dispatchprice", pd.Timestamp(month))

    write_parquet(month_of("2023-06-30 23:50", "UTC+10:00", "us"), path("2023-06-01"))
    write_parquet(month_of("2023-07-01 00:00", AEST, "ns"), path("2023-07-01"))
    start, end = (pd.Timestamp(t, tz=AEST) for t in ("2023-06-30 23:50", "2023-07-01 00:10"))
    prices = load_prices(start, end, tmp_path)
    assert len(prices) == 4
    assert str(prices["settlementdate"].dt.tz) == AEST
