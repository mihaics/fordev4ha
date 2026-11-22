# Ford EV for Home Assistant: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A HACS custom integration, `ford_ev`, that turns FordConnect Query (FCQ) entities into reports, charging cost, an independent self-calibrating range model, notifications and a self-building dashboard, configured entirely from the UI.

**Architecture:**
- **Pure Python core** with no Home Assistant imports, unit-tested in isolation: units, accounting, periods, range model.
- **One engine object per config entry.** It subscribes to the vehicle's FCQ entity states, applies the pure functions, persists everything in a versioned `Store`, and signals entities over the dispatcher.
- **Alert layer** on top of the engine: it raises events, and a notifier turns events into notifications.
- **Dashboard:** a plain-JS strategy served by the integration.

**Tech stack:**
- Python 3.14, Home Assistant ≥ 2026.5.
- Tests: pytest-homeassistant-custom-component 0.13.375 (pins HA 2026.10.4); `node --test` (Node 24) for the strategy.
- CI: GitHub Actions running hassfest, the HACS action and pytest.

**Spec:** `docs/design.md`. Read it before any task; section numbers below (§) refer to it.

## Global Constraints

- **Identity:** domain `ford_ev`, display name "Ford EV", version `0.1.0`, `iot_class` `calculated`, `integration_type` `device`, `config_flow` true. Manifest `requirements: []`, `dependencies: ["http"]`, `after_dependencies: ["frontend"]`.
- **Home Assistant floor:** 2026.5.0 (`hacs.json`). Test environment: `pytest-homeassistant-custom-component==0.13.375`, Python 3.14.
- **No network calls** made by the integration. No vehicle data leaves Home Assistant.
- **Internal units are km, °C and kWh.** Every FCQ, temperature or weather value is converted from its `unit_of_measurement` (or `temperature_unit`) on read.
- **FCQ entities are matched by `platform == "fordconnect_query"` plus `translation_key`, never by entity ID.**
- **Unknown and unavailable states are ignored.** Transitions are computed against the last valid value per FCQ key, not against `from_state`.
- **Test data:**
  - fake VIN `WF0TESTVIN0000001`;
  - fake coordinates `0.0, 0.0`;
  - device name "Test Explorer".
  - No real entity IDs, device names, phone names, locations, addresses or tokens anywhere in the repository.
- **Language:** English only for notification texts; UI strings go in `strings.json` and `translations/en.json`.
- **Commits:**
  - no AI or tool attribution in commit messages, docs or code comments;
  - every commit passes the local, uncommitted pre-commit denylist hook;
  - nothing is pushed to the public remote until Task 14, after explicit confirmation.
- **Commands:**
  - one-time setup: `uv venv -p 3.14 .venv && VIRTUAL_ENV=.venv uv pip install -r requirements_test.txt`
  - tests: `.venv/bin/pytest`
  - JS tests: `node --test tests/js/`

## Review Focus

1. **Imperial users.** FCQ odometer in `mi` and temperature sensor in `°F`: trips, periods and the model must work in km/°C internally. Tests are in Task 1 (conversion) and Task 8 (a trip with a miles odometer).
2. **Home Assistant restart in the middle of things:** while a charging session's 2-minute end delay is pending, while a trip's 2-minute accounting delay is pending, or while the car charges. Expected: no lost or double-counted session or trip. Tests are in Task 7 and Task 8.
3. **FCQ outage between transitions** (ignition `ON` → `unavailable` → `OFF`; `evccstatus` `C` → `unavailable` → `B`). Expected: the trip or session ends normally. Tests are in Task 7 and Task 8.
4. **Second vehicle or second entry.** An already configured VIN is not offered; two entries keep separate stores and entities. Tests are in Task 6 and Task 7.
5. **Notification target removed** (a phone uninstalled, so the `notify.mobile_app_x` service is missing). Expected: a warning is logged, other targets still get the notification, the event entity still fires. Test is in Task 10.

---

## File structure

```
custom_components/ford_ev/
  manifest.json, const.py         identity, config keys, defaults, merged_config()
  units.py                        pure: state → float in km / °C
  accounting.py                   pure: SoC energy accounting, charger counter
  periods.py                      pure: day/week/month snapshots
  range_model.py                  pure: trip energy, trip accounting, calibration fit
  state.py                        EngineState + (de)serialisation, Store version
  discovery.py                    FCQ vehicles from entity/device registries
  engine.py                       FordEvEngine: subscriptions, persistence, sessions, trips, calibration
  alerts.py                       DelayedCondition, AlertManager, build_notification()
  notify.py                       async_send() to notify services / entities
  entity.py                       FordEvEntity base (device_info, update subscription)
  sensor.py, binary_sensor.py, number.py, switch.py, event.py
  config_flow.py, diagnostics.py, services.yaml, strings.json, translations/en.json, icons.json
  __init__.py                     async_setup (static path, JS url, service), setup/unload entry
  frontend/ford-ev-strategy.js    dashboard strategy
tests/
  conftest.py                     enable custom integrations, FakeFcq fixture, setup helper
  test_units.py, test_accounting.py, test_periods.py, test_range_model.py,
  test_discovery.py, test_config_flow.py, test_engine.py, test_engine_model.py,
  test_entities.py, test_alerts.py, test_services_diagnostics.py, test_frontend.py
  js/strategy.test.mjs
pyproject.toml, requirements_test.txt, hacs.json, .github/workflows/ci.yml, README.md
```

---

### Task 1: Test harness and unit conversion

**Files:**
- Create: `pyproject.toml`, `requirements_test.txt`, `custom_components/ford_ev/__init__.py` (docstring only for now), `custom_components/ford_ev/units.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_units.py`

**Interfaces:**
- Produces, in `units.py`:
  - `state_number(state: State | None) -> float | None`: None for a missing state, `unknown`, `unavailable` or a non-numeric value such as `Unsupported`.
  - `to_km(value: float, unit: str | None) -> float`: `None` unit means km.
  - `to_celsius(value: float, unit: str | None) -> float`: `None` unit means °C.
  - `state_km(state: State | None) -> float | None` and `state_celsius(state: State | None) -> float | None`: read `unit_of_measurement`.
  - `weather_celsius(state: State | None) -> float | None`: attribute `temperature`, unit from attribute `temperature_unit`.

- [ ] **Step 1:** Write `pyproject.toml` with `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "function"`, `testpaths = ["tests"]`. Write `requirements_test.txt` with `pytest-homeassistant-custom-component==0.13.375`. Write `tests/conftest.py` with an autouse fixture that requests `enable_custom_integrations`. Run the one-time setup command from Global Constraints.
- [ ] **Step 2: Write the failing tests** in `tests/test_units.py`:

```python
def test_state_km_converts_miles():
    assert state_km(State("sensor.o", "100", {"unit_of_measurement": "mi"})) == pytest.approx(160.934, abs=1e-3)
def test_state_km_keeps_km():
    assert state_km(State("sensor.o", "326.0", {"unit_of_measurement": "km"})) == 326.0
@pytest.mark.parametrize("raw", ["unknown", "unavailable", "Unsupported", ""])
def test_non_numeric_is_none(raw):
    assert state_number(State("sensor.x", raw)) is None
def test_missing_state_is_none():
    assert state_km(None) is None
def test_state_celsius_from_fahrenheit():
    assert state_celsius(State("sensor.t", "68", {"unit_of_measurement": "°F"})) == pytest.approx(20.0)
def test_weather_celsius_uses_temperature_unit():
    assert weather_celsius(State("weather.h", "sunny", {"temperature": 50, "temperature_unit": "°F"})) == pytest.approx(10.0)
def test_weather_without_temperature_is_none():
    assert weather_celsius(State("weather.h", "sunny", {})) is None
```

- [ ] **Step 3:** Run `.venv/bin/pytest tests/test_units.py -v`. Expected: FAIL, import error.
- [ ] **Step 4:** Implement `units.py` with `homeassistant.util.unit_conversion.DistanceConverter` and `TemperatureConverter`.
- [ ] **Step 5:** Run `.venv/bin/pytest tests/test_units.py -v`. Expected: 7+ passed.
- [ ] **Step 6:** Commit: `git add pyproject.toml requirements_test.txt custom_components tests && git commit -m "Add test harness and unit conversion helpers"`

---

### Task 2: Energy accounting (pure)

**Files:**
- Create: `custom_components/ford_ev/accounting.py`, `tests/test_accounting.py`

**Interfaces:**
- Produces:
  - `@dataclass class SocAccountingState`: `last_soc: float | None = None`, `regen_credit: float = 0.0`, `energy_used: float = 0.0`, `energy_added: float = 0.0`, `energy_added_home: float = 0.0`.
  - `apply_soc_change(st: SocAccountingState, new_soc: float, capacity_kwh: float, plugged: bool, home: bool) -> SocAccountingState` returns a new object.
  - `charger_increment(last_reading: float | None, reading: float) -> float`.

**Rules (§6.4):**
- The first SoC (`last_soc is None`) only sets the baseline.
- Rise with `plugged` or `delta >= 5`: added += delta × cap / 100, and home-added too if `home`; credit = 0.
- Other rise: credit = min(credit + delta, 100).
- Drop: used += max(drop − credit, 0) × cap / 100; credit = max(credit − drop, 0).
- Always: `last_soc = new_soc`.
- Charger counter:
  - `last_reading is None`: the first observation is a baseline and counts 0. This differs from the YAML and avoids counting a stale value at install time.
  - `reading >= last`: reading − last.
  - Otherwise: a new run, returns reading.

- [ ] **Step 1: Write the failing tests:**

```python
CAP = 60.0
def test_first_soc_is_baseline():
    st = apply_soc_change(SocAccountingState(), 50, CAP, plugged=False, home=True)
    assert (st.last_soc, st.energy_used, st.energy_added) == (50, 0, 0)
def test_drop_counts_used():
    st = apply_soc_change(SocAccountingState(last_soc=50), 40, CAP, False, True)
    assert st.energy_used == pytest.approx(6.0)
def test_plugged_rise_counts_added_and_home():
    st = apply_soc_change(SocAccountingState(last_soc=40, regen_credit=2), 90, CAP, True, True)
    assert st.energy_added == pytest.approx(30.0) and st.energy_added_home == pytest.approx(30.0) and st.regen_credit == 0
def test_plugged_rise_away_not_home():
    st = apply_soc_change(SocAccountingState(last_soc=40), 50, CAP, True, False)
    assert st.energy_added == pytest.approx(6.0) and st.energy_added_home == 0
def test_large_unplugged_rise_counts_added():
    st = apply_soc_change(SocAccountingState(last_soc=40), 45, CAP, False, False)
    assert st.energy_added == pytest.approx(3.0)
def test_small_unplugged_rise_is_regen_credit_then_offsets_drop():
    st = apply_soc_change(SocAccountingState(last_soc=40), 42, CAP, False, False)
    assert st.regen_credit == 2 and st.energy_added == 0
    st = apply_soc_change(st, 37, CAP, False, False)
    assert st.energy_used == pytest.approx(1.8) and st.regen_credit == 0
def test_regen_credit_capped():
    st = apply_soc_change(SocAccountingState(last_soc=10, regen_credit=99), 13, CAP, False, False)
    assert st.regen_credit == 100
@pytest.mark.parametrize("last,reading,expected", [(None, 3.0, 0.0), (1.0, 2.5, 1.5), (3.0, 3.0, 0.0), (3.0, 0.5, 0.5)])
def test_charger_increment(last, reading, expected):
    assert charger_increment(last, reading) == pytest.approx(expected)
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_accounting.py -v`. Expected: FAIL, import error.
- [ ] **Step 3:** Implement `accounting.py` (dataclasses with `dataclasses.replace`).
- [ ] **Step 4:** Run the same command. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add SoC energy accounting and charger counter"`

---

### Task 3: Day/week/month periods (pure)

**Files:**
- Create: `custom_components/ford_ev/periods.py`, `tests/test_periods.py`

**Interfaces:**
- Produces:
  - `COUNTERS = ("odometer", "energy_used", "energy_added", "charger_energy", "charger_energy_home", "cost")`.
  - `PERIODS = ("day", "week", "month")`.
  - `@dataclass class PeriodSnapshot`: `start: str` (ISO date), `values: dict[str, float]`.
  - `@dataclass class PeriodState`: `snapshots: dict[str, PeriodSnapshot]`, `previous: dict[str, dict[str, float]]`.
  - `period_start(period: str, now_local: datetime) -> date`: week starts Monday.
  - `roll_periods(st: PeriodState, now_local: datetime, totals: dict[str, float]) -> PeriodState`.
  - `period_value(st: PeriodState, period: str, counter: str, totals: dict[str, float]) -> float | None`.

**Rules:**
- A missing snapshot is created from the current totals.
- A counter missing from a snapshot but now present in totals (for example, the odometer was unknown at creation) is added with its current value.
- When `period_start(now)` differs from `snapshot.start`:
  - `previous[period] = {c: totals[c] − snapshot.values[c]}` for the counters present in both;
  - then a new snapshot is taken.
- `period_value` is `totals[c] − snapshot.values[c]`, or None if the counter is absent from either.
- Rollover after downtime attributes the gap to the previous period, which is accepted.

- [ ] **Step 1: Write the failing tests:**

```python
T0 = {"odometer": 1000.0, "energy_used": 10.0}
def test_first_call_creates_zero_periods():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), T0)
    assert period_value(st, "week", "odometer", T0) == 0
def test_value_since_snapshot():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), T0)
    assert period_value(st, "day", "odometer", {**T0, "odometer": 1042.0}) == 42.0
def test_day_rollover_records_previous():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), T0)
    t1 = {"odometer": 1030.0, "energy_used": 14.0}
    st = roll_periods(st, datetime(2026, 11, 19, 0, 0, 5), t1)
    assert st.previous["day"] == {"odometer": 30.0, "energy_used": 4.0}
    assert period_value(st, "day", "odometer", t1) == 0
def test_week_starts_monday():
    assert period_start("week", datetime(2026, 11, 22, 12)) == date(2026, 11, 16)
def test_month_rollover_after_downtime():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 10, 30, 9), T0)
    st = roll_periods(st, datetime(2026, 11, 3, 9), {**T0, "odometer": 1100.0})
    assert st.previous["month"]["odometer"] == 100.0 and st.snapshots["month"].start == "2026-11-01"
def test_counter_added_late():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), {"energy_used": 1.0})
    st = roll_periods(st, datetime(2026, 11, 18, 10), {"energy_used": 1.0, "odometer": 500.0})
    assert period_value(st, "day", "odometer", {"energy_used": 1.0, "odometer": 510.0}) == 10.0
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_periods.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `periods.py`.
- [ ] **Step 4:** Run it again. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add period snapshots for daily, weekly and monthly reports"`

---

### Task 4: Range model (pure)

**Files:**
- Create: `custom_components/ford_ev/range_model.py`, `tests/test_range_model.py`

**Interfaces:**
- Produces:
  - **Constants:** `BASE_KWH_PER_KM = 0.16`, `REF_TEMP_C = 20`, `COLD_COEF = 0.01`, `HEAT_KW_PER_C = 0.12`, `COOL_KW_PER_C = 0.08`, `MIN_SPEED = 5`, `LAMBDA = 0.85`, `PRIOR_WEIGHT = 100`, `A_BOUNDS = (0.5, 2.0)`, `B_BOUNDS = (0.2, 3.0)`, `SOC_FULL = 99.5`, `FALLBACK_TEMP_C = 10.0`, `BY_TEMPERATURE = (-15, -10, -5, 0, 5, 10, 15, 20, 25, 30, 35)`.
  - `drive_kwh(km: float, temp_c: float) -> float`.
  - `climate_kw(temp_c: float, setpoint_c: float, on: bool) -> float`.
  - `predicted_kwh_per_km(temp_c: float, a: float, b: float, avg_speed: float, setpoint_c: float, climate_on: bool) -> float`.
  - `@dataclass class RangeModelState` with defaults:

    | group | fields |
    |---|---|
    | factors and priors | `a=1.0`, `b=1.0`, `a0=1.0`, `b0=1.0` |
    | fit sums | `s11=s12=s22=s1y=s2y=0.0` |
    | calibration history | `calibrations: int = 0`, `last_error_pct: float \| None = None`, `last_calibration: float \| None = None` (UTC timestamp), `last_result: dict \| None = None` |
    | current cycle | `cycle_x_drive=cycle_x_hvac=cycle_charged=cycle_km=0.0` |
    | model battery | `model_energy: float \| None = None` |
    | trips | `avg_speed=45.0`, `odo_accounted: float \| None = None`, `trip_start_ts: float \| None = None`, `trip_start_temp: float \| None = None` |

  - `@dataclass(frozen=True) class TripInput`: `odometer_km: float`, `start_ts: float | None`, `end_ts: float`, `start_temp_c: float | None`, `end_temp_c: float`, `setpoint_c: float`, `climate_on: bool`.
  - `account_trip(st: RangeModelState, trip: TripInput, capacity_kwh: float) -> RangeModelState`: clears `trip_start_ts` and `trip_start_temp`.
  - `add_charge(st: RangeModelState, kwh: float, capacity_kwh: float) -> RangeModelState`.
  - `calibration_allowed(st: RangeModelState, now_ts: float) -> bool`: `cycle_charged >= 1` and (never calibrated, or at least 6 h since the last calibration).
  - `@dataclass(frozen=True) class CalibrationResult`: `valid: bool`, `ratio`, `km`, `charged_kwh`, `predicted_kwh`, `error_pct`, `a`, `b`, `capacity_estimate_kwh: float | None`, `soc_start_car: float | None`, `soc_start_model: float | None`, `soc_start_charger: float | None`.
  - `calibrate(st, capacity_kwh, now_ts, session_kwh: float | None, session_start_soc_car: float | None, session_start_soc_model: float | None) -> tuple[RangeModelState, CalibrationResult]`.
  - `seed_drive_factor(soc: float | None, range_km: float | None, capacity_kwh: float, temp_c: float) -> float`.
  - `model_soc(st, capacity_kwh) -> float | None`: clamped 0–100.

**Rules (§6.6):**
- **Trips:**
  - `odo_accounted is None` → set it to the odometer; no energy is charged to the model.
  - km = odometer − odo_accounted. km < 0 → resync `odo_accounted`, no energy. km < 0.5 → return unchanged (the distance accumulates into the next trip).
  - **Duration:** hours = (end − start) / 3600. Valid if start is present and 0 < hours < 12 and km / max(hours, 0.01) < 160; otherwise hours = km / avg_speed.
  - **Temperature:** the mean of start and end if valid and the start temperature is present, else the end temperature.
  - **Energy:** x1 = drive_kwh(km, T); x2 = hours × climate_kw(T, sp, on); model_energy = max(model_energy − (a·x1 + b·x2), −capacity), unchanged if None. The cycle sums grow.
  - **Average speed:** updated only when valid and hours ≥ 0.1 and km ≥ 2: clamp(0.8·avg + 0.2·km/hours, 10, 130).
- **Charging:** `add_charge` caps model_energy at capacity (None stays None) and adds kwh to `cycle_charged`.
- **Calibration:**
  - y = cycle_charged, pred = a·x1 + b·x2, ratio = y / pred (0 if pred ≤ 0), valid = x1 + x2 ≥ 3 and 0.5 ≤ ratio ≤ 2. Fit:

    ```
    if valid:
        S ← λ·S + (x1², x1·x2, x2², x1·y, x2·y)
        det = (S11 + w)(S22 + w) − S12²
        a = clamp(((S1y + w·a0)(S22 + w) − S12(S2y + w·b0)) / det, 0.5, 2)
        b = clamp(((S11 + w)(S2y + w·b0) − S12(S1y + w·a0)) / det, 0.2, 3)
        calibrations += 1; last_error_pct = clamp((pred − y) / y · 100, −100, 100)
    ```

  - **Always:** model_energy = capacity; cycle sums = 0; last_calibration = now_ts; last_result = asdict(result).
  - **Capacity estimate:** session_kwh / (100 − start_soc_car) × 100, only if start_soc_car < 80 and session_kwh > 0.
  - soc_start_charger = 100 − session_kwh / capacity × 100.
- **Seeding:** implied = soc/100 × cap / range_km; a0 = clamp(implied / drive_kwh(1, T), 0.5, 2.0). Returns 1.0 if soc < 20 or range_km is missing or ≤ 0.

- [ ] **Step 1: Write the failing tests:**

```python
def test_drive_kwh():
    assert drive_kwh(100, 20) == pytest.approx(16.0); assert drive_kwh(100, 0) == pytest.approx(19.2); assert drive_kwh(100, 25) == pytest.approx(16.0)
def test_climate_kw():
    assert climate_kw(0, 21, True) == pytest.approx(2.52); assert climate_kw(30, 21, True) == pytest.approx(0.72); assert climate_kw(0, 21, False) == 0
def test_predicted_kwh_per_km():
    assert predicted_kwh_per_km(0, 1, 1, 40, 21, True) == pytest.approx(0.255)
def test_trip_valid_duration():
    st = RangeModelState(model_energy=50, odo_accounted=1000)
    st = account_trip(st, TripInput(1030, 0, 1800, 0.0, 10.0, 21, True), 60)
    assert st.cycle_x_drive == pytest.approx(5.52) and st.cycle_x_hvac == pytest.approx(0.96)
    assert st.model_energy == pytest.approx(43.52) and st.avg_speed == pytest.approx(48.0) and st.cycle_km == 30 and st.odo_accounted == 1030
def test_trip_missing_start_uses_average_speed():
    st = account_trip(RangeModelState(model_energy=50, odo_accounted=1000), TripInput(1030, None, 1800, None, 10.0, 21, True), 60)
    assert st.cycle_x_drive == pytest.approx(5.28) and st.cycle_x_hvac == pytest.approx(0.88, abs=1e-3) and st.avg_speed == 45.0
def test_trip_odometer_backwards_resyncs():
    st = account_trip(RangeModelState(model_energy=50, odo_accounted=1000), TripInput(990, 0, 600, 5, 5, 21, True), 60)
    assert st.odo_accounted == 990 and st.model_energy == 50
def test_short_trip_accumulates():
    st = account_trip(RangeModelState(model_energy=50, odo_accounted=1000), TripInput(1000.3, 0, 60, 5, 5, 21, True), 60)
    assert st.odo_accounted == 1000 and st.cycle_km == 0
def test_first_trip_sets_baseline():
    st = account_trip(RangeModelState(model_energy=50), TripInput(1000, 0, 600, 5, 5, 21, True), 60)
    assert st.odo_accounted == 1000 and st.model_energy == 50
def test_add_charge_caps_at_capacity():
    st = add_charge(RangeModelState(model_energy=58), 5, 60)
    assert st.model_energy == 60 and st.cycle_charged == 5
def test_calibrate_valid_fit():
    st = RangeModelState(cycle_x_drive=40, cycle_x_hvac=10, cycle_charged=45, cycle_km=300, model_energy=10)
    st2, r = calibrate(st, 60, 1_000_000, 40, 30, 35)
    assert r.valid and r.a == pytest.approx(0.8889, abs=1e-4) and r.b == pytest.approx(0.9722, abs=1e-4)
    assert r.error_pct == pytest.approx(11.11, abs=0.01) and r.capacity_estimate_kwh == pytest.approx(57.14, abs=0.01)
    assert r.soc_start_charger == pytest.approx(33.33, abs=0.01)
    assert st2.model_energy == 60 and st2.cycle_charged == 0 and st2.calibrations == 1 and st2.last_calibration == 1_000_000
def test_calibrate_invalid_keeps_factors_but_resets():
    st = RangeModelState(cycle_x_drive=1.5, cycle_x_hvac=0.5, cycle_charged=2, model_energy=10)
    st2, r = calibrate(st, 60, 1, None, None, None)
    assert not r.valid and st2.a == 1.0 and st2.s11 == 0 and st2.model_energy == 60 and st2.calibrations == 0
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
@pytest.mark.parametrize("soc,rng,expected", [(80, 300, 1.0), (80, 240, 1.25), (10, 300, 1.0), (80, None, 1.0), (80, 60, 2.0)])
def test_seed_drive_factor(soc, rng, expected):
    assert seed_drive_factor(soc, rng, 60, 20) == pytest.approx(expected)
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_range_model.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `range_model.py`.
- [ ] **Step 4:** Run it again. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add range model: trip energy, charging, calibration fit"`

---

### Task 5: FCQ discovery and test fixture

**Files:**
- Create: `custom_components/ford_ev/manifest.json`, `custom_components/ford_ev/const.py`, `custom_components/ford_ev/discovery.py`, `tests/test_discovery.py`
- Modify: `tests/conftest.py`, adding the `FakeFcq` fixture

**Interfaces:**
- Produces, in `const.py`:
  - `DOMAIN = "ford_ev"`, `VERSION = "0.1.0"`, `FCQ_DOMAIN = "fordconnect_query"`.
  - Config keys `CONF_DEVICE_ID="device_id"`, `CONF_VIN="vin"`, `CONF_BATTERY_KWH="battery_kwh"`, `CONF_TEMPERATURE_SENSOR="temperature_sensor"`, `CONF_WEATHER_ENTITY="weather_entity"`.
  - Option keys with defaults:

    | key | default |
    |---|---|
    | `CONF_NOTIFY_SERVICES="notify_services"` | `[]` |
    | `CONF_NOTIFY_ENTITIES="notify_entities"` | `[]` |
    | `CONF_DASHBOARD_PATH="dashboard_path"` | `""` |
    | `CONF_PRICE_ENTITY="price_entity"` | `None` |
    | `CONF_PRICE_FIXED="price_fixed"` | `None` |
    | `CONF_ALERT_DELAY_MIN="alert_delay_min"` | `10` |
    | `CONF_REMINDER_TIME="reminder_time"` | `"21:00:00"` |
    | `CONF_NIGHT_START="night_start"` | `"00:00:00"` |
    | `CONF_NIGHT_END="night_end"` | `"06:00:00"` |
    | `CONF_WEEKLY_DAY="weekly_day"` | `"sun"` |
    | `CONF_WEEKLY_TIME="weekly_time"` | `"20:00:00"` |
    | `CONF_STALE_UNAVAILABLE_H="stale_unavailable_h"` | `2` |
    | `CONF_STALE_NO_UPDATE_H="stale_no_update_h"` | `48` |

  - `DEFAULT_OPTIONS: dict`.
  - `merged_config(entry: ConfigEntry) -> dict[str, Any]` = `{**DEFAULT_OPTIONS, **entry.data, **entry.options}`.
- Produces, in `discovery.py`:
  - `REQUIRED_KEYS = ("soc", "odometer", "ignitionstatus", "evccstatus", "tracker")`.
  - `OPTIONAL_KEYS = ("elveh", "elvehplug", "elvehcharging", "doorlock", "doorstatus", "windowposition", "indicators", "lastrefresh")`.
  - `@dataclass(frozen=True) class FcqVehicle`: `device_id: str`, `vin: str`, `name: str`, `identifiers: frozenset[tuple[str, str]]`, `entities: Mapping[str, str]` (key → entity_id).
  - `async_discover_vehicles(hass) -> list[FcqVehicle]`: complete vehicles only.
  - `async_get_vehicle(hass, device_id: str) -> FcqVehicle | None`.
- Produces, in `tests/conftest.py`:
  - `VIN = "WF0TESTVIN0000001"`.
  - fixture `fcq(hass) -> FakeFcq` with `.vin`, `.device_id`, `.entity_id(key) -> str`, `.set(key, state, attrs=None)`.

**Rules:**
- Scan `er.async_get(hass).entities` for `platform == FCQ_DOMAIN` with `disabled_by is None`, grouped by `device_id`.
- The key is `translation_key`. VIN = `unique_id.removeprefix("fcq_uid_").removesuffix(f"_{key}")`.
- Name = `device.name_by_user or device.name`.
- Identifiers come from the FCQ device entry.

**FakeFcq:**
- Creates `MockConfigEntry(domain=FCQ_DOMAIN)` and a device with identifiers `{(FCQ_DOMAIN, VIN)}`, named "Test Explorer".
- Registers one entity per key: domain `device_tracker` for `tracker`, `sensor` for the rest; `unique_id f"fcq_uid_{VIN}_{key}"`, `suggested_object_id f"fcq_{VIN.lower()}_{key}"`, `translation_key=key`.
- Default states: `soc` "50" %; `elveh` "200" km; `odometer` "1000" km; `ignitionstatus` "OFF"; `evccstatus` "A"; `elvehplug` "DISCONNECTED"; `elvehcharging` "NOT_PLUGGED_IN" with no `chargerEnergyOutput`; `doorlock` "LOCKED"; `doorstatus` "Closed"; `windowposition` "Closed"; `indicators` "0"; `lastrefresh` now ISO; `tracker` "home" with `latitude`/`longitude` 0.0.

**Manifest:** `domain`, `name`, `version`, `config_flow`, `dependencies`, `after_dependencies`, `iot_class`, `integration_type` as in Global Constraints. `codeowners ["@mihaics"]`; `documentation` and `issue_tracker` point to `https://github.com/mihaics/fordev4ha`.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_discovers_vehicle(hass, fcq):
    [v] = await async_discover_vehicles(hass)
    assert v.vin == VIN and v.name == "Test Explorer" and v.entities["soc"] == fcq.entity_id("soc")
async def test_renamed_entity_still_found(hass, fcq):
    er.async_get(hass).async_update_entity(fcq.entity_id("soc"), new_entity_id="sensor.my_car_battery")
    [v] = await async_discover_vehicles(hass)
    assert v.entities["soc"] == "sensor.my_car_battery"
async def test_incomplete_vehicle_skipped(hass, fcq):
    er.async_get(hass).async_update_entity(fcq.entity_id("odometer"), disabled_by=er.RegistryEntryDisabler.USER)
    assert await async_discover_vehicles(hass) == []
async def test_optional_missing_ok(hass, fcq):
    er.async_get(hass).async_remove(fcq.entity_id("indicators"))
    [v] = await async_discover_vehicles(hass)
    assert "indicators" not in v.entities
async def test_no_fcq(hass):
    assert await async_discover_vehicles(hass) == []
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_discovery.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `const.py`, `manifest.json`, `discovery.py`, and `FakeFcq` in conftest.
- [ ] **Step 4:** Run it again. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add FordConnect Query vehicle discovery"`

---

### Task 6: Config flow and options flow

**Files:**
- Create: `custom_components/ford_ev/config_flow.py`, `strings.json`, `translations/en.json` (an identical copy), `tests/test_config_flow.py`
- Modify: `custom_components/ford_ev/__init__.py`: stub `async_setup_entry`/`async_unload_entry` returning True, replaced in Task 7

**Interfaces:**
- Consumes: `async_discover_vehicles`, the const keys.
- Produces:
  - `FordEvConfigFlow` (VERSION 1), with steps `user` (vehicle) and `settings`.
  - `FordEvOptionsFlow(OptionsFlowWithReload)`, with step `init`.
  - The entry's `unique_id` is the VIN. `data` = `{device_id, vin, battery_kwh, temperature_sensor, weather_entity}`. Title = vehicle name.

**Steps:**
- **`user`:**
  - Aborts with `no_vehicles` when discovery is empty. The abort text links the FCQ repository.
  - Lists vehicles not yet configured (by VIN) in a `SelectSelector` of device IDs labelled with names; aborts `already_configured` if all are configured.
- **`settings`:**
  - `battery_kwh`: `NumberSelector`, 10–200, step 0.1, required, no default.
  - `temperature_sensor`: `EntitySelector`, domain sensor, device_class temperature, optional.
  - `weather_entity`: `EntitySelector`, domain weather, optional. The default is `weather.forecast_home` if that entity exists.
  - Error `temperature_source_required` if both temperature fields are empty.
- **Options `init`:**
  - `notify_services`: `SelectSelector`, multiple, built from `hass.services.async_services_for_domain("notify")` minus `send_message`.
  - `notify_entities`: `EntitySelector`, domain notify, multiple.
  - `dashboard_path`: text. `price_entity`: domain sensor or input_number. `price_fixed`: number ≥ 0, step 0.0001.
  - `alert_delay_min`: 1–120. `reminder_time`, `night_start`, `night_end`, `weekly_time`: `TimeSelector`. `weekly_day`: select mon…sun.
  - `stale_unavailable_h`: 1–48. `stale_no_update_h`: 6–336.
  - `battery_kwh`, `temperature_sensor`, `weather_entity` (same validation as `settings`).

- [ ] **Step 1: Write the failing tests:**

```python
async def test_full_flow_creates_entry(hass, fcq):
    hass.states.async_set("sensor.outside", "5", {"unit_of_measurement": "°C", "device_class": "temperature"})
    with patch("custom_components.ford_ev.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"device_id": fcq.device_id})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"battery_kwh": 60, "temperature_sensor": "sensor.outside"})
    assert r["type"] == "create_entry" and r["title"] == "Test Explorer"
    assert r["result"].unique_id == VIN and r["data"]["battery_kwh"] == 60
async def test_abort_without_fcq(hass):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == "abort" and r["reason"] == "no_vehicles"
async def test_temperature_source_required(hass, fcq): ...   # configure settings with neither → errors == {"base": "temperature_source_required"}
async def test_configured_vehicle_not_offered(hass, fcq):     # MockConfigEntry(unique_id=VIN) added → abort "already_configured"
async def test_options_flow_saves(hass, fcq): ...             # init options, set price_fixed 0.25 and alert_delay_min 15 → entry.options has both
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_config_flow.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement the flows, `strings.json` (config, options, errors, aborts, all entity names from Task 9, service from Task 11) and `translations/en.json`.
- [ ] **Step 4:** Run it again. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add config and options flows"`

---

### Task 7: Engine core (storage, accounting, sessions, cost, periods, recovery)

**Files:**
- Create: `custom_components/ford_ev/state.py`, `custom_components/ford_ev/engine.py`, `tests/test_engine.py`
- Modify: `custom_components/ford_ev/__init__.py`: real `async_setup_entry`/`async_unload_entry`, platforms list empty until Task 9; `tests/conftest.py`: add `async_setup_ford_ev(hass, fcq, options=None) -> MockConfigEntry` (data: `battery_kwh 60`, `temperature_sensor "sensor.outside"`, `weather_entity None`; it first sets `sensor.outside` to "20" °C)

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces, in `state.py`:
  - `STORAGE_VERSION = 1`.
  - `@dataclass class Settings`: `climate_on=True`, `climate_setpoint=21.0`, `charge_target=100.0`, `plug_in_reminder_soc=40.0`, `notify: dict[str, bool]` (keys from `NOTIFY_KEYS` in const, all True except `calibration` False).
  - `@dataclass class ChargingSession`: `start_ts`, `start_soc: float | None`, `charger_at_start`, `added_at_start`, `model_soc_at_start: float | None`, `home: bool`.
  - `@dataclass class EngineState` with fields:

    | group | fields |
    |---|---|
    | accounting | `soc: SocAccountingState`, `charger_last_reading: float \| None`, `charger_energy`, `charger_energy_home`, `cost` |
    | charging | `session: ChargingSession \| None`, `pending_session_end: dict \| None` (`{"ts", "to"}`), `last_session: dict \| None` |
    | reports, model, settings | `periods: PeriodState`, `model: RangeModelState`, `settings: Settings` |
    | trips | `pending_trip_end_ts: float \| None` |
    | alerts | `alerts_armed: dict[str, bool]` |

  - `EngineState.to_dict()`, `EngineState.from_dict(d)`, `EngineState.new()`.
  - `NOTIFY_KEYS = ("charging", "unlocked", "open", "plug_in_reminder", "warning_light", "data_stale", "night_activity", "weekly_summary", "calibration")` in const.
- Produces, in `engine.py`:
  - `signal_update(entry_id) -> str` = `f"{DOMAIN}_{entry_id}_update"`.
  - `signal_event(entry_id) -> str` = `f"{DOMAIN}_{entry_id}_event"`, dispatched with `(entity_key: str, event_type: str, data: dict)`.
  - `class FordEvEngine(hass, entry, vehicle: FcqVehicle)` with:
    - `state: EngineState`, `vehicle`, `capacity_kwh: float` (property);
    - `async_start()`, `async_stop()`;
    - `totals() -> dict[str, float]`, `price() -> float | None`, `is_home() -> bool`, `last_valid(key) -> str | None`;
    - `set_setting(name: str, value)`: also accepts `notify.<key>`;
    - `emit(entity_key: str, event_type: str, data: dict)`;
    - `async_add_listener(cb) -> Callable[[], None]`: a thin wrapper over the update signal;
    - `save()`: delayed save, 10 s.
  - `entry.runtime_data = FordEvRuntime(engine=..., alerts=None)` (`alerts` is set in Task 10). `type FordEvConfigEntry = ConfigEntry[FordEvRuntime]`.

**Rules:**
- **Store:** `Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")`. Load in `async_start`. `EngineState.new()` if empty; first-start seeding is in Task 8.
- **Baselines at start:**
  - If `soc.last_soc` is None and the current FCQ SoC is numeric, it becomes the baseline. Nothing is counted.
  - If `charger_last_reading` is None and `chargerEnergyOutput` is numeric, it becomes the baseline too.
  - `_last_valid` is filled from the current valid states.
- **Subscriptions:** state-change events on all `vehicle.entities`.
- **Last valid value:** `_last_valid[key]` is updated only for states not in (unknown, unavailable). Transitions use `_last_valid` before the update.
- **SoC change:**
  1. `roll_periods(now_local, totals)`;
  2. `apply_soc_change(plugged = last_valid evccstatus in (B, C) or elvehplug == CONNECTED, home = tracker == home)`;
  3. signal, save.
- **`chargerEnergyOutput` attribute change** (numeric):
  1. d = `charger_increment`;
  2. roll periods;
  3. `charger_energy += d`; if home, also `charger_energy_home += d` and `cost += d × price` (price None counts as 0);
  4. `model = add_charge(model, d, cap)`;
  5. set the reading, signal, save.
- **Price:** the `price_entity` state if numeric, else `price_fixed`, else None.
- **Session start:** `evccstatus` → `C` with `session is None`, or with `pending_session_end` set (then the pending end is cancelled, and the session continues; no new session). Record `start_soc` (current SoC or `soc.last_soc`), the counters, `model_soc_at_start`, and home. Emit `("charging", "session_started", {start_soc, location})`.
- **Session end:**
  - Trigger: last valid `C` → `A`/`B`, or anything → `A`, while a session is open. Set `pending_session_end = {"ts": now, "to": new}` and `async_call_later(120)`.
  - Finalise:
    1. charger_delta = charger_energy − charger_at_start; added_delta = soc.energy_added − added_at_start.
    2. If charger_delta ≤ 0 and added_delta > 0, fallback: add added_delta to `charger_energy` (and home and cost if home now) and `add_charge`. kwh = max(charger_delta, fallback).
    3. Event type: "B" → `charging_complete` if end_soc ≥ charge_target − 1, else `charging_stopped_early`; "A" → `unplugged_while_charging`.
    4. data = `{start, end (ISO), duration_min, start_soc, end_soc, kwh, cost, location, target, range_km, summary, model_soc_at_start}`.
    5. Summary: `"{start:%d.%m %H:%M}-{end:%H:%M} ({h}h{mm:02}), {start_soc}% -> {end_soc}%, +{kwh:.1f} kWh, {location}"`, plus `", {cost:.2f} {currency}"` when a price is set.
    6. `last_session = data`; `session = None`; emit.
- **Midnight:** `async_track_time_change(hour=0, minute=0, second=5)` rolls the periods, signals and saves.
- **Recovery** in `async_start`, after load:
  - `pending_session_end` set: if `evccstatus` is `C` now, clear it; else finalise now with the recorded `to`.
  - Session open, nothing pending, current valid `evccstatus` not `C`: finalise now with `to` = current.

- [ ] **Step 1: Write the failing tests** (each sets FCQ states via `fcq.set` after `async_setup_ford_ev`; time control via `freezer` and `async_fire_time_changed`):

```python
async def test_soc_drop_counts_used(hass, fcq):                 # 50 → 40 → state.soc.energy_used == 6.0
async def test_charger_energy_and_cost_at_home(hass, fcq):      # options price_fixed 0.30; readings 0.5 (baseline), 2.0, 4.5 → charger_energy == 4.0, home 4.0, cost == 1.20
async def test_charger_away_has_no_cost(hass, fcq):             # tracker not_home → charger_energy 1.5, home 0, cost 0
async def test_session_complete_event(hass, fcq):               # evcc C, soc 50→100, charger 0.5→30.5, evcc B, +121 s → event ("charging","charging_complete") with kwh 30.0, end_soc 100
async def test_session_stopped_early(hass, fcq):                # charge_target 100, end soc 80 → charging_stopped_early
async def test_session_end_cancelled_by_resume(hass, fcq):      # C → B, +60 s → C, +121 s → no end event, session still open
async def test_session_fallback_to_soc_energy(hass, fcq):       # no chargerEnergyOutput; C, soc 50→60 (plugged), A, +121 s → charger_energy == 6.0, event kwh 6.0
async def test_session_end_after_unavailable(hass, fcq):        # C → unavailable → B → finalised
async def test_restart_with_pending_session_end(hass, fcq, hass_storage):  # stored pending_session_end + evcc B → finalised at start, one event
async def test_restart_with_open_session_while_unplugged(hass, fcq, hass_storage)  # stored session, evcc A → finalised at start
async def test_state_persists_across_reload(hass, fcq):         # energy_used after reload equals before
async def test_two_entries_separate_storage(hass, fcq, fcq2):   # second fake vehicle (fixture fcq2, VIN WF0TESTVIN0000002) → independent totals
async def test_midnight_rolls_day(hass, fcq, freezer):          # distance day resets, previous recorded
async def test_unsupported_state_ignored(hass, fcq):            # ignitionstatus "Unsupported" → no error, last_valid unchanged
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_engine.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `state.py`, `engine.py` and the entry setup/unload. Unload stops the engine (unsubscribe, cancel timers, final save).
- [ ] **Step 4:** Run it again. Expected: all pass. Also run the full `.venv/bin/pytest`: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add engine: storage, energy accounting, charging sessions, cost, periods"`

---

### Task 8: Engine range model wiring (temperature, forecast, trips, calibration, seeding)

**Files:**
- Modify: `custom_components/ford_ev/engine.py`
- Create: `tests/test_engine_model.py`

**Interfaces:**
- Consumes: Task 4 and Task 7.
- Produces, on `FordEvEngine`:
  - `current_temp_c() -> float | None`;
  - `forecast_tomorrow_c: float | None`;
  - `async_refresh_forecast() -> None`;
  - `reset_range_model() -> None`, used by Task 11.

**Rules:**
- **Temperature:** `state_celsius(temperature_sensor)`, else `weather_celsius(weather_entity)`, else None. The engine also subscribes to those two entities and signals on change.
- **Forecast:**
  - Runs at start and at minute 7 of every hour (`async_track_time_change(minute=7, second=0)`).
  - Calls `weather.get_forecasts` with `{"type": "daily"}`, `blocking=True`, `return_response=True`.
  - Takes the entry whose date (local) is tomorrow: (temperature + templow) / 2 if `templow` is present, else temperature, converted from the weather entity's `temperature_unit`.
  - Any exception, or no weather entity, gives None.
- **Ignition:**
  - Last valid `OFF` → `ON`: `trip_start_ts` = the new state's `last_changed` timestamp; `trip_start_temp = current_temp_c()`.
  - Last valid `ON` → `OFF`: `pending_trip_end_ts` = `last_changed` timestamp; `async_call_later(120)`.
  - Then `account_trip(TripInput(odometer_km=state_km(odometer), start_ts=trip_start_ts, end_ts=pending, start_temp_c=trip_start_temp, end_temp_c=current_temp_c() or FALLBACK_TEMP_C, setpoint, climate_on))`. Skip if the odometer is None. Clear pending, signal, save.
- **Calibration:**
  - SoC ≥ `SOC_FULL` and no timer → `async_call_later(300)`. SoC < `SOC_FULL` → cancel.
  - At expiry: if current SoC ≥ `SOC_FULL` and `calibration_allowed`, then calibrate.
  - Session values come from the open session (kwh = charger_energy − charger_at_start), else from `last_session` if its `end` is within 1 h, else None.
  - Emit `("report", "calibration", asdict(result) | {"summary": ...})`.
  - Summary: `"{km:.0f} km, charged {y:.1f} kWh, model {pred:.1f} kWh ({err:+.1f}%) | drive x{a:.2f} climate x{b:.2f}"`. When invalid, the factor part is `"NOT USED, ratio {ratio:.2f}"`.
- **First start** (no stored data):
  - `model.a0 = model.a = seed_drive_factor(soc, state_km(elveh), cap, current_temp_c() or 20)`;
  - `model_energy` = soc/100 × cap if SoC is known (otherwise set at the first numeric SoC);
  - `odo_accounted` = the current odometer.
- **Recovery:**
  - `pending_trip_end_ts` set → account now.
  - Else `trip_start_ts` set and ignition currently `OFF` → account with end = ignition `last_changed`.
- **`reset_range_model()`:** a fresh `RangeModelState`, seeded as on first start; `avg_speed` is kept.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_first_start_seeds_model(hass, fcq):            # soc 80, elveh 240, cap 60, outside 20 °C → model.a == 1.25, model_energy == 48, odo_accounted == 1000
async def test_trip_accounted_two_minutes_after_off(hass, fcq, freezer):  # ON, +30 min, odometer 1030, OFF; +119 s nothing; +2 s → cycle_km == 30
async def test_trip_with_miles_odometer(hass, fcq, freezer):   # odometer in mi: 621.371 → 640.0 (≈ 30 km) → cycle_km ≈ 30.0 (abs 0.1)
async def test_trip_after_unavailable_ignition(hass, fcq):     # ON → unavailable → OFF → accounted
async def test_restart_with_pending_trip(hass, fcq, hass_storage)  # stored pending_trip_end_ts → accounted at start, once
async def test_temperature_fallback_to_weather(hass, fcq):     # no sensor, weather temperature 50 °F → current_temp_c() == 10
async def test_forecast_tomorrow(hass, fcq, freezer):          # mocked weather.get_forecasts response → forecast_tomorrow_c == mean of tomorrow's high/low
async def test_calibration_after_five_minutes_at_full(hass, fcq, freezer):  # cycle_charged ≥ 1; soc 100 for 301 s → calibration event, model_energy == cap
async def test_no_calibration_if_soc_drops(hass, fcq, freezer): # soc 100 then 99 within 300 s → no event
async def test_reset_range_model(hass, fcq):                   # factors back to seed, calibrations 0
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_engine_model.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement it in `engine.py`.
- [ ] **Step 4:** Run it again, then the full `.venv/bin/pytest`. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Wire range model into engine: trips, forecast, calibration"`

---

### Task 9: Entities (sensor, binary_sensor, number, switch)

**Files:**
- Create: `custom_components/ford_ev/entity.py`, `sensor.py`, `binary_sensor.py`, `number.py`, `switch.py`, `icons.json`, `tests/test_entities.py`
- Modify: `__init__.py` (`PLATFORMS`), `strings.json` and `translations/en.json` (entity names)

**Interfaces:**
- Consumes: the `FordEvEngine` API from Tasks 7–8, and `predicted_kwh_per_km`, `model_soc`, `BY_TEMPERATURE` from Task 4.
- Produces:
  - `FordEvEntity(engine, key)`: `has_entity_name=True`, `translation_key=key`, `unique_id=f"{vin}_{key}"`.
  - Device info: `identifiers {(DOMAIN, vin)}`, `name f"Ford EV {vehicle.name}"`, `model "Ford EV"`, `via_device = next(iter(vehicle.identifiers))`.
  - Subscribes to `signal_update` and writes state.
  - Entity translation keys exactly as in the table; the strategy (Task 12) relies on them.

**Sensors** (description table in `sensor.py`, each with a `value_fn(engine)` and an optional `attrs_fn(engine)`):

| key | unit / class / state_class | value |
|---|---|---|
| `energy_used`, `energy_added`, `charger_energy`, `charger_energy_home` | kWh / energy / total_increasing | the counters |
| `charging_cost` | currency / monetary / total | `state.cost` |
| `distance_day`, `distance_week`, `distance_month` | km / distance / total, `last_reset` = period start | `period_value(..., "odometer")` |
| `energy_used_week`/`_month`, `charger_energy_week`/`_month`, `charger_energy_home_week`/`_month` | kWh / energy / total, `last_reset` | `period_value` |
| `cost_week`, `cost_month` | currency / monetary / total, `last_reset` | `period_value(..., "cost")` |
| `consumption_week`, `consumption_month` | kWh/100km / energy_distance / measurement | used ÷ km × 100, None if km < 10 |
| `car_full_range` | km / distance / measurement | range ÷ soc × 100 if soc > 5 and range > 0 |
| `car_km_per_percent` | `km/%` / measurement | range ÷ soc |
| `charging_state` | enum `unplugged`, `plugged`, `charging` | from `evccstatus` A/B/C |
| `data_age` | min / duration / measurement | now − `lastrefresh` |
| `last_charge` | text | `last_session["summary"][:255]`; attrs = `last_session` |
| `outside_temperature` | °C / temperature / measurement | `current_temp_c()` |
| `model_consumption` | kWh/100km / energy_distance / measurement | `predicted_kwh_per_km(...) × 100`; attrs `driving`, `climate`, `climate_kw`, `average_speed` |
| `model_soc` | % / battery / measurement | `model_soc()`; attr `difference_vs_car` |
| `model_range` | km / distance / measurement | max(model_energy, 0) ÷ (consumption / 100); attr `with_car_soc` |
| `model_range_full` | km / distance / measurement | cap ÷ (consumption / 100); attr `by_temperature` = `[{t, km, kwh100}]` over `BY_TEMPERATURE` |
| `model_range_full_tomorrow` | km / distance / — | from `forecast_tomorrow_c`; attr `temperature` |
| `drive_factor`, `climate_factor` | — / measurement | `model.a`, `model.b` |
| `cycle_error` | % / measurement | `model.last_error_pct` |
| `calibrations` | — / total_increasing | `model.calibrations` |
| `last_calibration` | timestamp | `model.last_calibration`; attrs = `model.last_result` |

Model sensors are None when the temperature is None or `model_energy` is None.

**Other platforms:**
- **Binary sensor:** `charging_session`, `battery_charging`, on while `state.session` is set.
- **Numbers** (mode slider, write via `set_setting`):
  - `charge_target`: 50–100, step 5, %.
  - `plug_in_reminder_soc`: 5–95, step 5, %.
  - `climate_setpoint`: 16–28, step 0.5, °C, device_class temperature.
- **Switches:** `climate_on`; `notify_<key>` for each of the `NOTIFY_KEYS`, with `entity_category=config`.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_core_sensors_present(hass, fcq):            # every key in the table has an entity; energy_used state_class total_increasing
async def test_counters_update(hass, fcq):                 # soc 50 → 40 → sensor energy_used == "6.0"
async def test_consumption_none_under_10_km(hass, fcq):    # 5 km driven → consumption_week state "unknown"
async def test_period_sensor_last_reset(hass, fcq):        # distance_week attributes last_reset == Monday 00:00 local ISO
async def test_model_range_full_by_temperature(hass, fcq): # attribute list length 11, first t == -15
async def test_number_writes_setting(hass, fcq):           # number.set_value climate_setpoint 22 → engine.state.settings.climate_setpoint == 22, persisted
async def test_switch_writes_notify_setting(hass, fcq):    # switch.turn_off notify_night_activity → settings.notify["night_activity"] is False
async def test_device_linked_to_fcq(hass, fcq):            # our device via_device_id == fcq.device_id
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_entities.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement the entity platforms and translations.
- [ ] **Step 4:** Run it again, then the full `.venv/bin/pytest`. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add sensor, binary sensor, number and switch entities"`

---

### Task 10: Alerts, notifications and event entities

**Files:**
- Create: `custom_components/ford_ev/alerts.py`, `custom_components/ford_ev/notify.py`, `custom_components/ford_ev/event.py`, `tests/test_alerts.py`
- Modify: `__init__.py` (start/stop the `AlertManager`, add `event` to `PLATFORMS`), `strings.json` and `translations/en.json`

**Interfaces:**
- Consumes: the engine API (`emit`, `last_valid`, `state.settings`, `state.alerts_armed`, `signal_event`, `totals`, `period_value`), plus `merged_config`.
- Produces:
  - `class DelayedCondition(hass, name: str, delay_s: float, predicate: Callable[[], bool], on_fire: Callable[[], None], armed: dict[str, bool])` with `evaluate()` and `cancel()`.
  - `class AlertManager(hass, engine, config)` with `async_start()` and `async_stop()`. It also listens to `signal_event` and calls the notifier.
  - `build_notification(event_type: str, data: dict, currency: str) -> tuple[str, str]`.
  - `EVENT_TYPES = {"alert": [...], "charging": [...], "report": [...]}`, as in §6.5.
  - `NOTIFY_FOR = {event_type: notify_key}`: `charging_complete`/`charging_stopped_early` → `charging`; the others map to the same-named key; `session_started` and `unplugged_while_charging` are absent.
  - In `notify.py`: `async_send(hass, services: list[str], entities: list[str], title: str, message: str, *, tag: str, channel: str, high_priority: bool, click_action: str | None) -> None`.
  - In `event.py`: three `EventEntity`s with keys `alert`, `charging`, `report` and `event_types` from `EVENT_TYPES`. On `signal_event` for their key: `_trigger_event(type, data)`, then write state.

**Rules:**
- **`DelayedCondition`:**
  - Predicate true, armed and no timer → start the timer.
  - Predicate false → cancel the timer, `armed[name] = True`.
  - At expiry, if the predicate is still true → `on_fire()` and `armed[name] = False`.
  - The armed state lives in `engine.state.alerts_armed`, so it survives restarts.
- **Alert conditions:**

  | alert | condition | event data |
  |---|---|---|
  | `unlocked` | ignition `OFF` and `doorlock` `UNLOCKED`, delay `alert_delay_min` | `location`, `doors` (attribute keys with value `UNLOCKED`), `delay_min` |
  | `open` | ignition `OFF` and (`doorstatus` or `windowposition` == `Open`) | `doors` (string attribute values not in `CLOSED`/`INVALID`/`UNKNOWN`, excluding `friendly_name` and `icon`), `windows` (mapping attributes with `doubleRange.upperBound > 0`), `delay_min` |
  | `data_stale`, unavailable | FCQ `soc` state `unavailable` for `stale_unavailable_h` (a `DelayedCondition`) | `reason "unavailable"`, `hours` |
  | `data_stale`, no update | every 10 min: `lastrefresh` age > `stale_no_update_h` fires once; re-armed when the age drops below | `reason "no_update"`, `last_update`, `hours` |
  | `warning_light` | numeric `indicators` greater than the last valid numeric value | `count`, `indicators` (truthy attribute keys) |
  | `night_activity` | inside `[night_start, night_end)` (wrapping midnight allowed): ignition last valid `OFF` → a valid value not in (`OFF`, `Unsupported`), or tracker `home` → another valid state | `reason` (`ignition`/`left_home`), `state`, `time` (HH:MM) |
  | `plug_in_reminder` | daily at `reminder_time`: tracker `home`, SoC < `plug_in_reminder_soc`, `evccstatus` not in (B, C), `elvehplug` ≠ `CONNECTED` | `soc`, `range_km` |
  | `weekly_summary` | at `weekly_time` when the local weekday equals `weekly_day` | `km`, `energy_used`, `consumption`, `charger_energy`, `charger_energy_home`, `cost`, `soc` |

- **Notifier:**
  - Sends only when `settings.notify[NOTIFY_FOR[type]]` is on and at least one target is configured.
  - `tag = f"{DOMAIN}-{entry_id}-{type}"`. `channel` is `"Car alert"` for `night_activity`, `"Car"` otherwise. `high_priority` only for `night_activity`. `click_action` is `dashboard_path` or None.
- **`async_send`:**
  - For each service: skip with a warning if `not hass.services.has_service("notify", svc)`. Data is `{title, message}`; for `mobile_app_*` add `data: {tag, channel, clickAction?}`, plus `priority: "high", ttl: 0` when high.
  - Entities get `notify.send_message` with `{entity_id, title, message}`.
  - Every call is wrapped in try/except: log and continue.
- **Notification copy** (titles exact; the message must contain the listed facts):

  | event | title | message |
  |---|---|---|
  | `charging_complete` / `charging_stopped_early` | "Charging complete" / "Charging stopped early" | `"{end_soc}% (target {target}%), range {range_km} km. {summary}"` |
  | `unlocked` | "Car unlocked" | `"Parked at home|away from home and unlocked for {delay} min. Unlocked: {doors}"` |
  | `open` | "Car open while parked" | `"Open for {delay} min. Doors: …. Windows: …."` |
  | `plug_in_reminder` | "Plug in the car" | `"Battery {soc}%, range {range_km} km, not plugged in."` |
  | `warning_light` | "Car warning light" | `"{count} active. {indicators or 'Check the FordPass app for details.'}"` |
  | `data_stale` | "Car data stopped" | unavailable: `"Ford integration unavailable for {h} h - likely an API or login problem (check the FordConnect Query integration)."`; no update: `"No update from the car for {h} h (last {local time}). Car asleep for a long time, or the integration has stopped."` |
  | `night_activity` | "Car activity at night" | `"Ignition turned {state} at {time}."` / `"Car left home at {time}."` |
  | `weekly_summary` | "Car - this week" | `"{km} km, {used} kWh used ({cons} kWh/100 km). Charged {charged} kWh ({home} kWh at home, {cost} {currency}). Battery now {soc}%."`; the consumption part is omitted under 10 km |
  | `calibration` | "Range model calibrated" | summary |

- [ ] **Step 1: Write the failing tests** (the notify service is mocked with `async_mock_service(hass, "notify", "mobile_app_test_phone")`; options `notify_services: ["mobile_app_test_phone"]`):

```python
async def test_unlocked_fires_once_after_delay(hass, fcq, freezer):  # doorlock UNLOCKED, +9 min none, +10 min one notification titled "Car unlocked"; +20 min still one
async def test_unlocked_rearms_after_lock(hass, fcq, freezer):       # lock, unlock again, +10 min → second notification
async def test_armed_state_survives_restart(hass, fcq, freezer):     # fired, reload, still unlocked → no new notification
async def test_open_lists_windows(hass, fcq, freezer):               # windowposition Open with frontLeft upperBound 10 → message contains "frontLeft"
async def test_warning_light_on_increase_only(hass, fcq):            # 0 → 1 fires; 1 → 0 no; 0 → 1 fires again
async def test_night_activity_window_wraps(hass, fcq, freezer):      # night 22:00–06:00; ignition ON at 23:30 → high priority, channel "Car alert"
async def test_plug_in_reminder(hass, fcq, freezer):                 # 21:00, home, soc 30, unplugged → "Plug in the car"
async def test_weekly_summary_sunday(hass, fcq, freezer):            # Sunday 20:00 → "Car - this week"; Saturday → none
async def test_data_stale_unavailable(hass, fcq, freezer):           # soc unavailable 2 h → "Car data stopped"
async def test_switch_off_suppresses_notification_not_event(hass, fcq, freezer)  # notify_unlocked off → no notification, event.<car>_alert state changed with type "unlocked"
async def test_missing_target_logged_and_others_sent(hass, fcq, freezer, caplog)  # services ["mobile_app_gone", "mobile_app_test_phone"] → one call, warning mentions "mobile_app_gone"
async def test_mobile_app_extras(hass, fcq, freezer):                # data.tag == f"ford_ev-{entry_id}-unlocked", data.channel == "Car"
async def test_charging_complete_notification(hass, fcq, freezer)    # session flow → title "Charging complete"
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_alerts.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `alerts.py`, `notify.py` and `event.py`; wire them in `__init__.py`.
- [ ] **Step 4:** Run it again, then the full `.venv/bin/pytest`. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add alerts, notifications and event entities"`

---

### Task 11: Reset service and diagnostics

**Files:**
- Create: `custom_components/ford_ev/services.yaml`, `custom_components/ford_ev/diagnostics.py`, `tests/test_services_diagnostics.py`
- Modify: `__init__.py` (`CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)`; `async_setup` registers the service), `strings.json` and `translations/en.json`

**Interfaces:**
- Consumes: `engine.reset_range_model()`, `EngineState.to_dict()`.
- Produces:
  - Service `ford_ev.reset_range_model` with field `config_entry_id` (`config_entry` selector, integration `ford_ev`). It is optional when exactly one entry is loaded; otherwise it raises `ServiceValidationError`.
  - `async_get_config_entry_diagnostics(hass, entry) -> dict` returning `{"config": ..., "vehicle_keys": [...], "state": ...}`.

**Redaction rules:**
- `async_redact_data` with `{"vin", "notify_services", "notify_entities", "dashboard_path", "device_id"}`.
- Then a recursive pass replaces any string containing the VIN (case-insensitive) with `"**REDACTED**"`. Entity IDs contain the VIN.
- Coordinates are never included.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_reset_service(hass, fcq):            # after a calibration, call service → calibrations 0, a == seed
async def test_reset_requires_entry_when_two(hass, fcq, fcq2)  # no config_entry_id with two entries → ServiceValidationError
async def test_diagnostics_has_no_vin(hass, fcq, hass_client):
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert VIN.lower() not in json.dumps(diag).lower() and "mobile_app" not in json.dumps(diag)
```

- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_services_diagnostics.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Run it again, then the full `.venv/bin/pytest`. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add reset service and redacted diagnostics"`

---

### Task 12: Dashboard strategy

**Files:**
- Create: `custom_components/ford_ev/frontend/ford-ev-strategy.js`, `tests/js/strategy.test.mjs`, `tests/test_frontend.py`
- Modify: `__init__.py` (`async_setup`: static path and module URL)

**Interfaces:**
- Consumes: entity translation keys from Task 9 and FCQ keys from Task 5. Our device's `via_device_id` → the FCQ device.
- Produces, as JS exports:
  - `findVehicles(hass) -> [{deviceId, name, fcqDeviceId, ours: {key: entity_id}, fcq: {key: entity_id}}]`, from `hass.devices`, `hass.entities` (fields `platform`, `translation_key`, `device_id`) and `via_device_id`;
  - `buildView(vehicle, hass) -> object`;
  - `buildDashboard(hass, config) -> {title: "Ford EV", views}`.
- Produces, on the Python side: `STRATEGY_URL = "/ford_ev/ford-ev-strategy.js"`.
  - `async_setup` calls `hass.http.async_register_static_paths([StaticPathConfig(STRATEGY_URL, <file>, False)])`.
  - Then, if `"frontend" in hass.config.components`, it calls `add_extra_js_url(hass, f"{STRATEGY_URL}?v={VERSION}")`.

**Rules:**
- **Registration:**
  - The custom element `ll-strategy-dashboard-ford-ev` is a class with `static async generate(config, hass)` returning `buildDashboard(hass, config)`.
  - `window.customStrategies = window.customStrategies || []`, then push `{type: "ford-ev", strategyType: "dashboard", name: "Ford EV", description: "Reports, charging, range estimate and alerts for Ford EVs (FordConnect Query)", documentationURL: "https://github.com/mihaics/fordev4ha"}`.
  - Both are guarded with `typeof customElements !== "undefined"` (so Node can import the module) and against double definition.
- **Views:** one `sections` view per vehicle (`max_columns: 4`, `path` = the slugified name). `config.device_id` limits it to one vehicle.
- **Sections and cards:** exactly as listed in §6.8. A card whose entity is missing is skipped. Markdown templates embed the resolved entity IDs.
- **Range estimate markdown:** while the `calibrations` entity is 0, it says "Not calibrated yet: charge to 100 % once." (§6.6).
- **No vehicles:** a single view with a markdown card explaining how to add the integration.

- [ ] **Step 1: Write the failing tests:**

```js
test("one view per vehicle with sections in order", () => {
  const d = buildDashboard(fakeHass(2), {});
  assert.equal(d.views.length, 2);
  assert.deepEqual(headings(d.views[0]), ["Now", "Location", "Driving", "Range estimate", "Charging", "Settings"]);
});
test("every referenced entity exists", () => { for (const e of referencedEntities(buildDashboard(fakeHass(1), {}))) assert.ok(e in fakeHass(1).entities, e); });
test("device_id filter", () => assert.equal(buildDashboard(fakeHass(2), {device_id: "dev_ev_2"}).views.length, 1));
test("missing optional entity skips card", () => { /* drop fcq indicators → no card references it */ });
test("no vehicles shows help", () => assert.match(JSON.stringify(buildDashboard(emptyHass(), {})), /FordConnect Query/));
```

```python
async def test_strategy_served(hass, hass_client, fcq):   # set up http + entry; GET STRATEGY_URL → 200, body contains "ll-strategy-dashboard-ford-ev"
async def test_module_url_registered_when_frontend_loaded(hass):  # hass.config.components.add("frontend"); patch add_extra_js_url → called with STRATEGY_URL + "?v=0.1.0"
```

- [ ] **Step 2:** Run `node --test tests/js/` and `.venv/bin/pytest tests/test_frontend.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement the strategy and its registration.
- [ ] **Step 4:** Run both again, then the full `.venv/bin/pytest`. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -m "Add self-building dashboard strategy"`

---

### Task 13: CI, HACS metadata and README

**Files:**
- Create: `hacs.json`, `.github/workflows/ci.yml`
- Modify: `README.md` (full rewrite)

**Interfaces:**
- Consumes: everything above, for documentation accuracy.

**Rules:**
- **`hacs.json`:** `{"name": "Ford EV", "homeassistant": "2026.5.0", "render_readme": true}`.
- **CI jobs** on push and PR:
  - `hassfest` (`home-assistant/actions/hassfest@master`);
  - `hacs` (`hacs/action@main`, `category: integration`, `ignore: brands`);
  - `tests`: checkout v4, setup-python v5 with 3.14, `pip install -r requirements_test.txt`, `pytest`; setup-node v4 with 24, `node --test tests/js/`.
- **README sections, in order:**
  1. What it does (with a screenshot placeholder path `docs/images/dashboard.png`, added after live validation).
  2. Requirements.
  3. Installation: HACS custom repository step by step; manual copy.
  4. Setup: the config flow fields and what to enter for battery capacity.
  5. Options: every option with its default.
  6. Dashboard: picker steps, the YAML form, "Take control".
  7. Notifications: the table from §6.7, targets, how to use the event entities in your own automations, with one YAML example.
  8. The range model: how it works in plain words, why to charge to 100 % periodically, what calibration does, the capacity estimate, the reset service.
  9. Entity reference: one table.
  10. Privacy: nothing leaves Home Assistant; diagnostics are redacted.
  11. Troubleshooting: `no_vehicles`, no charger energy, model not calibrated, unit systems.
  12. Not affiliated with Ford. License.
- **README content limits:** no personal data, no AI or tool mentions.

- [ ] **Step 1:** Write `hacs.json` and `ci.yml`.
- [ ] **Step 2:** Write the README.
- [ ] **Step 3:** Run `.venv/bin/pytest` and `node --test tests/js/`. Expected: all pass. Run the hassfest container locally: `docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest`. Expected: no errors.
- [ ] **Step 4:** Commit: `git commit -m "Add CI, HACS metadata and installation documentation"`

---

### Task 14: Live validation and first release (needs explicit confirmation at each marked step)

**Files:** none in the repository, except a screenshot `docs/images/dashboard.png` with personal data cropped out.

- [ ] **Step 1:** Copy `custom_components/ford_ev` to the reference Home Assistant (`/config/custom_components/ford_ev`). **Confirm with the user before** restarting Home Assistant core.
- [ ] **Step 2:** Add the integration through the config-flow REST API, matching the YAML's settings (same battery capacity, temperature sensor and weather entity). Configure **no notification targets**, so nothing is sent next to the YAML alerts.
- [ ] **Step 3:** Create a dashboard with `strategy: {type: custom:ford-ev}` through the websocket API. Check that "Ford EV" appears under community dashboards in the "Add dashboard" dialog. The user confirms visually.
- [ ] **Step 4:** After at least 2 days that include one charge and some trips, compare:
  - `ford_ev` deltas against the YAML deltas for energy used, energy charged, km per period, and the charging sessions log;
  - range model behaviour after the next 100 % charge.
  
  Report the differences with numbers. Fix any bug through a test-first change in the relevant task's files.
- [ ] **Step 5:** **Confirm with the user, then:**
  1. push `main`;
  2. set the repository description and topics (`home-assistant`, `hacs`, `ford`, `ev`) with `GH_TOKEN` for the personal account;
  3. tag `v0.1.0`;
  4. create a GitHub release with notes. No AI attribution anywhere.
