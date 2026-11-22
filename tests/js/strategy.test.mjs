import { test } from "node:test";
import assert from "node:assert/strict";

import { buildDashboard, findVehicles } from "../../custom_components/ford_ev/frontend/ford-ev-strategy.js";

const OURS = [
  "energy_used", "energy_added", "charger_energy", "charger_energy_home", "charging_cost",
  "distance_day", "distance_week", "distance_month", "energy_used_week", "energy_used_month",
  "charger_energy_week", "charger_energy_month", "charger_energy_home_week", "charger_energy_home_month",
  "cost_week", "cost_month", "consumption_week", "consumption_month", "car_full_range", "car_km_per_percent",
  "charging_state", "data_age", "last_charge", "outside_temperature", "model_consumption", "model_soc",
  "model_range", "model_range_full", "model_range_full_tomorrow", "drive_factor", "climate_factor",
  "cycle_error", "calibrations", "last_calibration", "charging_session", "charge_target",
  "plug_in_reminder_soc", "climate_setpoint", "climate_on", "notify_charging", "notify_unlocked",
  "notify_open", "notify_plug_in_reminder", "notify_warning_light", "notify_data_stale",
  "notify_night_activity", "notify_weekly_summary", "notify_calibration", "alert", "charging", "report",
];
const FCQ = [
  "soc", "elveh", "odometer", "ignitionstatus", "evccstatus", "elvehplug", "elvehcharging",
  "doorlock", "doorstatus", "windowposition", "indicators", "lastrefresh", "tracker",
];

function fakeHass(n, { dropFcq = [] } = {}) {
  const entities = {};
  const devices = {};
  for (let i = 1; i <= n; i++) {
    devices[`dev_fcq_${i}`] = { id: `dev_fcq_${i}`, name: `Car ${i}` };
    devices[`dev_ev_${i}`] = { id: `dev_ev_${i}`, name: `Ford EV Car ${i}`, via_device_id: `dev_fcq_${i}` };
    for (const key of OURS) {
      const id = `sensor.ford_ev_car_${i}_${key}`;
      entities[id] = { entity_id: id, platform: "ford_ev", device_id: `dev_ev_${i}`, translation_key: key };
    }
    for (const key of FCQ) {
      if (dropFcq.includes(key)) continue;
      const domain = key === "tracker" ? "device_tracker" : "sensor";
      const id = `${domain}.fcq_car${i}_${key}`;
      entities[id] = { entity_id: id, platform: "fordconnect_query", device_id: `dev_fcq_${i}`, translation_key: key };
    }
  }
  return { entities, devices, states: {} };
}

function headings(view) {
  return view.sections.map((s) => s.cards.find((c) => c.type === "heading").heading);
}

function referencedEntities(node, out = new Set()) {
  if (Array.isArray(node)) node.forEach((n) => referencedEntities(n, out));
  else if (node && typeof node === "object") {
    if (typeof node.entity === "string") out.add(node.entity);
    if (Array.isArray(node.entities)) node.entities.forEach((e) => out.add(typeof e === "string" ? e : e.entity));
    Object.values(node).forEach((v) => referencedEntities(v, out));
  }
  return out;
}

test("findVehicles groups our entities and links FCQ ones", () => {
  const [v] = findVehicles(fakeHass(1));
  assert.equal(v.name, "Car 1");
  assert.equal(v.ours.model_range, "sensor.ford_ev_car_1_model_range");
  assert.equal(v.fcq.soc, "sensor.fcq_car1_soc");
});

test("one view per vehicle with sections in order", () => {
  const d = buildDashboard(fakeHass(2), {});
  assert.equal(d.title, "Ford EV");
  assert.equal(d.views.length, 2);
  assert.equal(d.views[0].type, "sections");
  assert.deepEqual(headings(d.views[0]), ["Now", "Location", "Driving", "Range estimate", "Charging", "Settings"]);
});

test("every referenced entity exists", () => {
  const hass = fakeHass(1);
  for (const e of referencedEntities(buildDashboard(hass, {}))) assert.ok(e in hass.entities, e);
});

test("device_id filter", () => {
  const d = buildDashboard(fakeHass(2), { device_id: "dev_ev_2" });
  assert.equal(d.views.length, 1);
  assert.equal(d.views[0].title, "Car 2");
});

test("missing optional entity skips card", () => {
  const hass = fakeHass(1, { dropFcq: ["indicators"] });
  const refs = referencedEntities(buildDashboard(hass, {}));
  assert.ok(![...refs].some((e) => e.endsWith("_indicators")));
});

test("markdown embeds resolved entity ids and calibration hint", () => {
  const text = JSON.stringify(buildDashboard(fakeHass(1), {}));
  assert.match(text, /sensor\.ford_ev_car_1_model_consumption/);
  assert.match(text, /Not calibrated yet: charge to 100 % once\./);
});

test("no vehicles shows help", () => {
  const d = buildDashboard({ entities: {}, devices: {}, states: {} }, {});
  assert.equal(d.views.length, 1);
  assert.match(JSON.stringify(d), /FordConnect Query/);
});
