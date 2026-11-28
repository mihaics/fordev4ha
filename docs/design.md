# Ford EV for Home Assistant: design

Status: draft for approval. Date: 2026-11-22.

## 1. Intent

Ford EV owners who use the [FordConnect Query](https://github.com/marq24/ha-fordconnect-query) integration (FCQ) get raw vehicle data in Home Assistant: state of charge, range, odometer, plug and charging state, locks, doors, windows, warning lights, location. Turning that into something useful takes a lot of hand-written YAML: energy accounting, trip and charging reports, cost, notifications, a dashboard, and an independent range estimate.

All of this already runs on one installation as YAML packages written against that car's VIN-based entity IDs. This project packages it as one HACS custom integration that any FCQ user can install and configure from the UI, with no YAML and no entity IDs to edit.

The range estimate is the distinctive part. On LFP batteries the car's displayed state of charge drifts between full charges, and with it the car's own range estimate. Here an independent model counts energy in and out, accounts for outside temperature and cabin climate, and recalibrates itself every time the car reaches 100 %.

## 2. Users and prerequisites

- Owners of Ford battery-electric vehicles supported by FCQ (EU/UK/North America).
- Home Assistant 2026.5 or newer. 2026.5 is the release where custom dashboard strategies appear in the "Add dashboard" dialog.
- FCQ installed and working, tested against FCQ 2026.11.0.
- Optional: the Home Assistant Companion app for push notifications; an outside temperature sensor; a weather entity (the default `weather.forecast_home` is enough).

## 3. Goals, non-goals, anti-goals

### Goals

1. Install through HACS as a custom repository, then add the integration in the UI. It finds the FCQ vehicle on its own.
2. Reports: distance per day, week and month; energy used and charged; consumption; charging sessions with a log; charging cost.
3. A range model that is independent of the car's estimate, calibrates at 100 %, accounts for temperature and climate, and keeps what it has learned across restarts.
4. Built-in notifications, each switchable. Every alert is also exposed as an event entity for users who write their own automations.
5. A bundled dashboard that builds itself from the vehicle's entities and can be taken over and edited.
6. Complete README: prerequisites, installation (HACS and manual), configuration, dashboard, notifications, how the range model works, entity reference, troubleshooting.

### Non-goals (first release)

- Plug-in hybrids, combustion Fords, and the FordPass integration (a different API).
- Controlling the car. FCQ is read-only.
- Notification texts in languages other than English. The UI strings live in translation files, so other languages can be added later.
- Seeding the range model from recorder history.
- Listing in the default HACS store. That submission can follow once the integration is stable.
- Charger control, or any other integration besides FCQ, notify, weather, a temperature sensor and the price and solar power entities the user chooses. (The solar share of charging was added in 0.2.0.)

### Anti-goals: things that count as failures even if the code works

- Any vehicle data leaving the Home Assistant instance. The integration makes no network calls of its own.
- Personal data in the repository: real VINs, locations, names, device names, addresses or tokens. Tests use fake VINs.
- Users having to edit YAML or entity IDs.
- Notification spam. An alert fires once per occurrence and re-arms only after its condition has cleared.
- Wrong totals after FCQ outages or Home Assistant restarts. Deltas are always taken against the last known value, so gaps are counted when data returns, never lost and never double-counted.

## 4. Constraints from the data source

FCQ exposes about 20 entities per vehicle. Each has `unique_id` `fcq_uid_<vin>_<key>` and `translation_key` `<key>`. The entities this design uses:

| key | type | used for |
|---|---|---|
| `soc` | sensor, % | energy accounting, calibration trigger, reports |
| `elveh` | sensor, km (attribute `batteryCharge`) | car range estimate |
| `odometer` | sensor, km | distance, trips |
| `ignitionstatus` | sensor, `ON`/`OFF` | trip start/end, parked state |
| `evccstatus` | sensor, `A` unplugged / `B` plugged, not charging / `C` charging | charging sessions |
| `elvehplug` | sensor, `CONNECTED`/`DISCONNECTED` | plugged-in check |
| `elvehcharging` | sensor, attribute `chargerEnergyOutput` | energy delivered by the charger |
| `doorlock` | sensor, `LOCKED`/`UNLOCKED`, attribute per door | unlocked alert |
| `doorstatus` | sensor, `Open`/`Closed`, attribute per door | open alert |
| `windowposition` | sensor, `Open`/`Closed`, attribute per window | open alert |
| `indicators` | sensor, count of active warning lights, attribute per indicator | warning-light alert |
| `lastrefresh` | sensor, timestamp | data age, stale alert |
| `tracker` | device_tracker | home/away, map |

Facts established on a live system that the design depends on:

- **No outside temperature and no climate state.** The temperature comes from a user-chosen sensor, with the weather entity as fallback. Whether climate is on while driving is a user setting.
- **Use `evccstatus` and `elvehplug` for charging state.** The `elvehcharging` state has been seen to miss an entire home charging session.
- **`chargerEnergyOutput` counts kWh within one charging run**, in steps of about 0.5 kWh. It restarts near zero when a run restarts, and its last value persists after unplugging. Counting rule: a rise adds the difference; a drop means a new run and adds the new value.
- **The odometer can arrive in a later update than the ignition change.** Trip accounting waits about 2 minutes after ignition off.
- **The car sleeps.** Updates can be hours apart, and the integration can be unavailable for long periods.
- **Unit conversion.** States read from `hass.states` are in the entity's displayed unit, which Home Assistant converts to imperial for imperial users. The integration reads each value's `unit_of_measurement` and converts to km and °C internally.

## 5. Approaches considered

1. **One integration that does everything (chosen).** Discovery, accounting, the range model and notifications are Python inside the integration. State is persisted in its own storage. The dashboard is a bundled strategy. It is the most code to write, but installation is a single step, behaviour is identical everywhere, and every part can be tested automatically.
2. **Integration for the data, blueprints for notifications.** The same core, but each alert is an automation blueprint that the user imports and configures. Rejected: about 8 imports per user, HACS cannot install blueprints, and the two parts can drift out of sync between releases.
3. **Generalised YAML packages.** Rejected: no discovery, users must substitute VIN-based entity IDs, learned values live in loose helper entities, and updating means copying files again.

## 6. Design

### 6.1 Repository and packaging

```
custom_components/ford_ev/      integration (domain ford_ev, display name "Ford EV")
  frontend/ford-ev-strategy.js  dashboard strategy, plain ES module, no build step
  translations/en.json
tests/                          pytest-homeassistant-custom-component
docs/design.md                  this document
hacs.json                       minimum HA version 2026.5
README.md, LICENSE (MIT)
.github/workflows/              hassfest, HACS validation, pytest
```

- Releases are GitHub releases with semantic-version tags. HACS installs from them.
- The README and the manifest state that the project is not affiliated with Ford.

### 6.2 Setup (config flow)

1. **Vehicle.** The flow lists FCQ devices that have a `soc` entity, found by scanning the entity registry for `platform == fordconnect_query` and grouping by device. Matching uses `translation_key`, never entity IDs, so renamed entities still work. One vehicle is preselected. Several vehicles mean one config entry each, and a vehicle already configured is not offered again.
2. **Battery.** Usable capacity in kWh is required, because FCQ does not report it. Help text explains where to find it.
3. **Temperature.** An outside temperature sensor (optional) and a weather entity. At least one is required; the weather entity is needed for tomorrow's forecast.

The flow aborts with a clear message if FCQ is not installed or no vehicle has the required entities. Required entities: `soc`, `odometer`, `ignitionstatus`, `evccstatus`, `tracker`. Others are optional, and the features that need them switch off when they are missing.

### 6.3 Options flow: settings changed rarely

- **Notification targets:** any `notify` services (for example `mobile_app_*`) and/or notify entities; none by default.
- **Dashboard path** used as the tap action of notifications (optional).
- **Electricity price:** a numeric entity (sensor or input_number, price per kWh) or a fixed value. Currency is Home Assistant's configured currency.
- **Alert timing:**

  | setting | default |
  |---|---|
  | unlocked / open delay | 10 min |
  | plug-in reminder time | 21:00 |
  | night window | 00:00–06:00 |
  | weekly summary | Sunday 20:00 |
  | stale-data limits | unavailable 2 h, no update 48 h |

- **Battery capacity and temperature sources**, so they can be changed after setup.

Settings changed day to day are entities instead (6.5).

### 6.4 Engine

There is one engine object per config entry. It subscribes to state changes of the vehicle's FCQ entities and to time triggers. It writes all internal state to a versioned `Store` (`ford_ev.<entry_id>`), with delayed save on change and a final save on shutdown. Unavailable and unknown states are ignored.

Inspectable parts are pure functions with no Home Assistant dependency, unit-tested on their own: energy accounting, the charger counter, period rollover, the trip model and the calibration fit.

**Energy accounting from SoC** (battery side, SoC delta × capacity, against the last known SoC):
- SoC rise while plugged (`evccstatus` B/C or `elvehplug` CONNECTED), or a rise of 5 points or more, counts as energy added, and also as added at home if the tracker is `home`. It clears the regeneration credit.
- A smaller rise while unplugged is regeneration. It accumulates as a credit, capped at 100 points.
- A drop counts as energy used, minus the credit; the credit is reduced by the drop.

**Charger energy:**
- `chargerEnergyOutput` is counted with the rule in section 4 into "energy delivered by charger", plus an at-home counter.
- If a charging session ends with no charger increments but the SoC rose, the session's SoC-based energy is added instead, so a session is never missing. This also covers vehicles that do not report `chargerEnergyOutput` at all. Fallback energy counts everywhere that charger energy does: the counters, cost, model energy and the calibration cycle's energy charged.
- Cost is the at-home charger energy increments × the price at the moment each increment is counted, so variable tariffs are priced correctly.

**Charging sessions:**
- Start: `evccstatus` becomes `C` while no session is open. Record start time, SoC and the charger counter.
- End: `C` → `A`/`B`, or anything → `A`, while a session is open, after a 2-minute settle delay.
- Each session is recorded as start, end, duration, start and end SoC, kWh, cost and location. The last session is a sensor; its history is the charging log.

**Periods:**
- The engine keeps snapshots of the cumulative counters (odometer, energy used, energy added, charger energy, at-home charger energy, cost) at the start of each local day, week (Monday) and month.
- Period sensors show the difference since the snapshot, with the previous period's value as an attribute.

**Restart recovery.** Pending delays are lost on restart, so on startup the engine reconciles them:
- an open trip with ignition `OFF` is accounted;
- an open session with `evccstatus` not `C` is closed.

### 6.5 Entities

Everything belongs to one device per vehicle, "Ford EV <vehicle name>", linked to the FCQ device with `via_device`. All numeric sensors carry `state_class`, so long-term statistics and the dashboard's statistics graphs work.

**Reports:**
- **Energy:** used; added to battery; delivered by charger; delivered by charger at home. All total_increasing kWh.
- **Charging cost:** total, monetary.
- **Per period:** distance today, this week and this month; energy used, charger energy, at-home charger energy and cost for the week and month.
- **Consumption:** week and month, device class `energy_distance`, so users can display mi/kWh or km/kWh. No value under 10 km.
- **From the car's own estimate:** full range (range ÷ SoC × 100) and km per %.
- **Status:** charging state (enum: unplugged / plugged, not charging / charging); data age in minutes; last charging session.

**Range model:**
- **Inputs and estimates:** outside temperature used by the model; model consumption, with driving, climate, climate kW and average speed as attributes; model SoC, with the difference to the car's SoC as an attribute; model range, with "model consumption × car SoC" as an attribute.
- **Range at 100 %:** now, with a by-temperature table (−15…35 °C) as an attribute; and tomorrow, from the forecast.
- **Calibration:** drive factor, climate factor, last cycle error; number of calibrations; last calibration (timestamp, with cycle details and the capacity estimate as attributes).

**Binary sensor:** charging session active.

**Controls:**
- **Numbers:** charge target SoC (used to tell "complete" from "stopped early"), plug-in reminder threshold, climate setpoint (default 21 °C).
- **Switches:** climate on while driving; one switch per notification type, `entity_category: config`.

**Events:**
- `event.<car>_alert`, with types `unlocked`, `open`, `warning_light`, `data_stale`, `night_activity` and `plug_in_reminder`.
- `event.<car>_charging`, with types `session_started`, `charging_complete`, `charging_stopped_early` and `unplugged_while_charging`.
- `event.<car>_report`, with types `weekly_summary` and `calibration`.
- Each event carries the same data as its notification.

**Service:** `ford_ev.reset_range_model` (learned factors back to their initial values, model SoC from the car's SoC), for when the battery capacity was set wrong.

**Diagnostics:** download with the VIN, location and coordinates redacted.

### 6.6 Range model

Ported unchanged from the reference YAML.

**Trip energy:**

```
drive(km, T)       = km × 0.16 kWh/km × (1 + 0.01 × max(0, 20 − T))
climate(T, sp, on) = on ? 0.12 kW × max(0, sp − T) + 0.08 kW × max(0, T − sp) : 0
trip energy        = a × drive(km, T) + b × hours × climate(T, sp, on)
predicted kWh/km   = a × drive(1, T) + b × climate(T, sp, on) / max(average speed, 5)
```

**Per trip** (ignition `ON` → `OFF`, accounted 2 min after `OFF`):
- **Distance:** odometer minus the last accounted odometer. A distance under 0.5 km is skipped. If the odometer is lower than the accounted value, it is resynced and the trip skipped.
- **Duration:** from the ignition timestamps. If the start was missed, or the duration is implausible (not between 0 and 12 h, or above 160 km/h average), duration = km ÷ average speed.
- **Temperature:** the mean of the temperatures at start and end, or the end temperature if the start is not valid.
- **Average speed:** an exponential average, 0.8 old + 0.2 new, clamped to 10–130 km/h. Updated only from valid trips of at least 0.1 h and 2 km.
- **Effects:** the trip energy is subtracted from model energy, and the cycle accumulators grow: x_drive += drive, x_hvac += hours × climate, km += km.

**Charging:** charger increments are added to model energy (capped at capacity) and to the cycle's energy charged, y.

**Calibration:**
- **Trigger:** car SoC ≥ 99.5 % for 5 min, cycle y ≥ 1 kWh, and more than 6 h since the last calibration.
- **Principle:** at 100 % the battery is full again, so y is the energy really used in the cycle.
- **Validity:** a cycle counts only if x_drive + x_hvac ≥ 3 kWh and 0.5 ≤ y ÷ prediction ≤ 2.
- **Fit:** ridge least squares with forgetting λ = 0.85 and prior weight w = 100 towards (a₀, b₀):

  ```
  S ← λS + [x1², x1·x2, x2², x1·y, x2·y]
  [a, b] = solve([[S11 + w, S12], [S12, S22 + w]], [S1y + w·a₀, S2y + w·b₀])
  a ∈ [0.5, 2], b ∈ [0.2, 3]
  ```

- **Every calibration, valid or not:** model energy is reset to capacity, the cycle accumulators are cleared, and a calibration event is recorded with: km, y, prediction, error %, new factors (or "not used" and the ratio), and SoC at the start of the final charge as seen by the car, the model and the charger energy (100 − session kWh ÷ capacity × 100).

**Additions to the YAML version (proposed):**
- **Seeded drive factor.** At setup, a₀ is seeded from the car's own estimate: implied consumption = SoC × capacity ÷ range, divided by drive(1, current T), and clamped. If SoC is below 20 % or the car's range is missing, a₀ = 1. The model then starts close for vehicles that differ from the 0.16 kWh/km baseline, for example a pickup. b₀ = 1. The ridge prior pulls towards (a₀, b₀).
- **Capacity estimate.** Each calibration with a final session from below 80 % reports a usable-capacity estimate: session charger kWh ÷ (100 − start SoC) × 100. It is shown on the dashboard and never applied automatically.

**Before the first calibration**, model SoC starts from the car's SoC at setup, and the dashboard says the model is not yet calibrated.

### 6.7 Notifications

Each notification is sent to the configured targets when its switch is on. For `mobile_app` targets it also sets a tag (newer replaces older), an Android channel (`Car`, or `Car alert` for night activity, which is also high priority) and the tap action (the configured dashboard path). Other notify targets get only a title and message.

| type | condition | default |
|---|---|---|
| Charging complete / stopped early | `C` → `B`. "Complete" if SoC ≥ target − 1. Includes SoC, range and the session summary | on |
| Car unlocked | ignition `OFF` and `doorlock` `UNLOCKED` for the delay. Lists unlocked doors and home/away | on |
| Open while parked | ignition `OFF` and a door or window open for the delay. Lists which | on |
| Plug-in reminder | at the reminder time: car at home, SoC below threshold, not plugged | on |
| Warning light | `indicators` count increased. Lists active indicators | on |
| Car data stopped | FCQ `soc` unavailable for 2 h, or no update for 48 h | on |
| Night activity | ignition changes from `OFF`, or the car leaves home, inside the night window | on |
| Weekly summary | weekly time: km, kWh used, consumption, kWh charged (at home), cost, current SoC | on |
| Range calibration | after each calibration: error, new factors, capacity estimate | off |

### 6.8 Dashboard

The integration serves `frontend/ford-ev-strategy.js` and loads it on every frontend page with `frontend.add_extra_js_url`, so users never add a resource by hand. The script:
- defines `ll-strategy-dashboard-ford-ev`;
- registers with `window.customStrategies` (`strategyType: "dashboard"`), so "Ford EV" appears under community dashboards in the "Add dashboard" dialog.

The strategy finds the vehicle's entities through `hass.entities` (platform `ford_ev` and FCQ, by translation key). It generates one sections view per configured vehicle. A vehicle can be selected in the strategy config, with all vehicles as the default.

Sections:
- **Now:** battery gauge, range, charging state, locks, doors, windows, ignition, warning lights, odometer, last update.
- **Location:** map.
- **Driving:**
  - tiles: km today, week, month; consumption, week and month; car's full range;
  - statistics graphs: km per day, energy used per week, full-range trend;
  - battery history.
- **Range estimate:**
  - tiles: car and model range; car and model SoC; model range at 100 %, now and tomorrow; model consumption; outside temperature;
  - history graphs: car vs model, for range and SoC;
  - a markdown card: consumption breakdown, by-temperature table, calibration status and the current cycle;
  - statistics: full range model vs car; learned factors;
  - calibration log.
- **Charging:**
  - tiles: charger energy this month and at home; cost this month and week; session active;
  - last session;
  - charging log, from the last-session sensor's history;
  - statistics: energy charged per month.
- **Settings:** climate switch and setpoint, charge target, plug-in reminder threshold, notification switches.

"Take control" in the dashboard menu converts it to an ordinary editable dashboard, which is standard Home Assistant behaviour. The README also documents the YAML form, `strategy: {type: custom:ford-ev}`.

### 6.9 Testing and validation

- **Unit tests:** accounting, the charger counter, period rollover, trip energy, the calibration fit, and unit conversion. Edge cases: FCQ unavailable mid-trip, restart with a pending trip or session, a charger counter reset, imperial units, an odometer going backwards.
- **Integration tests** with fake FCQ entities (fake VIN) registered in the entity registry: the config flow, discovery with renamed entity IDs, entity creation, notifications sent to a mocked notify service, events fired, storage round trip.
- **CI:** hassfest, HACS validation, pytest.
- **Live validation** before the first release: install on the reference system next to its YAML version, with built-in notifications off, and compare sensors and events for a few days.

### 6.10 Repository hygiene (public repo)

- Fake VINs and coordinates in tests and documentation.
- No real entity IDs, device names, addresses or credentials.
- Commits use a noreply address.
- A local pre-commit denylist, never committed, blocks known personal strings.

## 7. Calls made by the author of this document

These were not decided explicitly and can be overridden:

- **Names:** domain `ford_ev`, display name "Ford EV", minimum Home Assistant 2026.5.
- **Day-to-day vs set-once settings:** day-to-day settings (climate, setpoint, charge target, reminder threshold, alert on/off) are entities; set-once settings are in the options flow.
- **Charging cost:** charger-reported energy, priced per increment. (Since 0.2.0 charging away from home is priced at the public price.)
- **Defaults:** all notifications on except the calibration notification; English notification texts only.
- **Range model additions:** the seeded drive factor, the capacity estimate, and the reset service.
- **Dashboard:** plain JavaScript strategy, no build step.

## 8. Left to the builder

- Module layout inside `custom_components/ford_ev/`.
- Exact entity names and icons, and the translation strings.
- How the strategy lays out cards within each section.
- Debounce details beyond the delays stated above.
