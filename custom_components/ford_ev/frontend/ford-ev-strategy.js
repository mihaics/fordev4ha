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
    // Device names are "EV" (one vehicle) or "EV <vehicle name>".
    const name = (device.name_by_user || device.name || "EV").replace(/^(Ford )?EV (?=.)/, "");
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
  const num = (id, digits) => `{{ states('${id}') | float(0) | round(${digits}) }}`;
  const unit = (id) => `{{ state_attr('${id}', 'unit_of_measurement') }}`;
  const attr = (id, a) => `{{ state_attr('${id}', '${a}') }}`;
  const carSoc = f.soc ? `, car ${num(f.soc, 0)} %` : "";
  return `{% set cal = states('${o.calibrations}') | int(0) %}{% if cal == 0 %}**Not calibrated yet: charge to 100 % once.**

{% endif %}**Now:** ${num(o.outside_temperature, 1)} ${unit(o.outside_temperature)}, climate {{ 'on' if is_state('${o.climate_on}', 'on') else 'off' }} at ${num(o.climate_setpoint, 1)} ${unit(o.climate_setpoint)}, average ${attr(o.model_consumption, "average_speed")} km/h.
**Consumption:** ${num(o.model_consumption, 1)} kWh/100 km (driving ${attr(o.model_consumption, "driving")}, climate ${attr(o.model_consumption, "climate")}).
**Battery level:** model ${num(o.model_soc, 0)} %${carSoc}.

| Outside | Range at 100 % | kWh/100 km |
|---:|---:|---:|
{% for r in state_attr('${o.model_range_full}', 'by_temperature') or [] %}| {{ r.t }} °C | {{ r.km }} km | {{ r.kwh100 }} |
{% endfor %}
**Calibrations:** {{ cal }}, last {{ as_timestamp(states('${o.last_calibration}'), 0) | timestamp_custom('%d.%m %H:%M') if states('${o.last_calibration}') not in ['unknown', 'unavailable'] else 'never' }}. Drive x${num(o.drive_factor, 2)}, climate x${num(o.climate_factor, 2)}, last error {{ states('${o.cycle_error}') | float(0) | round(1) if states('${o.cycle_error}') not in ['unknown', 'unavailable'] else '-' }} %.
{% set capest = state_attr('${o.last_calibration}', 'capacity_estimate_kwh') %}{% if capest %}**Capacity estimate:** {{ capest | round(1) }} kWh at the charger.
{% endif %}**This cycle:** ${attr(o.calibrations, "cycle_km")} km, ${attr(o.calibrations, "cycle_charged_kwh")} kWh charged{% set refill = state_attr('${o.calibrations}', 'cycle_refill_kwh') %}{% if refill %} ({{ refill | round(1) }} kWh of it to fill the battery from where it was at setup){% endif %}, model ${attr(o.calibrations, "cycle_model_kwh")} kWh.`;
}

function chargingMarkdown(o) {
  return `**Last charge:** {{ states('${o.last_charge}') if states('${o.last_charge}') not in ['unknown', 'unavailable'] else 'none yet' }}`;
}

function tripMarkdown(o) {
  return `**Last trip:** {{ states('${o.last_trip}') if states('${o.last_trip}') not in ['unknown', 'unavailable'] else 'none yet' }}`;
}

function tripStatsMarkdown(o) {
  const id = o.trips;
  return `${ATTR_MACRO}
{% if (state_attr('${id}', 'trips') or 0) > 0 %}**Last {{ state_attr('${id}', 'trips') }} trips:** median {{ v('${id}', 'median_km', 1) }} km, longest {{ v('${id}', 'longest_km', 0) }} km, {{ v('${id}', 'short_trip_km_pct', 0) }} % of the km in trips under 10 km.
{% set err = state_attr('${id}', 'model_error_pct') %}{% if err is number %}**Model on trips of 20 km or more:** {{ '%+.1f' | format(err) }} % ({{ state_attr('${id}', 'model_error_trips') }} trips).
{% endif %}{% set rows = state_attr('${id}', 'by_speed') or [] %}{% if rows %}
| Average speed | km | kWh/100 km |
|--:|--:|--:|
{% for r in rows %}| from {{ r.speed }} km/h | {{ r.km }} | {{ r.kwh100 if r.kwh100 is not none else '-' }} |
{% endfor %}{% endif %}{% else %}**Trips:** none yet{% endif %}`;
}

// Jinja: an attribute as a rounded number, or "-" while it has no value.
const ATTR_MACRO = "{% macro v(id, a, d) %}{% set x = state_attr(id, a) %}{{ x | round(d) if x is number else '-' }}{% endmacro %}";

function costMarkdown(o) {
  // A period that began before tracking did says since when it counts, on a second line.
  // Five short columns, so the table fits a phone.
  const since = (id) =>
    `{% set s = state_attr('${id}', 'since') %}{% if s %}<br><small>since&nbsp;{{ strptime(s, '%Y-%m-%d').strftime('%-d&nbsp;%b') }}</small>{% endif %}`;
  const rows = [
    ["Week", o.cost_per_100km_week, true],
    ["Month", o.cost_per_100km_month, true],
    ["Year", o.cost_per_100km_year, true],
    ["Since start", o.cost_per_100km_total, false],
  ].filter(([, id]) => id);
  if (!rows.length) return null;
  const unit = `{{ state_attr('${rows[0][1]}', 'unit_of_measurement') }}`;
  const lines = rows.map(
    ([name, id, label]) =>
      `| ${name}${label ? since(id) : ""} | {{ v('${id}', 'km', 0) }} | {{ v('${id}', 'charging_cost', 2) }} | ` +
      `{{ v('${id}', 'driving_cost', 2) }} | {{ states('${id}') | float(0) | round(2) if has_value('${id}') else '-' }} |`,
  );
  return `${ATTR_MACRO}
| | Tracked km | Charged | Driven | ${unit} |
|:--|--:|--:|--:|--:|
${lines.join("\n")}

**Charged** is what the energy bought in the period cost. **Driven** is what the energy used in the period cost, at the average price of the energy in the battery.`;
}

function costByTemperatureMarkdown(o) {
  if (!o.model_cost_per_100km) return null;
  const id = o.model_cost_per_100km;
  let text = `{% set rows = state_attr('${id}', 'by_temperature') or [] %}{% set cols = ['home', 'solar', 'public', 'battery'] | select('in', (rows | first | default({})).keys() | list) | list %}**Model: cost per 100 km by temperature and price** ({{ state_attr('${id}', 'unit_of_measurement') }})

| Outside | kWh/100 km |{% for c in cols %} {{ c | capitalize }} |{% endfor %}
|--:|--:|{% for c in cols %}--:|{% endfor %}
{% for r in rows %}| {{ r.t }} °C | {{ r.kwh100 }} |{% for c in cols %} {{ r[c] }} |{% endfor %}
{% endfor %}`;
  if (o.consumption_by_temperature) {
    text += `
{% set bands = state_attr('${o.consumption_by_temperature}', 'by_temperature') or [] %}{% if bands %}**Measured on trips**

| Outside | km | kWh/100 km | Model | Cost/100 km |
|--:|--:|--:|--:|--:|
{% for r in bands %}| {{ r.t }} to {{ r.t + 5 }} °C | {{ r.km }} | {{ r.kwh100 if r.kwh100 is not none else '-' }} | {{ r.model if r.model is not none else '-' }} | {{ r.cost100 if r.cost100 is not none else '-' }} |
{% endfor %}{% endif %}`;
  }
  return text;
}

const NOTIFY_ROWS = [
  ["charging", "Charging"],
  ["unlocked", "Unlocked"],
  ["open", "Door or window open"],
  ["plug_in_reminder", "Plug-in reminder"],
  ["warning_light", "Warning light"],
  ["data_stale", "No data"],
  ["night_activity", "Night activity"],
  ["weekly_summary", "Weekly summary"],
  ["calibration", "Calibration"],
];

function row(entity, name) {
  return entity ? { entity, name } : null;
}

export function buildView(vehicle) {
  const o = vehicle.ours;
  const f = vehicle.fcq;
  const sections = [
    section("Now", [
      f.soc ? { type: "gauge", entity: f.soc, name: "Battery", min: 0, max: 100, needle: true, severity: { green: 40, yellow: 20, red: 0 } } : null,
      tile(f.elveh, "Range"),
      tile(o.charging_state, "Charging"),
      o.charge_end && o.charging_state
        ? { ...tile(o.charge_end, "Charging ends"), visibility: [{ condition: "state", entity: o.charging_state, state: "charging" }] }
        : null,
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
      tile(o.car_full_range, "Car range at 100 %"),
      tile(o.distance_year, "km this year"),
      tile(o.consumption_year, "kWh/100 km year"),
      tile(o.consumption_by_temperature, "kWh/100 km on trips"),
      tile(o.parked_energy, "Used while parked"),
      tile(o.days_of_range, "Days of range"),
      tile(o.trips, "Trips, 7 days"),
      o.last_trip ? { type: "markdown", content: tripMarkdown(o) } : null,
      o.trips ? { type: "markdown", content: tripStatsMarkdown(o) } : null,
      statistics("km per day", [f.odometer ? { entity: f.odometer, name: "Distance" } : null], "change", "day", 30, "bar"),
      statistics("Energy used per week (kWh)", [o.energy_used ? { entity: o.energy_used, name: "Energy used" } : null], "change", "week", 90, "bar"),
      statistics("Car range at 100 %", [o.car_full_range ? { entity: o.car_full_range, name: "Car" } : null], "mean", "day", 90, "line"),
      history("Battery", [f.soc ? { entity: f.soc, name: "Battery" } : null]),
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
      tile(o.capacity_estimate, "Capacity estimate"),
      history("Range: car vs model", [
        f.elveh ? { entity: f.elveh, name: "Car" } : null,
        o.model_range ? { entity: o.model_range, name: "Model" } : null,
      ]),
      history("SoC: car vs model", [
        f.soc ? { entity: f.soc, name: "Car" } : null,
        o.model_soc ? { entity: o.model_soc, name: "Model" } : null,
      ]),
      o.model_consumption ? { type: "markdown", content: rangeMarkdown(o, f) } : null,
      statistics("Range at 100 %: model vs car", [
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
      tile(o.charging_session, "Charging"),
      tile(o.charging_efficiency, "Charging efficiency"),
      tile(o.charging_sessions, "Sessions"),
      o.charging_power && o.charging_state
        ? { ...tile(o.charging_power, "Charging power"), visibility: [{ condition: "state", entity: o.charging_state, state: "charging" }] }
        : null,
      o.last_charge ? { type: "markdown", content: chargingMarkdown(o) } : null,
      o.last_charge ? { type: "logbook", title: "Charging log", entities: [o.last_charge], hours_to_show: 24 * 30 } : null,
      statistics("Energy charged per month (kWh)", [
        o.charger_energy ? { entity: o.charger_energy, name: "All" } : null,
        o.charger_energy_home ? { entity: o.charger_energy_home, name: "Home" } : null,
      ], "change", "month", 365, "bar"),
    ]),
    section("Costs", [
      tile(o.cost_per_100km_week, "Cost/100 km week"),
      tile(o.cost_per_100km_month, "Cost/100 km month"),
      tile(o.cost_per_100km_year, "Cost/100 km year"),
      tile(o.cost_per_100km_total, "Cost/100 km since start"),
      tile(o.model_cost_per_100km, "Model cost/100 km now"),
      tile(o.model_cost_per_100km_tomorrow, "Model cost/100 km tomorrow"),
      tile(o.battery_energy_price, "Battery energy price"),
      tile(o.charge_to_target_cost, "Cost to charge to target"),
      tile(o.cost_month_projection, "Projected this month"),
      tile(o.cost_year, "Charging cost this year"),
      tile(o.fuel_savings, "Saved vs fuel"),
      tile(o.solar_savings, "Saved by solar"),
      tile(o.co2_savings, "CO2 saved vs fuel"),
      costMarkdown(o) ? { type: "markdown", content: costMarkdown(o) } : null,
      costByTemperatureMarkdown(o) ? { type: "markdown", content: costByTemperatureMarkdown(o) } : null,
      statistics("Charging cost per month", [
        o.charging_cost_home ? { entity: o.charging_cost_home, name: "Home" } : null,
        o.charging_cost_public ? { entity: o.charging_cost_public, name: "Public" } : null,
      ], "change", "month", 365, "bar"),
      statistics("Driving cost per month", [o.driving_cost ? { entity: o.driving_cost, name: "Driving cost" } : null], "change", "month", 365, "bar"),
      statistics("km per month", [f.odometer ? { entity: f.odometer, name: "Distance" } : null], "change", "month", 365, "bar"),
    ]),
    section("Settings", [
      entitiesCard("Settings", [
        row(o.climate_on, "Climate while driving"),
        row(o.climate_setpoint, "Climate setpoint"),
        row(o.charge_target, "Charge target"),
        row(o.plug_in_reminder_soc, "Plug-in reminder below"),
      ]),
      entitiesCard("Notifications", NOTIFY_ROWS.map(([key, name]) => row(o[`notify_${key}`], name))),
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
                "No vehicle found. Install FordConnect Query, then add the **Ford EV** integration " +
                `in Settings > Devices & services. Documentation: ${DOCS}`,
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
