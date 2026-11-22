"""Tests for the range model."""

import pytest

from custom_components.ford_ev.range_model import (
    RangeModelState,
    TripInput,
    account_trip,
    add_charge,
    calibrate,
    calibration_allowed,
    climate_kw,
    drive_kwh,
    model_soc,
    predicted_kwh_per_km,
    seed_drive_factor,
)


def test_drive_kwh():
    assert drive_kwh(100, 20) == pytest.approx(16.0)
    assert drive_kwh(100, 0) == pytest.approx(19.2)
    assert drive_kwh(100, 25) == pytest.approx(16.0)


def test_climate_kw():
    assert climate_kw(0, 21, True) == pytest.approx(2.52)
    assert climate_kw(30, 21, True) == pytest.approx(0.72)
    assert climate_kw(0, 21, False) == 0


def test_predicted_kwh_per_km():
    assert predicted_kwh_per_km(0, 1, 1, 40, 21, True) == pytest.approx(0.255)


def test_predicted_kwh_per_km_floors_speed():
    assert predicted_kwh_per_km(20, 1, 1, 1, 21, True) == pytest.approx(0.16 + 0.12 / 5)


def test_trip_valid_duration():
    st = RangeModelState(model_energy=50, odo_accounted=1000)
    st = account_trip(st, TripInput(1030, 0, 1800, 0.0, 10.0, 21, True), 60)
    assert st.cycle_x_drive == pytest.approx(5.52)
    assert st.cycle_x_hvac == pytest.approx(0.96)
    assert st.model_energy == pytest.approx(43.52)
    assert st.avg_speed == pytest.approx(48.0)
    assert st.cycle_km == 30
    assert st.odo_accounted == 1030


def test_trip_missing_start_uses_average_speed():
    st = account_trip(RangeModelState(model_energy=50, odo_accounted=1000), TripInput(1030, None, 1800, None, 10.0, 21, True), 60)
    assert st.cycle_x_drive == pytest.approx(5.28)
    assert st.cycle_x_hvac == pytest.approx(0.88, abs=1e-3)
    assert st.avg_speed == 45.0


def test_trip_implausible_speed_uses_average_speed():
    st = account_trip(RangeModelState(model_energy=50, odo_accounted=1000), TripInput(1100, 0, 600, 10.0, 10.0, 21, False), 60)
    assert st.avg_speed == 45.0
    assert st.cycle_x_hvac == 0


def test_trip_clears_trip_start():
    st = RangeModelState(model_energy=50, odo_accounted=1000, trip_start_ts=0, trip_start_temp=5)
    st = account_trip(st, TripInput(1030, 0, 1800, 5, 5, 21, True), 60)
    assert st.trip_start_ts is None and st.trip_start_temp is None


def test_trip_odometer_backwards_resyncs():
    st = account_trip(RangeModelState(model_energy=50, odo_accounted=1000), TripInput(990, 0, 600, 5, 5, 21, True), 60)
    assert st.odo_accounted == 990
    assert st.model_energy == 50


def test_short_trip_accumulates():
    st = account_trip(RangeModelState(model_energy=50, odo_accounted=1000), TripInput(1000.3, 0, 60, 5, 5, 21, True), 60)
    assert st.odo_accounted == 1000
    assert st.cycle_km == 0


def test_first_trip_sets_baseline():
    st = account_trip(RangeModelState(model_energy=50), TripInput(1000, 0, 600, 5, 5, 21, True), 60)
    assert st.odo_accounted == 1000
    assert st.model_energy == 50


def test_model_energy_floor_is_minus_capacity():
    st = account_trip(RangeModelState(model_energy=-59, odo_accounted=0), TripInput(100, None, 1, None, 20, 21, False), 60)
    assert st.model_energy == -60


def test_add_charge_caps_at_capacity():
    st = add_charge(RangeModelState(model_energy=58), 5, 60)
    assert st.model_energy == 60
    assert st.cycle_charged == 5


def test_add_charge_without_model_energy():
    st = add_charge(RangeModelState(), 5, 60)
    assert st.model_energy is None and st.cycle_charged == 5


def test_calibrate_valid_fit():
    st = RangeModelState(cycle_x_drive=40, cycle_x_hvac=10, cycle_charged=45, cycle_km=300, model_energy=10)
    st2, r = calibrate(st, 60, 1_000_000, 40, 30, 35)
    assert r.valid
    assert r.a == pytest.approx(0.8889, abs=1e-4)
    assert r.b == pytest.approx(0.9722, abs=1e-4)
    assert r.error_pct == pytest.approx(11.11, abs=0.01)
    assert r.capacity_estimate_kwh == pytest.approx(57.14, abs=0.01)
    assert r.soc_start_charger == pytest.approx(33.33, abs=0.01)
    assert st2.model_energy == 60
    assert st2.cycle_charged == 0 and st2.cycle_x_drive == 0 and st2.cycle_km == 0
    assert st2.calibrations == 1
    assert st2.last_calibration == 1_000_000
    assert st2.last_result["valid"] is True


def test_calibrate_prior_pulls_towards_seed():
    st = RangeModelState(a=1.25, a0=1.25, cycle_x_drive=40, cycle_x_hvac=10, cycle_charged=50 * 1.25)
    _, r = calibrate(st, 60, 1, None, None, None)
    assert r.a > 1.0


def test_calibrate_invalid_keeps_factors_but_resets():
    st = RangeModelState(cycle_x_drive=1.5, cycle_x_hvac=0.5, cycle_charged=2, model_energy=10)
    st2, r = calibrate(st, 60, 1, None, None, None)
    assert not r.valid
    assert st2.a == 1.0 and st2.s11 == 0
    assert st2.model_energy == 60
    assert st2.calibrations == 0


def test_calibrate_ratio_out_of_range_invalid():
    st = RangeModelState(cycle_x_drive=10, cycle_x_hvac=0, cycle_charged=25)
    assert not calibrate(st, 60, 1, None, None, None)[1].valid


def test_capacity_estimate_needs_low_start():
    st = RangeModelState(cycle_x_drive=40, cycle_x_hvac=10, cycle_charged=45)
    assert calibrate(st, 60, 1, 10, 85, None)[1].capacity_estimate_kwh is None


def test_calibration_allowed_rules():
    assert not calibration_allowed(RangeModelState(cycle_charged=0.5), 10**6)
    assert calibration_allowed(RangeModelState(cycle_charged=2), 10**6)
    assert not calibration_allowed(RangeModelState(cycle_charged=2, last_calibration=10**6 - 3600), 10**6)


@pytest.mark.parametrize(("soc", "rng", "expected"), [(80, 300, 1.0), (80, 240, 1.25), (10, 300, 1.0), (80, None, 1.0), (80, 60, 2.0)])
def test_seed_drive_factor(soc, rng, expected):
    assert seed_drive_factor(soc, rng, 60, 20) == pytest.approx(expected)


def test_model_soc_clamped():
    assert model_soc(RangeModelState(model_energy=30), 60) == 50
    assert model_soc(RangeModelState(model_energy=-5), 60) == 0
    assert model_soc(RangeModelState(model_energy=None), 60) is None
