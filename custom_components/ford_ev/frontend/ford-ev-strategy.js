// Ford EV dashboard strategy: builds one view per vehicle from the integration's entities.
// Loaded on every frontend page by the integration; also importable by Node for tests.

const DOMAIN = "ford_ev";
const FCQ_DOMAIN = "fordconnect_query";
const DOCS = "https://github.com/mihaics/fordev4ha";

export function findVehicles(hass) {
  const entities = Object.values(hass.entities || {});
  const devices = hass.devices || {};
  const byDevice = new Map();
  for (const e of entities) {
    if (e.platform !== DOMAIN || !e.device_id || !e.translation_key) continue;
    if (!byDevice.has(e.device_id)) byDevice.set(e.device_id, {});
    byDevice.get(e.device_id)[e.translation_key] = e.entity_id;
  }
  const vehicles = [];
  for (const [deviceId, ours] of byDevice) {
    const device = devices[deviceId] || {};
    const fcqDeviceId = device.via_device_id || null;
    const fcq = {};
    if (fcqDeviceId) {
      for (const e of entities) {
        if (e.platform === FCQ_DOMAIN && e.device_id === fcqDeviceId && e.translation_key) fcq[e.translation_key] = e.entity_id;
      }
    }
    const name = (device.name_by_user || device.name || "Ford EV").replace(/^Ford EV /, "");
    vehicles.push({ deviceId, name, fcqDeviceId, ours, fcq });
  }
  return vehicles.sort((a, b) => a.name.localeCompare(b.name));
}

function slug(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "vehicle";
}

function section(heading, cards) {
  return { type: "grid", cards: [{ type: "heading", heading }, ...cards.filter(Boolean)] };
}

function tile(entity, name) {
  return entity ? { type: "tile", entity, name } : null;
}

function present(list) {
  return list.filter((item) => item && (typeof item === "string" ? item : item.entity));
}

function history(title, entities, hours = 168) {
  const items = present(entities);
  return items.length ? { type: "history-graph", title, entities: items, hours_to_show: hours } : null;
}

function statistics(title, entities, statType, period, days, chartType) {
  const items = present(entities);
  if (!items.length) return null;
  return {
    type: "statistics-graph",
    title,
    entities: items,
    stat_types: [statType],
    period,
    days_to_show: days,
    chart_type: chartType,
  };
}

function entitiesCard(title, rows) {
  const items = present(rows);
  return items.length ? { type: "entities", title, entities: items } : null;
}

function rangeMarkdown(o, f) {
  const soc = f.soc ? `{{ states('${f.soc}') }}` : "?";
  return `{% set cal = states('${o.calibrations}') | int(0) %}{% if cal == 0 %}**Not calibrated yet: charge to 100 % once.**

{% endif %}**Model now**: {{ states('${o.outside_temperature}') }} °C, climate {{ 'on' if is_state('${o.climate_on}', 'on') else 'off' }} at {{ states('${o.climate_setpoint}') }} °C, average speed {{ state_attr('${o.model_consumption}', 'average_speed') }} km/h:
driving {{ state_attr('${o.model_consumption}', 'driving') }} + climate {{ state_attr('${o.model_consumption}', 'climate') }} ({{ state_attr('${o.model_consumption}', 'climate_kw') }} kW) = **{{ states('${o.model_consumption}') }} kWh/100 km**.

**State of charge**: model {{ states('${o.model_soc}') }} % vs car ${soc} % (difference {{ state_attr('${o.model_soc}', 'difference_vs_car') }}). With the car's SoC the model gives {{ state_attr('${o.model_range}', 'with_car_soc') }} km.

| Outside | Range at 100 % | kWh/100 km |
|---:|---:|---:|
{% for r in state_attr('${o.model_range_full}', 'by_temperature') or [] %}| {{ r.t }} °C | {{ r.km }} km | {{ r.kwh100 }} |
{% endfor %}
**Calibration**: {{ cal }} done, last {{ as_timestamp(states('${o.last_calibration}'), 0) | timestamp_custom('%d.%m %H:%M') if states('${o.last_calibration}') not in ['unknown', 'unavailable'] else 'never' }}. Drive x{{ states('${o.drive_factor}') }}, climate x{{ states('${o.climate_factor}') }}, last cycle error {{ states('${o.cycle_error}') }} %.
{% set capest = state_attr('${o.last_calibration}', 'capacity_estimate_kwh') %}{% if capest %}Usable capacity estimate from the last full charge: {{ capest | round(1) }} kWh.{% endif %}
This cycle: {{ state_attr('${o.calibrations}', 'cycle_km') }} km, charged {{ state_attr('${o.calibrations}', 'cycle_charged_kwh') }} kWh, model used {{ state_attr('${o.calibrations}', 'cycle_model_kwh') }} kWh.

<small>The model resets to full and refits its factors every time the car reaches 100 %.</small>`;
}

function chargingMarkdown(o) {
  return `**Last session**: {{ states('${o.last_charge}') if states('${o.last_charge}') not in ['unknown', 'unavailable'] else 'none yet' }}`;
}

const NOTIFY_KEYS = [
  "charging", "unlocked", "open", "plug_in_reminder", "warning_light",
  "data_stale", "night_activity", "weekly_summary", "calibration",
];

export function buildView(vehicle) {
  const o = vehicle.ours;
  const f = vehicle.fcq;
  const sections = [
    section("Now", [
      f.soc ? { type: "gauge", entity: f.soc, name: "Battery", min: 0, max: 100, severity: { green: 50, yellow: 20, red: 0 } } : null,
      tile(f.elveh, "Range"),
      tile(o.charging_state, "Charging"),
      tile(f.doorlock, "Locks"),
      tile(f.doorstatus, "Doors"),
      tile(f.windowposition, "Windows"),
      tile(f.ignitionstatus, "Ignition"),
      tile(f.indicators, "Warning lights"),
      tile(f.odometer, "Odometer"),
      tile(f.lastrefresh, "Last update from car"),
    ]),
    section("Location", [f.tracker ? { type: "map", entities: [f.tracker], hours_to_show: 24 } : null]),
    section("Driving", [
      tile(o.distance_day, "km today"),
      tile(o.distance_week, "km this week"),
      tile(o.distance_month, "km this month"),
      tile(o.consumption_week, "kWh/100 km week"),
      tile(o.consumption_month, "kWh/100 km month"),
      tile(o.car_full_range, "Car estimate at 100 %"),
      statistics("km per day", [f.odometer], "change", "day", 30, "bar"),
      statistics("Energy used per week (kWh)", [o.energy_used], "change", "week", 90, "bar"),
      statistics("Car estimate at 100 % (efficiency trend)", [o.car_full_range], "mean", "day", 90, "line"),
      history("Battery", [f.soc]),
    ]),
    section("Range estimate", [
      tile(f.elveh, "Car range"),
      tile(o.model_range, "Model range"),
      tile(f.soc, "Car SoC"),
      tile(o.model_soc, "Model SoC"),
      tile(o.model_range_full, "Model, 100 % now"),
      tile(o.model_range_full_tomorrow, "Model, 100 % tomorrow"),
      tile(o.model_consumption, "Model consumption"),
      tile(o.outside_temperature, "Outside"),
      history("Range: car vs model", [
        f.elveh ? { entity: f.elveh, name: "Car" } : null,
        o.model_range ? { entity: o.model_range, name: "Model" } : null,
      ]),
      history("SoC: car vs model", [
        f.soc ? { entity: f.soc, name: "Car" } : null,
        o.model_soc ? { entity: o.model_soc, name: "Model" } : null,
      ]),
      o.model_consumption ? { type: "markdown", content: rangeMarkdown(o, f) } : null,
      statistics("Range at 100 %: model vs car (daily mean)", [
        o.model_range_full ? { entity: o.model_range_full, name: "Model" } : null,
        o.car_full_range ? { entity: o.car_full_range, name: "Car" } : null,
      ], "mean", "day", 90, "line"),
      statistics("Learned factors", [
        o.drive_factor ? { entity: o.drive_factor, name: "Drive" } : null,
        o.climate_factor ? { entity: o.climate_factor, name: "Climate" } : null,
      ], "mean", "week", 365, "line"),
      o.last_calibration ? { type: "logbook", title: "Calibrations", entities: [o.last_calibration], hours_to_show: 24 * 60 } : null,
    ]),
    section("Charging", [
      tile(o.charger_energy_month, "Charged this month"),
      tile(o.charger_energy_home_month, "Charged at home, month"),
      tile(o.cost_month, "Cost this month"),
      tile(o.cost_week, "Cost this week"),
      tile(o.charging_session, "Session active"),
      o.last_charge ? { type: "markdown", content: chargingMarkdown(o) } : null,
      o.last_charge ? { type: "logbook", title: "Charging log", entities: [o.last_charge], hours_to_show: 24 * 30 } : null,
      statistics("Energy charged per month (kWh)", [
        o.charger_energy ? { entity: o.charger_energy, name: "All" } : null,
        o.charger_energy_home ? { entity: o.charger_energy_home, name: "Home" } : null,
      ], "change", "month", 365, "bar"),
    ]),
    section("Settings", [
      entitiesCard("Range model", [o.climate_on, o.climate_setpoint, o.charge_target, o.plug_in_reminder_soc]),
      entitiesCard("Notifications", NOTIFY_KEYS.map((k) => o[`notify_${k}`])),
    ]),
  ];
  return { title: vehicle.name, path: slug(vehicle.name), type: "sections", max_columns: 4, sections };
}

export function buildDashboard(hass, config = {}) {
  let vehicles = findVehicles(hass);
  if (config.device_id) vehicles = vehicles.filter((v) => v.deviceId === config.device_id);
  if (!vehicles.length) {
    return {
      title: "Ford EV",
      views: [
        {
          title: "Ford EV",
          cards: [
            {
              type: "markdown",
              content:
                "No Ford EV vehicle found. Install FordConnect Query, then add the **Ford EV** integration " +
                `under Settings > Devices & services. Documentation: ${DOCS}`,
            },
          ],
        },
      ],
    };
  }
  return { title: "Ford EV", views: vehicles.map((v) => buildView(v)) };
}

if (typeof customElements !== "undefined" && !customElements.get("ll-strategy-dashboard-ford-ev")) {
  class FordEvDashboardStrategy extends HTMLElement {
    static async generate(config, hass) {
      return buildDashboard(hass, config);
    }
  }
  customElements.define("ll-strategy-dashboard-ford-ev", FordEvDashboardStrategy);
  window.customStrategies = window.customStrategies || [];
  if (!window.customStrategies.some((s) => s.type === "ford-ev")) {
    window.customStrategies.push({
      type: "ford-ev",
      strategyType: "dashboard",
      name: "Ford EV",
      description: "Reports, charging, range estimate and alerts for Ford EVs (FordConnect Query)",
      documentationURL: DOCS,
    });
  }
}
