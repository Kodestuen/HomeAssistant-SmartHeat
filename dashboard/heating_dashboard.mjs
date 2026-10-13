// Builds the "Heating" dashboard for SmartHeat zones and saves it to Home Assistant.
//
//   node dashboard/heating_dashboard.mjs --url http://homeassistant.local:8123 --token-file ha.token.txt
//     [--dry-run] [--out config.json] [--force] [--state-file <file>] [--snapshot <file>] [--save-snapshot <file>]
//
// Node 22 or newer, no npm packages. HA_URL and HA_TOKEN can replace --url and --token-file.
// See dashboard/README.md for what the dashboard shows and how a run works.
//
// The config is a pure function of a snapshot: a plain object keyed by WebSocket command,
// each holding that command's result.
//   {
//     "get_states": [...],                    required
//     "config/entity_registry/list": [...],   required (the full list, with unique_id)
//     "config/device_registry/list": [...],   required
//     "config/area_registry/list": [...],     required
//     "config_entries/get": [...],            required
//     "config/floor_registry/list": [...],    optional: floor order and floor headings
//     "manifest/get:smartheat": {...},        optional: manifest/get {"integration": "smartheat"}
//     "config/auth/list": [...]               optional: hides admin-only links from other users
//   }
// Entities are found through the registry (unique_id, platform, config entry), never by
// guessing entity IDs, and only usable entities are referenced.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual, parseArgs } from "node:util";

// ------------------------------------------------------------------ constants

// The fields of lovelace/dashboards/create
export const DASHBOARD = {
  url_path: "dashboard-heating",
  title: "Heating",
  icon: "mdi:radiator",
  show_in_sidebar: true,
  require_admin: false,
  mode: "storage",
};

const BASE = `/${DASHBOARD.url_path}`;
const P_OVERVIEW = "overview";
const P_COMPARE = "compare";
const P_ROOMS = "rooms";
const ADD_ZONE_URL = "/_my_redirect/config_flow_start?domain=smartheat";
const INTEGRATION_URL = "/config/integrations/integration/smartheat";
const DOCS_FALLBACK = "https://github.com/Kodestuen/HomeAssistant-SmartHeat";
const OUTDOOR_TITLE = "Outdoor temperature";
const DEFAULT_HVAC_MODES = ["heat", "off"];
const DEFAULT_PRESET_MODES = ["none", "eco", "away", "frost"];
const PRESET_TILE_LIMIT = 4; // with more zones the overview tiles drop the preset dropdown

const REQUIRED_KEYS = [
  "get_states",
  "config/entity_registry/list",
  "config/device_registry/list",
  "config/area_registry/list",
  "config_entries/get",
];

const SYSTEM = {
  underfloor: {
    icon: "mdi:heating-coil", hours: 48, emitter: "the floor", holds: "The floor holds",
    keeps: "The floor keeps", tooHot: "so the valve stays closed to protect the floor",
  },
  radiator: {
    icon: "mdi:radiator", hours: 24, emitter: "the radiators", holds: "The radiators hold",
    keeps: "The radiators keep", tooHot: "so the valve stays closed",
  },
  unknown: {
    icon: "mdi:radiator", hours: 24, emitter: "the stored heat", holds: "The heating loop holds",
    keeps: "The heating loop keeps", tooHot: "so the valve stays closed",
  },
};
const MODEL_SYSTEMS = { "Underfloor heating zone": "underfloor", "Radiator zone": "radiator" };

// Diagnostic sensor role (unique_id suffix) -> label, icon. Also the row order of
// "What SmartHeat has learned".
const ROLES = [
  ["learned_overshoot", "Learned overshoot", "mdi:chart-bell-curve"],
  ["last_peak_error", "Last peak vs target", "mdi:target"],
  ["holding_difference", "Holding difference", "mdi:thermometer-lines"],
  ["heat_storage_factor", "Heat storage factor", "mdi:heat-wave"],
  ["heat_up_rate", "Heat-up rate", "mdi:thermometer-chevron-up"],
  ["learning_cycles", "Learning cycles", "mdi:counter"],
  ["valve_duty", "Valve open (24 h)", "mdi:valve-open"],
  ["supply_return_difference", "Supply-return difference", "mdi:swap-vertical"],
];

// Tuning attributes (SmartHeat 0.2.0 exposes the option keys verbatim): name, suffix, icon
const TUNING = [
  ["hysteresis", "Hysteresis", "°C", "mdi:arrow-up-down"],
  ["min_open_minutes", "Minimum open time", "min", "mdi:valve-open"],
  ["min_closed_minutes", "Minimum closed time", "min", "mdi:valve-closed"],
  ["coast_window_minutes", "Coast window", "min", "mdi:timer-sand"],
  ["max_return_temp", "Maximum return temperature", "°C", "mdi:thermometer-alert"],
  ["sensor_timeout_minutes", "Room sensor timeout", "min", "mdi:timer-alert-outline"],
  ["eco_temp", "Eco temperature", "°C", "mdi:leaf"],
  ["away_temp", "Away temperature", "°C", "mdi:account-arrow-right"],
  ["frost_temp", "Frost protection temperature", "°C", "mdi:snowflake-thermometer"],
];

// Whole-house buttons: label, icon, color, preset, confirmation
const WHOLE_HOUSE = [
  ["Comfort", "mdi:sofa", "deep-orange", "none", "Set the whole house back to comfort temperature?"],
  ["Eco", "mdi:leaf", "green", "eco", "Set the whole house to Eco?"],
  ["Away", "mdi:account-arrow-right", "blue-grey", "away", "Set the whole house to Away?"],
];

// Every icon this generator emits; all are in MDI 7.4.47, bundled with frontend 20260930.3
const MDI_ICONS = new Set([
  "account-arrow-right", "arrow-up-down", "book-open-variant", "brain", "chart-bell-curve",
  "chart-line", "chart-multiline", "chart-timeline-variant", "cog", "counter", "devices",
  "heat-wave", "heating-coil", "home-group", "home-thermometer", "leaf", "pipe-valve", "plus",
  "radiator", "restore", "scale-balance", "school", "snowflake-thermometer", "sofa",
  "swap-vertical", "target", "thermometer", "thermometer-alert", "thermometer-chevron-up",
  "thermometer-lines", "thermometer-water", "timer-alert-outline", "timer-sand", "tune", "valve",
  "valve-closed", "valve-open", "water-thermometer", "weather-partly-cloudy",
]);

// The climate view strategy (All rooms) shows these covers: device class none or one of
const STRATEGY_COVER_CLASSES = new Set(["awning", "blind", "curtain", "shade", "shutter", "window"]);
const COLLATOR = new Intl.Collator("da", { sensitivity: "base", numeric: true });

// ------------------------------------------------------------------ snapshot

function index(snapshot) {
  for (const key of REQUIRED_KEYS) {
    if (!Array.isArray(snapshot?.[key])) throw new Error(`The snapshot has no "${key}" list.`);
  }
  const entities = snapshot["config/entity_registry/list"];
  const devices = snapshot["config/device_registry/list"];
  const floors = snapshot["config/floor_registry/list"];
  const users = snapshot["config/auth/list"];
  const manifest = snapshot["manifest/get:smartheat"];
  return {
    states: new Map(snapshot["get_states"].map((s) => [s.entity_id, s])),
    entities,
    entityById: new Map(entities.map((e) => [e.entity_id, e])),
    devices,
    deviceById: new Map(devices.map((d) => [d.id, d])),
    areas: snapshot["config/area_registry/list"],
    floors: Array.isArray(floors) ? floors : null,
    entries: snapshot["config_entries/get"],
    manifest: manifest && typeof manifest === "object" ? manifest : null,
    users: Array.isArray(users) ? users : null,
  };
}

// Usable: the entity has a state (any state, unavailable included) and its registry row,
// if it has one, is not disabled.
function usable(S, id) {
  return typeof id === "string" && S.states.has(id) && !S.entityById.get(id)?.disabled_by;
}

const attributes = (S, id) => S.states.get(id)?.attributes ?? {};
const byString = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

function firstNonEmpty(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

// The entity's own area, else its device's
function effectiveArea(S, id) {
  const row = S.entityById.get(id);
  if (!row) return null;
  return row.area_id ?? (row.device_id ? S.deviceById.get(row.device_id)?.area_id ?? null : null);
}

// ------------------------------------------------------------------ discovery

function findWeather(S) {
  const ids = [...S.states.keys()].filter((id) => id.startsWith("weather.") && usable(S, id));
  const rank = (id) => (S.entityById.get(id)?.platform === "met" ? 0 : 1);
  ids.sort((a, b) => rank(a) - rank(b) || byString(a, b));
  if (!ids.length) return null;
  const features = Number(attributes(S, ids[0]).supported_features ?? 0);
  return {
    entity: ids[0],
    daily: (features & 1) !== 0,
    hourly: (features & 2) !== 0,
    twiceDaily: (features & 4) !== 0,
  };
}

// The template helper titled "Outdoor temperature" (see README.md). Its state class must be
// measurement: the holding-difference charts plot its daily mean from long-term statistics,
// and the trend graph needs a unit or a state class.
function findOutdoor(S, warnings) {
  const entry = S.entries
    .filter((e) => e.domain === "template" && !e.disabled_by
      && String(e.title ?? "").trim().toLowerCase() === OUTDOOR_TITLE.toLowerCase())
    .sort((a, b) => byString(a.entry_id, b.entry_id))[0];
  if (!entry) return null;
  const row = S.entities.find((r) => r.platform === "template" && r.config_entry_id === entry.entry_id
    && r.entity_id.startsWith("sensor."));
  if (!row || !usable(S, row.entity_id)) return null;
  if (attributes(S, row.entity_id).state_class !== "measurement") {
    warnings.push(`The "${OUTDOOR_TITLE}" helper is left out because its state class is not Measurement, which the outdoor charts need. Set its state class to Measurement and re-run.`);
    return null;
  }
  return row.entity_id;
}

// SmartHeat's HACS update entity
function findUpdate(S, docsUrl) {
  const candidates = S.entities.filter((r) => r.platform === "hacs" && r.entity_id.startsWith("update.")
    && usable(S, r.entity_id));
  const prefix = `${docsUrl.toLowerCase().replace(/\/+$/, "")}/`;
  let hit = candidates.find((r) => String(attributes(S, r.entity_id).release_url ?? "").toLowerCase().startsWith(prefix));
  // Fallback: the HACS device named SmartHeat. Zone devices never match, as they have no hacs identifier.
  hit ??= candidates.find((r) => {
    const device = r.device_id ? S.deviceById.get(r.device_id) : null;
    return device && (device.name === "SmartHeat" || device.name_by_user === "SmartHeat")
      && (device.identifiers ?? []).some(([domain]) => domain === "hacs");
  });
  return hit?.entity_id ?? null;
}

// Only admins can open /config pages; hide those links when other people use the dashboard
function adminVisibility(S) {
  if (!S.users) return null;
  const people = S.users.filter((u) => u.is_active && !u.system_generated);
  const admins = people.filter((u) => (u.group_ids ?? []).includes("system-admin")).map((u) => u.id).sort(byString);
  if (!admins.length || admins.length === people.length) return null;
  return [{ condition: "user", users: admins }];
}

export function slugify(text) {
  return String(text)
    .toLowerCase()
    .replace(/æ/g, "ae").replace(/ø/g, "o").replace(/ß/g, "ss")
    .normalize("NFKD").replace(/\p{M}/gu, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function discoverZones(S) {
  const zones = [];
  const warnings = [];
  for (const entry of S.entries.filter((e) => e.domain === "smartheat")) {
    const title = firstNonEmpty(entry.title) || entry.entry_id;
    if (entry.disabled_by) {
      warnings.push(`Zone "${title}" is disabled in Home Assistant, so it is left out.`);
      continue;
    }
    const prefix = `${entry.entry_id}_`;
    const roles = {};
    for (const row of S.entities) {
      if (row.platform !== "smartheat" || row.config_entry_id !== entry.entry_id) continue;
      if (typeof row.unique_id !== "string" || !row.unique_id.startsWith(prefix)) continue;
      if (usable(S, row.entity_id)) roles[row.unique_id.slice(prefix.length)] = row.entity_id;
    }
    if (!roles.climate) {
      warnings.push(`Zone "${title}" has no usable climate entity (disabled or not loaded), so it is left out.`);
      continue;
    }
    const a = attributes(S, roles.climate);
    const device = S.devices.find((d) => (d.identifiers ?? []).some(([domain, id]) => domain === "smartheat" && id === entry.entry_id)) ?? null;
    const name = firstNonEmpty(a.friendly_name, device?.name_by_user, device?.name, entry.title);
    const system = [a.system_type, device?.model_id, MODEL_SYSTEMS[device?.model]]
      .find((v) => v === "underfloor" || v === "radiator") ?? "unknown";
    const input = (key) => (usable(S, a[key]) ? a[key] : null); // SmartHeat 0.2.0 attributes
    const zone = {
      entry_id: entry.entry_id,
      device_id: device?.id ?? null,
      roles,
      climate: roles.climate,
      name,
      system,
      valve: input("valve_entity_id"),
      room: input("room_sensor_entity_id"),
      ret: input("return_sensor_entity_id"),
      supply: input("supply_sensor_entity_id"),
      window: input("window_sensor_entity_id"),
      invert: a.invert_valve === true,
      hvacModes: Array.isArray(a.hvac_modes) && a.hvac_modes.length ? a.hvac_modes : DEFAULT_HVAC_MODES,
      presetModes: Array.isArray(a.preset_modes) && a.preset_modes.length ? a.preset_modes : DEFAULT_PRESET_MODES,
      tuning: TUNING.filter(([attr]) => typeof a[attr] === "number"),
      state: S.states.get(roles.climate).state,
    };
    zone.hasSupply = Boolean(zone.supply || "supply_temperature" in a || roles.supply_return_difference);
    // SmartHeat 0.3.0 sets the attribute to null for a radiator zone without a return sensor.
    // SmartHeat 0.1.0 has no such attribute, and always a return sensor.
    zone.hasReturn = a.return_sensor_entity_id !== null;
    if (!zone.hasReturn) zone.tuning = zone.tuning.filter(([attr]) => attr !== "max_return_temp"); // nothing to limit
    zone.area = S.entityById.get(zone.climate)?.area_id ?? device?.area_id ?? (zone.room ? effectiveArea(S, zone.room) : null);
    if (zone.state === "unavailable" || zone.state === "unknown") {
      warnings.push(`Zone "${name}" is ${zone.state}, so its page may lack details. Re-run when the zone responds again.`);
    }
    zones.push(zone);
  }

  const areaRank = new Map(S.areas.map((area, i) => [area.area_id, i]));
  const areaFloor = new Map(S.areas.map((area) => [area.area_id, area.floor_id ?? null]));
  const floorRank = new Map((S.floors ?? []).map((floor, i) => [floor.floor_id, i]));
  for (const zone of zones) {
    if (zone.area !== null && !areaRank.has(zone.area)) zone.area = null;
    zone.floor = S.floors && zone.area !== null ? areaFloor.get(zone.area) : null;
    if (zone.floor !== null && !floorRank.has(zone.floor)) zone.floor = null;
  }
  const last = Number.MAX_SAFE_INTEGER;
  zones.sort((x, y) =>
    (x.floor === null ? last : floorRank.get(x.floor)) - (y.floor === null ? last : floorRank.get(y.floor))
    || (x.area === null ? last : areaRank.get(x.area)) - (y.area === null ? last : areaRank.get(y.area))
    || COLLATOR.compare(x.name, y.name)
    || byString(x.entry_id, y.entry_id));

  const used = new Set();
  for (const zone of zones) {
    let slug = slugify(zone.name) || zone.entry_id.toLowerCase();
    if (used.has(slug)) {
      let n = 2;
      while (used.has(`${slug}-${n}`)) n++;
      slug = `${slug}-${n}`;
    }
    used.add(slug);
    zone.path = `zone-${slug}`;
  }
  return { zones, warnings };
}

// Whether All rooms (the climate view strategy) shows more than the zones
function extraRooms(S, zones) {
  // Like the strategy, an area's temperature or humidity sensor counts only while it has a state
  const sensor = (id) => typeof id === "string" && S.states.has(id);
  if (S.areas.some((area) => sensor(area.temperature_entity_id) || sensor(area.humidity_entity_id))) return true;
  const zoneClimates = new Set(zones.map((z) => z.climate));
  for (const [id, state] of S.states) {
    const row = S.entityById.get(id);
    if (row && (row.entity_category || row.hidden_by || row.disabled_by)) continue;
    const domain = id.split(".")[0];
    const deviceClass = state.attributes?.device_class ?? null;
    const shown = ["climate", "humidifier", "fan", "water_heater"].includes(domain)
      || (domain === "cover" && (deviceClass === null || STRATEGY_COVER_CLASSES.has(deviceClass)))
      || (domain === "binary_sensor" && deviceClass === "window");
    if (shown && !zoneClimates.has(id)) return true;
  }
  return false;
}

// ------------------------------------------------------------------ templates

// Jinja string literals: escape \ and '
const jstr = (s) => `'${String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
const jlist = (ids) => `[${ids.map(jstr).join(", ")}]`;
const jdict = (pairs) => `{${pairs.map(([k, v]) => `${jstr(k)}: ${jstr(v)}`).join(", ")}}`;

// T1: no SmartHeat zone is running
const T1 = "{{ states.climate | selectattr('entity_id', 'in', integration_entities('smartheat')) | list | count == 0 }}";

// T2a: the running zones differ from the ones this config was built for
function T2a(known) {
  const K = jlist(known);
  return `{{ states.climate | selectattr('entity_id', 'in', integration_entities('smartheat')) | rejectattr('entity_id', 'in', ${K}) | list | count > 0 or ${K} | reject('in', states.climate | map(attribute='entity_id') | list) | list | count > 0 }}`;
}

// T2b: the notice text
function T2b(zones) {
  return [
    `{%- set known = ${jlist(zones.map((z) => z.climate))} -%}`,
    `{%- set names = ${jdict(zones.map((z) => [z.climate, z.name]))} -%}`,
    "{%- set present = states.climate | map(attribute='entity_id') | list -%}",
    "{%- set new = states.climate | selectattr('entity_id', 'in', integration_entities('smartheat')) | rejectattr('entity_id', 'in', known) | map(attribute='name') | list -%}",
    "{%- set gone = known | reject('in', present) | list -%}",
    "> [!NOTE]",
    "> **The heating zones have changed.**{% if new %} New: {{ new | join(', ') }}.{% endif %}{% if gone %} Removed: {% for e in gone %}{{ names.get(e, e) }}{{ ', ' if not loop.last else '' }}{% endfor %}.{% endif %} Run the Heating dashboard generator again to update this page.{% if new %} Until then, tap here to see {{ 'the new zone' if new | count == 1 else 'the new zones' }} under All rooms.{% endif %}",
  ].join("\n");
}

// T3: the overview summary (view header)
function T3(known) {
  return [
    `{%- set zones = ${jlist(known)} -%}`,
    "{%- set present = states.climate | map(attribute='entity_id') | list -%}",
    "{%- set ns = namespace(n=0, heating=[], off=0, warn=[], note=[]) -%}",
    "{%- for e in zones if e in present -%}",
    "{%- set ns.n = ns.n + 1 -%}",
    "{%- set n = state_attr(e, 'friendly_name') or e -%}",
    "{%- set s = states(e) -%}",
    "{%- set r = state_attr(e, 'reason') -%}",
    "{%- if s in ['unavailable', 'unknown'] -%}",
    "{%- set ns.warn = ns.warn + [n ~ ' is not responding'] -%}",
    "{%- else -%}",
    "{%- if s == 'off' -%}{%- set ns.off = ns.off + 1 -%}{%- endif -%}",
    "{%- if is_state_attr(e, 'hvac_action', 'heating') -%}{%- set ns.heating = ns.heating + [n] -%}{%- endif -%}",
    "{%- if r == 'room_sensor_lost' -%}{%- set ns.warn = ns.warn + [n ~ ': room sensor lost, valve closed'] -%}",
    "{%- elif r == 'room_sensor_unavailable' -%}{%- set ns.warn = ns.warn + [n ~ ': waiting for the room sensor'] -%}",
    "{%- elif r == 'max_return_temp' -%}{%- set ns.warn = ns.warn + [n ~ ': water too hot, valve closed'] -%}",
    "{%- elif r == 'window_open' -%}{%- set ns.note = ns.note + [n ~ ': window open, heating paused'] -%}",
    "{%- elif r == 'frost_protection' -%}{%- set ns.note = ns.note + [n ~ ': frost protection is heating'] -%}",
    "{%- endif -%}",
    "{%- if state_attr(e, 'no_heat_source') -%}{%- set ns.warn = ns.warn + [n ~ ': no hot water reaching the loop'] -%}{%- endif -%}",
    "{%- endif -%}",
    "{%- endfor -%}",
    "{%- set h = ns.heating -%}",
    "## {% if ns.n == 0 %}No heating zones found{% elif h | count > 3 %}Heating in {{ h | count }} of {{ ns.n }} rooms{% elif h | count > 1 %}Heating in {{ h[:-1] | join(', ') }} and {{ h[-1] }}{% elif h %}Heating in {{ h[0] }}{% elif ns.off == ns.n %}Heating is off{% else %}Not heating right now{% endif %}",
    "",
    "{% if ns.warn -%}",
    "> [!WARNING]",
    "> {{ ns.warn | join(' · ') }}",
    "",
    "{% endif -%}",
    "{% if ns.note -%}",
    "> [!NOTE]",
    "> {{ ns.note | join(' · ') }}",
    "",
    "{% endif -%}",
    "{% if ns.n and not ns.warn and not ns.note %}Everything is running normally.{% endif %}",
  ].join("\n");
}

// T4: the zone status (zone view header)
function T4(z) {
  const w = SYSTEM[z.system];
  const lines = [
    `{%- set e = ${jstr(z.climate)} -%}`,
    "{%- set s = states(e) -%}",
    "{%- set r = state_attr(e, 'reason') -%}",
    "{%- set t = state_attr(e, 'temperature') -%}",
    "{%- set ct = state_attr(e, 'close_threshold') -%}",
    "{%- set rt = state_attr(e, 'return_temperature') -%}",
    "{%- set tt = (t | round(1) ~ ' °C') if t is number else 'the target' -%}",
    "{%- set hy = state_attr(e, 'hysteresis') -%}",
    "{%- set mr = state_attr(e, 'max_return_temp') -%}",
    "{%- set ft = state_attr(e, 'frost_temp') -%}",
    "{% if s in ['unavailable', 'unknown'] %}## Not responding",
    "",
    "SmartHeat can't control this zone right now. Check the zone under Settings at the bottom of this page.",
    "{% elif r is none %}## Starting",
    "",
    "SmartHeat makes its first decision within a minute.",
    "{% elif r == 'heating' %}## Heating",
    "",
    `The valve is open.{% if ct is number %} It closes at **{{ ct | round(1) }} °C** and lets ${w.emitter} carry the room the rest of the way to **{{ tt }}**.{% endif %}`,
    "{% elif r == 'min_open_time' %}## Heating",
    "",
    "The valve is open and stays open a few more minutes, so the actuator can open fully.",
    "{% elif r == 'predictive_close' %}## Closing early",
    "",
    `${w.holds} enough heat to reach **{{ tt }}**, so the valve closes now.`,
    "{% elif r == 'coasting' %}## Coasting",
    "",
    `The valve closed early. ${w.keeps} warming the room towards **{{ tt }}**.`,
    "{% elif r == 'idle' %}## Warm enough",
    "",
    "The room is at or near **{{ tt }}**, so the valve is closed.{% if hy is number and t is number %} It opens again below **{{ (t - hy) | round(1) }} °C**.{% endif %}",
    "{% elif r == 'min_closed_time' %}## Waiting",
    "",
    "The valve stays closed a few more minutes before it may open again.",
    "{% elif r == 'off' %}## Off",
    "",
    "This zone is switched off. Frost protection still opens the valve {{ ('below ' ~ (ft | round(1)) ~ ' °C') if ft is number else 'if the room gets very cold' }}.",
    "{% elif r == 'window_open' %}## Paused: window open",
    "",
    "The valve stays closed while the window or door is open.",
    "{% elif r == 'max_return_temp' %}## Paused: water too hot",
    "",
    `The return water is {{ (rt | round(1) ~ ' °C') if rt is number else 'too hot' }}, above {{ ('the ' ~ (mr | round(1)) ~ ' °C limit') if mr is number else 'the limit' }} for this zone, ${w.tooHot}.`,
    "{% elif r == 'room_sensor_unavailable' %}## Waiting for the room sensor",
    "",
    "There is no reading from the room sensor right now. The valve keeps its position for a while.",
    "{% elif r == 'room_sensor_lost' %}## Room sensor lost",
    "",
    "No reading for too long, so the valve is closed for safety. Check the sensor's battery and connection.",
    "{% elif r == 'frost_protection' %}## Frost protection",
    "",
    "The room is very cold, so the valve is open to protect it.",
    "{% else %}## {{ r }}",
    "{% endif %}",
  ];
  if (z.hasSupply) {
    lines.push(
      "{% if state_attr(e, 'no_heat_source') %}",
      "> [!WARNING]",
      "> No hot water is reaching the loop: the supply pipe is barely warmer than the room. Check that the boiler or heat pump is running.",
      "",
    );
    // The loop saturated note needs both sensors, like its row in T5
    if (z.hasReturn) {
      lines.push(
        "{% endif %}{% if state_attr(e, 'loop_saturated') %}",
        "> [!NOTE]",
        "> Supply and return are almost equally warm: the loop is giving the room all the heat it can.",
        "",
      );
    }
    lines.push("{% endif %}");
  }
  if (z.roles.learning_cycles) {
    lines.push(
      `{%- set cy = states(${jstr(z.roles.learning_cycles)}) -%}`,
      "{% if cy | is_number %}",
      "",
      "{{ 'SmartHeat is still learning this room.' if cy | int == 0 else 'SmartHeat has learned from ' ~ (cy | int) ~ (' heating cycle.' if cy | int == 1 else ' heating cycles.') }}{% endif %}",
    );
  }
  return lines.join("\n");
}

// T5: the controller table
function T5(z) {
  const rows = [
    `{%- set e = ${jstr(z.climate)} -%}`,
    "{%- set ok = states(e) not in ['unavailable', 'unknown'] -%}",
    "{%- set r = state_attr(e, 'reason') -%}",
    "{%- set labels = {'heating': 'Heating', 'idle': 'Warm enough', 'predictive_close': 'Closing early', 'coasting': 'Coasting', 'min_open_time': 'Minimum open time', 'min_closed_time': 'Minimum closed time', 'off': 'Zone off', 'window_open': 'Window open', 'max_return_temp': 'Water too hot', 'room_sensor_unavailable': 'Waiting for room sensor', 'room_sensor_lost': 'Room sensor lost', 'frost_protection': 'Frost protection'} -%}",
    "{%- macro deg(v, p) -%}{{ (v | round(p) ~ ' °C') if v is number else '–' }}{%- endmacro -%}",
    "| Decision | {{ labels.get(r, r) if ok and r else '–' }} |",
    "|:--|--:|",
    "| Valve | {{ ('Open' if is_state_attr(e, 'hvac_action', 'heating') else 'Closed') if ok else '–' }} |",
    "| Target now | {{ deg(state_attr(e, 'temperature'), 1) }} |",
    "| Comfort target | {{ deg(state_attr(e, 'comfort_temperature'), 1) }} |",
    "| Predicted overshoot | {{ deg(state_attr(e, 'predicted_overshoot'), 2) }} |",
    "| Valve closes at | {{ deg(state_attr(e, 'close_threshold'), 2) }} |",
    "| Coasting | {{ ('Yes' if state_attr(e, 'coasting') else 'No') if ok else '–' }} |",
  ];
  if (z.hasReturn) rows.push("| Return water | {{ deg(state_attr(e, 'return_temperature'), 1) }} |");
  // Supply − return and Loop saturated need both sensors
  if (z.hasSupply) {
    rows.push("| Supply water | {{ deg(state_attr(e, 'supply_temperature'), 1) }} |");
    if (z.hasReturn) rows.push("| Supply − return | {{ deg(state_attr(e, 'supply_return_delta'), 1) }} |");
    rows.push("| Hot water arriving | {{ ('No' if state_attr(e, 'no_heat_source') else 'Yes') if ok else '–' }} |");
    if (z.hasReturn) rows.push("| Loop saturated | {{ ('Yes' if state_attr(e, 'loop_saturated') else 'No') if ok else '–' }} |");
  }
  rows.push(
    "{%- set tr = state_attr(e, 'trained') -%}",
    "{%- if ok and tr is not none %}",
    "| Prediction | {{ 'Learned model' if tr else 'Average of past cycles' }} |",
    "{%- endif %}",
    "{%- if ok and r %}",
    "",
    "Reason code: `{{ r }}`",
    "{%- endif %}",
  );
  return rows.join("\n");
}

// S1: the checklist shown while there are no zones
const S1 = [
  "### Before you add a zone",
  "Each zone needs:",
  "- a **valve**: the switch, input boolean or valve entity that lets hot water into the loop or radiators",
  "- a **room temperature sensor** in the room",
  "- for underfloor heating, a **return temperature sensor** on the return pipe at the end of the loop (optional for radiators)",
  "",
  "Optional:",
  "- a **supply temperature sensor** on the flow pipe, to notice when no hot water arrives",
  "- a **window or door sensor**: the valve stays closed while it is open",
  "",
  "Name each zone after its room, for example *Stue*.",
].join("\n");

// S2: the note under the learned values
const S2 = "*Overshoot* is how far the room keeps warming after the valve closes. SmartHeat learns it from every heating cycle and closes the valve that much early. *Last peak vs target* shows how the last cycle landed: close to 0 °C is good.";

// ------------------------------------------------------------------ cards

const withVisibility = (obj, visibility) => (visibility ? { ...obj, visibility } : obj);
const heading = (text, icon, style = "title") => ({ type: "heading", heading: text, heading_style: style, icon });
const navigate = (viewPath) => ({ action: "navigate", navigation_path: `${BASE}/${viewPath}` });
const valveLabel = (z) => (z.invert ? "Valve actuator (on = closed)" : "Valve");

function weatherBadge(weather) {
  return {
    type: "entity", entity: weather.entity, show_name: false, show_state: true, show_icon: true,
    state_content: ["state", "temperature"], tap_action: { action: "more-info" },
  };
}

function windowLabel(S, id) {
  const deviceClass = attributes(S, id).device_class;
  return deviceClass === "window" ? "Window" : deviceClass === "door" ? "Door" : "Window or door";
}

function outsideCards(weather, outdoor) {
  const cards = [];
  if (weather) {
    const tile = {
      type: "tile", entity: weather.entity, name: "Weather",
      state_content: ["state", "temperature", "wind_speed"], grid_options: { columns: 12 },
    };
    if (weather.hourly) {
      tile.features = [{ type: "temperature-forecast", forecast_type: "hourly", hours_to_show: 24, show_labels: true }];
    }
    cards.push(tile);
    // Never "legacy"; without daily or twice-daily forecasts the card is left out
    const forecastType = weather.daily ? "daily" : weather.twiceDaily ? "twice_daily" : null;
    if (forecastType) {
      cards.push({
        type: "weather-forecast", entity: weather.entity, show_current: false, show_forecast: true,
        forecast_type: forecastType, forecast_slots: 5, round_temperature: true, grid_options: { columns: 12 },
      });
    }
  }
  if (outdoor) {
    cards.push({
      type: "tile", entity: outdoor, name: "Outdoor temperature, last 24 h", icon: "mdi:thermometer",
      features: [{ type: "trend-graph", hours_to_show: 24 }], grid_options: { columns: 12 },
    });
  }
  return cards.length ? [heading("Outside", "mdi:weather-partly-cloudy"), ...cards] : [];
}

function smartheatCards(ctx) {
  const cards = [{
    ...heading("SmartHeat", "mdi:radiator"),
    badges: [{ type: "button", icon: "mdi:book-open-variant", text: "Docs", tap_action: { action: "url", url_path: ctx.docsUrl } }],
  }];
  if (ctx.update) {
    // Two tiles: update-actions shows disabled buttons while nothing is pending
    cards.push(
      {
        type: "tile", entity: ctx.update, name: "Software update", state_content: ["install_status", "installed_version"],
        grid_options: { columns: 12 }, visibility: [{ condition: "state", entity: ctx.update, state_not: "on" }],
      },
      {
        type: "tile", entity: ctx.update, name: "Software update", state_content: ["install_status", "latest_version"],
        features: [{ type: "update-actions" }], grid_options: { columns: 12 },
        visibility: [{ condition: "state", entity: ctx.update, state: "on" }],
      },
    );
  }
  if (ctx.hasCompare) {
    cards.push({
      type: "shortcut", label: "Compare zones", description: "Learning and valve use side by side",
      icon: "mdi:chart-multiline", tap_action: navigate(P_COMPARE), grid_options: { columns: 12 },
    });
  }
  if (ctx.zones.length) {
    cards.push(
      withVisibility({
        type: "shortcut", label: "Add a zone", description: "Underfloor loop or radiators", icon: "mdi:plus",
        tap_action: { action: "navigate", navigation_path: ADD_ZONE_URL }, grid_options: { columns: 6 },
      }, ctx.adminVisibility),
      withVisibility({
        type: "shortcut", label: "Settings", description: "All SmartHeat zones", icon: "mdi:cog",
        tap_action: { action: "navigate", navigation_path: INTEGRATION_URL }, grid_options: { columns: 6 },
      }, ctx.adminVisibility),
    );
  }
  return cards.length > 1 ? cards : []; // the heading only when a card follows it
}

function zoneTile(z, withPresets) {
  const features = [{ type: "target-temperature" }];
  if (withPresets) features.push({ type: "climate-preset-modes", style: "dropdown", preset_modes: z.presetModes });
  // No name, so the tile follows renames
  return {
    type: "tile", entity: z.climate, icon: SYSTEM[z.system].icon,
    state_content: ["current_temperature", "hvac_action"],
    features, features_position: "bottom",
    tap_action: navigate(z.path),
    icon_tap_action: { action: "more-info" },
    grid_options: { columns: 12 },
  };
}

function presetShortcut([label, icon, color, preset, text], climates) {
  return {
    type: "shortcut", label, icon, color, vertical: true,
    tap_action: {
      action: "perform-action", perform_action: "climate.set_preset_mode",
      target: { entity_id: climates }, data: { preset_mode: preset }, confirmation: { text },
    },
  };
}

// ------------------------------------------------------------------ views

function overviewView(S, ctx) {
  const { zones } = ctx;
  const known = zones.map((z) => z.climate);
  // The notice is hidden by a template condition, never by markdown show_empty: a markdown
  // card hidden that way is removed from the page and never comes back.
  const sections = [{
    type: "grid", column_span: 3,
    visibility: [{ condition: "template", value_template: T2a(known) }],
    cards: [{
      type: "markdown", text_only: true, content: T2b(zones),
      tap_action: navigate(P_ROOMS), grid_options: { columns: "full" },
    }],
  }];
  if (!zones.length) {
    sections.push({
      type: "grid", column_span: 2,
      visibility: [{ condition: "template", value_template: T1 }],
      cards: [
        {
          type: "empty-state", icon: "mdi:radiator", title: "No heating zones yet",
          content: "SmartHeat runs one heating zone per room, an underfloor loop or a set of radiators, and learns to close the valve early so the room does not overshoot. Add a zone to see and set its temperature here.",
          buttons: [
            { text: "Add a zone", icon: "mdi:plus", appearance: "filled", variant: "brand", tap_action: { action: "navigate", navigation_path: ADD_ZONE_URL } },
            { text: "How SmartHeat works", icon: "mdi:book-open-variant", appearance: "plain", variant: "neutral", tap_action: { action: "url", url_path: ctx.docsUrl } },
          ],
          grid_options: { columns: "full" },
        },
        { type: "markdown", content: S1, grid_options: { columns: "full" } },
      ],
    });
  } else {
    const rooms = heading("Rooms", "mdi:home-thermometer");
    if (ctx.extraRooms) rooms.tap_action = navigate(P_ROOMS);
    const cards = [rooms];
    const withPresets = zones.length <= PRESET_TILE_LIMIT;
    const byFloor = S.floors !== null && new Set(zones.map((z) => z.floor)).size >= 2;
    let floor;
    for (const z of zones) {
      if (byFloor && z.floor !== floor) {
        const name = S.floors.find((f) => f.floor_id === z.floor)?.name ?? "Other rooms";
        cards.push({ type: "heading", heading: name, heading_style: "subtitle" });
        floor = z.floor;
      }
      cards.push(zoneTile(z, withPresets));
    }
    if (zones.length >= 2) {
      cards.push(
        heading("Whole house", "mdi:home-group", "subtitle"),
        {
          type: "grid", columns: 3, square: false, grid_options: { columns: "full" },
          cards: WHOLE_HOUSE.map((button) => presetShortcut(button, known)),
        },
      );
    }
    sections.push({ type: "grid", column_span: 2, cards });
  }
  const side = [...outsideCards(ctx.weather, ctx.outdoor), ...smartheatCards(ctx)];
  if (side.length) sections.push({ type: "grid", column_span: 1, cards: side });

  const view = {
    type: "sections", title: "Heating", path: P_OVERVIEW, icon: "mdi:radiator", max_columns: 3,
    header: { layout: "responsive", badges_position: "bottom", badges_wrap: "wrap" },
  };
  if (zones.length) view.header.card = { type: "markdown", text_only: true, content: T3(known) };
  if (ctx.weather) view.badges = [weatherBadge(ctx.weather)];
  view.sections = sections;
  return view;
}

function zoneView(S, z, ctx) {
  const system = SYSTEM[z.system];
  const sections = [];

  // Z1 control
  sections.push({
    type: "grid", column_span: 1,
    cards: [{
      type: "thermostat", entity: z.climate, show_current_as_primary: true,
      features: [
        { type: "climate-hvac-modes", style: "icons", hvac_modes: z.hvacModes },
        { type: "climate-preset-modes", style: "dropdown", preset_modes: z.presetModes },
      ],
    }],
  });

  // Z2 room vs target; the window is drawn as a timeline on top
  const room = [];
  if (z.window) room.push({ entity: z.window, name: windowLabel(S, z.window) });
  room.push({ entity: z.climate, name: "Room" });
  sections.push({
    type: "grid", column_span: 2,
    cards: [
      heading(`Room vs target, last ${system.hours} h`, "mdi:chart-line"),
      { type: "history-graph", hours_to_show: system.hours, entities: room, grid_options: { columns: "full" } },
    ],
  });

  // Z3 controller
  sections.push({
    type: "grid", column_span: 1,
    cards: [heading("Controller", "mdi:brain"), { type: "markdown", content: T5(z), grid_options: { columns: "full" } }],
  });

  // Z4 sensors and valve (SmartHeat 0.2.0). simple-entity shows the valve without a toggle:
  // SmartHeat would undo a manual change within a minute.
  const inputs = [];
  if (z.valve) {
    const row = { type: "simple-entity", entity: z.valve, name: valveLabel(z), secondary_info: "last_changed" };
    if (!z.valve.startsWith("valve.")) row.icon = "mdi:valve"; // valve entities show their open/closed icon
    inputs.push(row);
  }
  if (z.room) inputs.push({ entity: z.room, name: "Room temperature", icon: "mdi:home-thermometer", secondary_info: "last_updated" });
  if (z.ret) inputs.push({ entity: z.ret, name: "Return water", icon: "mdi:thermometer-water" });
  if (z.supply) inputs.push({ entity: z.supply, name: "Supply water", icon: "mdi:water-thermometer" });
  if (z.window) inputs.push({ entity: z.window, name: windowLabel(S, z.window), secondary_info: "last_changed" });
  if (inputs.length) {
    sections.push({
      type: "grid", column_span: 1,
      cards: [heading("Sensors and valve", "mdi:pipe-valve"), { type: "entities", entities: inputs, grid_options: { columns: "full" } }],
    });
  }

  // Z5 water temperatures (SmartHeat 0.2.0): whichever water sensors are usable, as a radiator
  // zone may have no return sensor and a configured one may be deleted or disabled
  if (z.ret || z.supply) {
    const water = [];
    if (z.valve) water.push({ entity: z.valve, name: valveLabel(z) });
    if (z.ret) water.push({ entity: z.ret, name: "Return" });
    if (z.supply) water.push({ entity: z.supply, name: "Supply" });
    sections.push({
      type: "grid", column_span: 1,
      cards: [
        heading(`Water temperatures, last ${system.hours} h`, "mdi:thermometer-water"),
        { type: "history-graph", hours_to_show: system.hours, entities: water, grid_options: { columns: "full" } },
      ],
    });
  }

  // Z6 what SmartHeat has learned
  const learned = ROLES.filter(([role]) => z.roles[role]).map(([role, name, icon]) => ({ entity: z.roles[role], name, icon }));
  if (learned.length) {
    sections.push({
      type: "grid", column_span: 1,
      cards: [
        heading("What SmartHeat has learned", "mdi:school", "subtitle"),
        { type: "entities", entities: learned, grid_options: { columns: "full" } },
        { type: "markdown", text_only: true, content: S2, grid_options: { columns: "full" } },
      ],
    });
  }

  // Z7 overshoot learning
  const overshoot = [["last_peak_error", "Last peak vs target"], ["learned_overshoot", "Learned overshoot"]]
    .filter(([role]) => z.roles[role]).map(([role, name]) => ({ entity: z.roles[role], name }));
  if (overshoot.length) {
    sections.push({
      type: "grid", column_span: 2,
      cards: [
        heading("Overshoot learning, last 14 days", "mdi:chart-timeline-variant", "subtitle"),
        { type: "history-graph", hours_to_show: 336, entities: overshoot, grid_options: { columns: "full" } },
      ],
    });
  }

  // Z8 valve open per day
  if (z.roles.valve_duty) {
    sections.push({
      type: "grid", column_span: 1,
      cards: [
        heading("Valve open per day", "mdi:valve-open", "subtitle"),
        {
          type: "statistics-graph", chart_type: "bar", period: "day", days_to_show: 30, stat_types: ["mean"],
          min_y_axis: 0, max_y_axis: 100, hide_legend: true,
          entities: [{ entity: z.roles.valve_duty, name: "Valve open" }], grid_options: { columns: "full" },
        },
      ],
    });
  }

  // Z9 holding difference vs outdoor; both in °C, as a statistics graph has one y axis
  if (z.roles.holding_difference && ctx.outdoor) {
    sections.push({
      type: "grid", column_span: 2,
      cards: [
        heading("Holding difference vs outdoor, last 60 days", "mdi:scale-balance", "subtitle"),
        {
          type: "statistics-graph", chart_type: "line", period: "day", days_to_show: 60, stat_types: ["mean"],
          entities: [{ entity: z.roles.holding_difference, name: "Holding difference" }, { entity: ctx.outdoor, name: "Outdoor" }],
          grid_options: { columns: "full" },
        },
      ],
    });
  }

  // Z10 settings. A button row, because the perform-action row has no target; hold_action
  // none, because the default more-info has no entity here.
  const rows = [{
    type: "button", name: "Reset learning", icon: "mdi:restore", action_name: "Reset",
    tap_action: {
      action: "perform-action", perform_action: "smartheat.reset_learning", target: { entity_id: z.climate },
      confirmation: { text: `Forget what SmartHeat has learned about ${z.name}? It starts again from the defaults.` },
    },
    hold_action: { action: "none" },
  }, {
    type: "weblink", name: "Change zone settings", url: `${INTEGRATION_URL}#config_entry=${z.entry_id}`, icon: "mdi:tune",
  }];
  if (z.device_id) rows.push({ type: "weblink", name: "Zone device", url: `/config/devices/device/${z.device_id}`, icon: "mdi:devices" });
  if (z.tuning.length) {
    rows.push({ type: "section", label: "Current tuning" });
    for (const [attribute, name, suffix, icon] of z.tuning) {
      rows.push({ type: "attribute", entity: z.climate, attribute, name, suffix, icon });
    }
  }
  sections.push(withVisibility({
    type: "grid", column_span: 1,
    cards: [heading("Settings", "mdi:cog", "subtitle"), { type: "entities", entities: rows, grid_options: { columns: "full" } }],
  }, ctx.adminVisibility));

  const view = {
    type: "sections", title: z.name, path: z.path, icon: system.icon, subview: true, back_path: `${BASE}/${P_OVERVIEW}`,
    max_columns: 3, dense_section_placement: true,
    header: { layout: "responsive", badges_position: "bottom", badges_wrap: "wrap", card: { type: "markdown", text_only: true, content: T4(z) } },
  };
  if (ctx.weather) view.badges = [weatherBadge(ctx.weather)];
  view.sections = sections;
  return view;
}

function compareView(ctx) {
  const { zones, outdoor } = ctx;
  const series = (role) => zones.filter((z) => z.roles[role]).map((z) => ({ entity: z.roles[role], name: z.name }));
  const sections = [];
  const add = (card) => {
    if (card.entities.length) sections.push({ type: "grid", column_span: 1, cards: [{ ...card, grid_options: { columns: "full" } }] });
  };
  const history = (title, role) => add({ type: "history-graph", title, hours_to_show: 336, entities: series(role) });
  history("Last peak vs target, 14 days", "last_peak_error");
  history("Learned overshoot, 14 days", "learned_overshoot");
  add({
    type: "statistics-graph", title: "Valve open per day, 30 days", chart_type: "bar", period: "day",
    days_to_show: 30, stat_types: ["mean"], min_y_axis: 0, max_y_axis: 100, entities: series("valve_duty"),
  });
  const holding = series("holding_difference");
  if (holding.length) {
    add({
      type: "statistics-graph", title: outdoor ? "Holding difference vs outdoor, 60 days" : "Holding difference, 60 days",
      chart_type: "line", period: "day", days_to_show: 60, stat_types: ["mean"],
      entities: outdoor ? [...holding, { entity: outdoor, name: "Outdoor" }] : holding,
    });
  }
  history("Heat-up rate, 14 days", "heat_up_rate");
  history("Heat storage factor, 14 days", "heat_storage_factor");
  if (!sections.length) return null;
  const view = {
    type: "sections", title: "Compare zones", path: P_COMPARE, icon: "mdi:chart-multiline", subview: true,
    back_path: `${BASE}/${P_OVERVIEW}`, max_columns: 2,
  };
  if (ctx.weather) view.badges = [weatherBadge(ctx.weather)];
  view.sections = sections;
  return view;
}

// The built-in climate view strategy: it regenerates itself when registries change
function roomsView() {
  return {
    title: "All rooms", path: P_ROOMS, icon: "mdi:home-thermometer", subview: true,
    back_path: `${BASE}/${P_OVERVIEW}`, strategy: { type: "climate" },
  };
}

/**
 * Build the dashboard config from a snapshot. Pure and deterministic.
 * Returns { config, warnings, zones }, where zones summarises each zone for the CLI.
 */
export function buildDashboard(snapshot) {
  const S = index(snapshot);
  const documentation = S.manifest?.documentation;
  const docsUrl = typeof documentation === "string" && documentation ? documentation : DOCS_FALLBACK;
  const { zones, warnings } = discoverZones(S);
  const ctx = {
    zones,
    weather: findWeather(S),
    outdoor: findOutdoor(S, warnings),
    update: findUpdate(S, docsUrl),
    docsUrl,
    adminVisibility: adminVisibility(S),
    extraRooms: extraRooms(S, zones),
  };
  const compare = zones.length >= 2 ? compareView(ctx) : null;
  ctx.hasCompare = compare !== null;
  const views = [overviewView(S, ctx), ...zones.map((z) => zoneView(S, z, ctx))];
  if (compare) views.push(compare);
  views.push(roomsView());

  const areaName = (id) => S.areas.find((a) => a.area_id === id)?.name ?? null;
  const summaries = zones.map((z) => ({
    name: z.name,
    entry_id: z.entry_id,
    climate: z.climate,
    path: z.path,
    system: z.system,
    state: z.state,
    area: z.area === null ? null : areaName(z.area),
    learned: ROLES.filter(([role]) => z.roles[role]).length,
    inputs: [z.valve, z.room, z.ret, z.supply, z.window].filter(Boolean).length,
    tuning: z.tuning.length,
  }));
  return { config: { title: DASHBOARD.title, views }, warnings, zones: summaries };
}

// ------------------------------------------------------------------ checks

// Types and keys this generator uses (spec §12.3). Every card may also have type,
// grid_options and visibility.
const CARD_KEYS = {
  grid: ["columns", "square", "cards"],
  heading: ["heading", "heading_style", "icon", "tap_action", "badges"],
  tile: ["entity", "name", "icon", "state_content", "features", "features_position", "tap_action", "icon_tap_action"],
  thermostat: ["entity", "show_current_as_primary", "features"],
  "history-graph": ["title", "hours_to_show", "entities"],
  "statistics-graph": ["title", "chart_type", "period", "days_to_show", "stat_types", "min_y_axis", "max_y_axis", "hide_legend", "entities"],
  entities: ["entities"],
  markdown: ["text_only", "content", "tap_action"],
  "weather-forecast": ["entity", "show_current", "show_forecast", "forecast_type", "forecast_slots", "round_temperature"],
  "empty-state": ["icon", "title", "content", "buttons"],
  shortcut: ["label", "description", "icon", "color", "vertical", "tap_action"],
};
const ROW_KEYS = {
  entity: ["entity", "name", "icon", "secondary_info"],
  "simple-entity": ["type", "entity", "name", "icon", "secondary_info"],
  attribute: ["type", "entity", "attribute", "name", "suffix", "icon"],
  button: ["type", "name", "icon", "action_name", "tap_action", "hold_action"],
  weblink: ["type", "name", "url", "icon"],
  section: ["type", "label"],
};
const FEATURE_KEYS = {
  "target-temperature": [],
  "climate-preset-modes": ["style", "preset_modes"],
  "climate-hvac-modes": ["style", "hvac_modes"],
  "temperature-forecast": ["forecast_type", "hours_to_show", "show_labels"],
  "trend-graph": ["hours_to_show"],
  "update-actions": [],
};
const CONDITION_KEYS = { template: ["value_template"], state: ["entity", "state", "state_not"], user: ["users"] };
const ACTION_KEYS = {
  "more-info": [],
  navigate: ["navigation_path"],
  url: ["url_path"],
  "perform-action": ["perform_action", "target", "data", "confirmation"],
  none: [],
};
const VIEW_KEYS = ["type", "title", "path", "icon", "subview", "back_path", "max_columns", "dense_section_placement", "header", "badges", "sections"];
const STRATEGY_VIEW_KEYS = ["title", "path", "icon", "subview", "back_path", "strategy"];
const HEADER_KEYS = ["layout", "badges_position", "badges_wrap", "card"];
const BADGE_KEYS = ["type", "entity", "show_name", "show_state", "show_icon", "state_content", "tap_action"];
const SECTION_KEYS = ["type", "column_span", "visibility", "cards"];
const PATH_RE = /^[a-zA-Z0-9_-]+$/;
// Links that leave the dashboard; the config/integration pages and the add-zone redirect
const OUTSIDE_LINKS = ["/config/", "/_my_redirect/"];

/**
 * Check a config against the snapshot it was built from: spec §12 checks 1-6 and 8.
 * Returns a list of problems; an empty list means the config is fine to save.
 */
export function checkConfig(config, snapshot) {
  const S = index(snapshot);
  const problems = [];
  const bad = (where, message) => problems.push(`${where}: ${message}`);
  const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const list = (v) => (Array.isArray(v) ? v : []);

  // 3: only known types and keys
  const keys = (obj, allowed, where) => {
    if (!isObject(obj)) {
      bad(where, "is not an object");
      return false;
    }
    const extra = Object.keys(obj).filter((k) => !allowed.includes(k));
    if (extra.length) bad(where, `unexpected ${extra.length > 1 ? "keys" : "key"} ${extra.join(", ")}`);
    return true;
  };
  // 1: every referenced entity is usable
  const entity = (id, where) => {
    if (!usable(S, id)) bad(where, `entity ${JSON.stringify(id)} is not usable (no state, or disabled)`);
  };
  // 5: icons from the bundled MDI set
  const icon = (value, where) => {
    if (value === undefined) return;
    if (typeof value !== "string" || !value.startsWith("mdi:") || !MDI_ICONS.has(value.slice(4))) {
      bad(where, `icon ${JSON.stringify(value)} is not a known MDI 7.4.47 icon`);
    }
  };

  // 6: view paths
  const views = list(config?.views);
  if (keys(config, ["title", "views"], "config") && config.title !== DASHBOARD.title) bad("config", `title is not "${DASHBOARD.title}"`);
  if (!views.length) bad("config", "has no views");
  const paths = new Set();
  views.forEach((view, i) => {
    const p = view?.path;
    if (typeof p !== "string" || !PATH_RE.test(p) || /^\d+$/.test(p)) bad(`views[${i}]`, `bad path ${JSON.stringify(p)}`);
    else if (paths.has(p)) bad(`views[${i}]`, `duplicate path ${p}`);
    else paths.add(p);
  });
  // 6 and 8: links stay inside this dashboard and point at existing views
  const link = (target, where) => {
    if (typeof target !== "string" || !target.startsWith("/")) return bad(where, `bad link ${JSON.stringify(target)}`);
    if (target.startsWith(`${BASE}/`)) {
      const viewPath = target.slice(BASE.length + 1).split(/[?#]/)[0];
      if (!paths.has(viewPath)) bad(where, `link to a missing view ${target}`);
    } else if (!OUTSIDE_LINKS.some((prefix) => target.startsWith(prefix))) {
      bad(where, `link ${target} points outside ${BASE}`);
    }
  };

  const action = (act, where) => {
    if (act === undefined) return;
    if (!keys(act, ["action", ...(ACTION_KEYS[act?.action] ?? [])], where)) return;
    if (!(act.action in ACTION_KEYS)) return bad(where, `unknown action ${JSON.stringify(act.action)}`);
    if (act.action === "navigate") link(act.navigation_path, where);
    if (act.action === "url" && !/^https?:\/\//.test(String(act.url_path))) bad(where, `bad url ${JSON.stringify(act.url_path)}`);
    if (act.action === "perform-action") {
      if (act.target !== undefined && keys(act.target, ["entity_id"], `${where}.target`)) {
        const ids = Array.isArray(act.target.entity_id) ? act.target.entity_id : [act.target.entity_id];
        if (!ids.length) bad(`${where}.target`, "has no entities");
        ids.forEach((id) => entity(id, `${where}.target`));
      }
      if (act.confirmation !== undefined) keys(act.confirmation, ["text"], `${where}.confirmation`);
    }
  };

  const conditions = (conds, where) => {
    if (!Array.isArray(conds) || !conds.length) return bad(where, "visibility is not a list of conditions");
    conds.forEach((c, i) => {
      const w = `${where}[${i}]`;
      if (!keys(c, ["condition", ...(CONDITION_KEYS[c?.condition] ?? [])], w)) return;
      if (!(c.condition in CONDITION_KEYS)) return bad(w, `unknown condition ${JSON.stringify(c.condition)}`);
      if (c.condition === "template" && !(typeof c.value_template === "string" && c.value_template)) bad(w, "has no value_template");
      if (c.condition === "state") entity(c.entity, w);
      if (c.condition === "user" && !(Array.isArray(c.users) && c.users.length)) bad(w, "has no users");
    });
  };

  const features = (card, where) => {
    list(card.features).forEach((f, i) => {
      const w = `${where}.features[${i}]`;
      if (!keys(f, ["type", ...(FEATURE_KEYS[f?.type] ?? [])], w)) return;
      if (!(f.type in FEATURE_KEYS)) return bad(w, `unknown feature ${JSON.stringify(f.type)}`);
      // 4: trend-graph needs a sensor with a unit or a state class
      if (f.type === "trend-graph") {
        const a = attributes(S, card.entity);
        if (!String(card.entity).startsWith("sensor.") || !(a.unit_of_measurement || a.state_class)) {
          bad(w, "trend-graph needs a sensor with a unit or state class");
        }
      }
    });
  };

  const row = (r, where) => {
    const type = r?.type ?? "entity";
    if (!keys(r, ROW_KEYS[type] ?? ["type"], where)) return;
    if (!(type in ROW_KEYS)) return bad(where, `unknown row type ${JSON.stringify(type)}`);
    if (["entity", "simple-entity", "attribute"].includes(type)) entity(r.entity, where);
    if (type === "weblink") link(r.url, where);
    icon(r.icon, where);
    action(r.tap_action, `${where}.tap_action`);
    action(r.hold_action, `${where}.hold_action`);
  };

  const card = (c, where) => {
    if (!keys(c, ["type", "grid_options", "visibility", ...(CARD_KEYS[c?.type] ?? [])], where)) return;
    if (!(c.type in CARD_KEYS)) return bad(where, `unknown card type ${JSON.stringify(c.type)}`);
    if (c.grid_options !== undefined && keys(c.grid_options, ["columns"], `${where}.grid_options`)) {
      const columns = c.grid_options.columns;
      if (!(columns === "full" || (Number.isInteger(columns) && columns >= 1 && columns <= 12))) bad(`${where}.grid_options`, `bad columns ${JSON.stringify(columns)}`);
    }
    if (c.visibility !== undefined) conditions(c.visibility, `${where}.visibility`);
    if (["tile", "thermostat", "weather-forecast"].includes(c.type)) entity(c.entity, where);
    // 2: thermostats need a climate entity; graphs need entities
    if (c.type === "thermostat" && !String(c.entity).startsWith("climate.")) bad(where, "thermostat needs a climate entity");
    if (c.type === "history-graph" || c.type === "statistics-graph") {
      if (!Array.isArray(c.entities) || !c.entities.length) bad(where, `${c.type} has no entities`);
      list(c.entities).forEach((e, i) => {
        if (keys(e, ["entity", "name"], `${where}.entities[${i}]`)) entity(e.entity, `${where}.entities[${i}]`);
      });
    }
    if (c.type === "entities") {
      if (!Array.isArray(c.entities) || !c.entities.length) bad(where, "entities card has no rows");
      list(c.entities).forEach((r, i) => row(r, `${where}.entities[${i}]`));
    }
    if (c.type === "markdown" && !(typeof c.content === "string" && c.content)) bad(where, "markdown has no content");
    if (c.type === "grid") list(c.cards).forEach((cc, i) => card(cc, `${where}.cards[${i}]`));
    icon(c.icon, where);
    features(c, where);
    action(c.tap_action, `${where}.tap_action`);
    action(c.icon_tap_action, `${where}.icon_tap_action`);
    list(c.buttons).forEach((b, i) => {
      const w = `${where}.buttons[${i}]`;
      if (!keys(b, ["text", "icon", "appearance", "variant", "tap_action"], w)) return;
      icon(b.icon, w);
      action(b.tap_action, `${w}.tap_action`);
    });
    list(c.badges).forEach((b, i) => {
      const w = `${where}.badges[${i}]`;
      if (!keys(b, ["type", "icon", "text", "tap_action"], w)) return;
      if (b.type !== "button") bad(w, `unknown heading badge ${JSON.stringify(b.type)}`);
      icon(b.icon, w);
      action(b.tap_action, `${w}.tap_action`);
    });
  };

  views.forEach((view, i) => {
    const where = `views[${i}] ${view?.path ?? ""}`.trim();
    if (isObject(view) && "strategy" in view) {
      keys(view, STRATEGY_VIEW_KEYS, where);
      if (!isDeepStrictEqual(view.strategy, { type: "climate" })) bad(where, "unexpected strategy");
    } else if (keys(view, VIEW_KEYS, where) && view.type !== "sections") {
      bad(where, "is not a sections view");
    }
    if (!isObject(view)) return;
    icon(view.icon, where);
    if (view.back_path !== undefined) link(view.back_path, `${where}.back_path`);
    if (view.subview && view.back_path === undefined) bad(where, "subview without back_path");
    if (view.header !== undefined && keys(view.header, HEADER_KEYS, `${where}.header`) && view.header.card !== undefined) {
      card(view.header.card, `${where}.header.card`);
    }
    list(view.badges).forEach((b, j) => {
      const w = `${where}.badges[${j}]`;
      if (!keys(b, BADGE_KEYS, w)) return;
      if (b.type !== "entity") bad(w, `unknown badge type ${JSON.stringify(b.type)}`);
      entity(b.entity, w);
      action(b.tap_action, `${w}.tap_action`);
    });
    list(view.sections).forEach((section, j) => {
      const w = `${where}.sections[${j}]`;
      if (!keys(section, SECTION_KEYS, w)) return;
      if (section.type !== "grid") bad(w, "is not a grid section");
      const span = section.column_span;
      if (!(Number.isInteger(span) && span >= 1 && span <= (view.max_columns ?? 4))) bad(w, `bad column_span ${JSON.stringify(span)}`);
      if (section.visibility !== undefined) conditions(section.visibility, `${w}.visibility`);
      if (!Array.isArray(section.cards) || !section.cards.length) bad(w, "has no cards");
      list(section.cards).forEach((c, k) => card(c, `${w}.cards[${k}]`));
    });
  });
  return problems;
}

// The Jinja templates in a config (markdown content and template conditions), each with
// where it is first used
export function collectTemplates(config) {
  const found = new Map();
  const walk = (node, where) => {
    if (Array.isArray(node)) {
      node.forEach((child, i) => walk(child, `${where}[${i}]`));
    } else if (node && typeof node === "object") {
      if (node.type === "markdown" && typeof node.content === "string" && !found.has(node.content)) found.set(node.content, `${where}.content`);
      if (node.condition === "template" && typeof node.value_template === "string" && !found.has(node.value_template)) found.set(node.value_template, `${where}.value_template`);
      for (const [key, child] of Object.entries(node)) walk(child, where ? `${where}.${key}` : key);
    }
  };
  (config?.views ?? []).forEach((view, i) => walk(view, view?.path ?? `views[${i}]`));
  return [...found].map(([template, where]) => ({ template, where }));
}

// ------------------------------------------------------------------ Home Assistant

const TIMEOUT_MS = 30_000;
const OPTIONAL_COMMANDS = {
  "config/floor_registry/list": { type: "config/floor_registry/list" },
  "manifest/get:smartheat": { type: "manifest/get", integration: "smartheat" },
  "config/auth/list": { type: "config/auth/list" },
};

class CommandError extends Error {
  constructor(type, error) {
    super(`${type} failed: ${error?.message ?? "unknown error"}${error?.code ? ` (${error.code})` : ""}`);
    this.code = error?.code;
  }
}

function websocketUrl(base) {
  let url;
  try {
    url = new URL(base);
  } catch {
    throw new Error(`Not a valid URL: ${base}`);
  }
  if (url.protocol === "http:" || url.protocol === "https:") url.protocol = url.protocol === "http:" ? "ws:" : "wss:";
  else if (url.protocol !== "ws:" && url.protocol !== "wss:") throw new Error(`The URL must start with http:// or https://: ${base}`);
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/api/websocket`;
  url.search = "";
  url.hash = "";
  return url.href;
}

const messageText = (data) => (typeof data === "string" ? data : Buffer.from(data).toString("utf8"));

// Node's WebSocket usually gives no reason for a failed connection
function eventError(event) {
  return firstNonEmpty(event?.error?.cause?.message, event?.error?.message, event?.message);
}

// A WebSocket connection to Home Assistant: auth, then commands with incrementing ids
class HomeAssistant {
  #ws;
  #nextId = 1;
  #handlers = new Map(); // id -> { onMessage, onClose }
  version = null;
  timeoutMs = TIMEOUT_MS;

  static async connect(baseUrl, token, timeoutMs = TIMEOUT_MS) {
    const url = websocketUrl(baseUrl);
    const ha = new HomeAssistant();
    ha.timeoutMs = timeoutMs;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    ha.#ws = ws;
    // The auth flow: auth_required, then our auth message, then auth_ok or auth_invalid
    await new Promise((resolve, reject) => {
      const stop = () => {
        clearTimeout(timer);
        ws.removeEventListener("message", onMessage);
        ws.removeEventListener("error", onError);
        ws.removeEventListener("close", onClose);
      };
      const fail = (message) => {
        stop();
        reject(new Error(message));
        try {
          ws.close();
        } catch {
          // already closed
        }
      };
      const onMessage = (event) => {
        let msg;
        try {
          msg = JSON.parse(messageText(event.data));
        } catch {
          return fail(`${url} did not answer like Home Assistant.`);
        }
        if (msg.type === "auth_required") {
          ws.send(JSON.stringify({ type: "auth", access_token: token }));
        } else if (msg.type === "auth_ok") {
          stop();
          ha.version = msg.ha_version ?? null;
          resolve();
        } else if (msg.type === "auth_invalid") {
          fail(`Home Assistant refused the token: ${msg.message ?? "invalid access token"}.`);
        }
      };
      const onError = (event) => {
        const reason = eventError(event);
        fail(`Cannot connect to ${url}${reason ? ` (${reason})` : ""}. Check the address and that Home Assistant is running.`);
      };
      const onClose = (event) => fail(`${url} closed the connection (code ${event.code}).`);
      const timer = setTimeout(() => fail(`No answer from ${url} within ${timeoutMs / 1000} s.`), timeoutMs);
      ws.addEventListener("message", onMessage);
      ws.addEventListener("error", onError);
      ws.addEventListener("close", onClose);
    });
    ws.addEventListener("message", (event) => ha.#dispatch(event));
    ws.addEventListener("close", () => {
      for (const handler of [...ha.#handlers.values()]) handler.onClose();
    });
    return ha;
  }

  #dispatch(event) {
    let data;
    try {
      data = JSON.parse(messageText(event.data));
    } catch {
      return;
    }
    for (const msg of Array.isArray(data) ? data : [data]) this.#handlers.get(msg?.id)?.onMessage(msg);
  }

  #register(id, onMessage, onClose) {
    this.#handlers.set(id, { onMessage, onClose });
    return () => this.#handlers.delete(id);
  }

  #send(msg) {
    if (this.#ws.readyState !== WebSocket.OPEN) throw new Error("The connection to Home Assistant is closed.");
    this.#ws.send(JSON.stringify(msg));
  }

  // Send a command and resolve with its result; reject with a CommandError on an error reply
  call(type, params = {}) {
    return new Promise((resolve, reject) => {
      const id = this.#nextId++;
      const done = () => {
        clearTimeout(timer);
        unregister();
      };
      const timer = setTimeout(() => {
        done();
        reject(new Error(`${type}: no answer within ${this.timeoutMs / 1000} s.`));
      }, this.timeoutMs);
      const unregister = this.#register(id, (msg) => {
        if (msg.type !== "result") return;
        done();
        if (msg.success) resolve(msg.result);
        else reject(new CommandError(type, msg.error));
      }, () => {
        done();
        reject(new Error(`${type}: the connection closed.`));
      });
      try {
        this.#send({ id, type, ...params });
      } catch (err) {
        done();
        reject(err);
      }
    });
  }

  // Render a template once through a render_template subscription (strict, reporting
  // errors), then unsubscribe. Resolves with { result } or { error }.
  renderTemplate(template) {
    return new Promise((resolve) => {
      const id = this.#nextId++;
      const errors = [];
      let subscribed = false;
      let finished = false;
      const finish = async (outcome) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        unregister();
        if (subscribed) await this.call("unsubscribe_events", { subscription: id }).catch(() => {});
        resolve(outcome);
      };
      const timer = setTimeout(() => finish({ error: `no result within ${this.timeoutMs / 1000} s` }), this.timeoutMs);
      const unregister = this.#register(id, (msg) => {
        if (msg.type === "result") {
          if (!msg.success) return finish({ error: msg.error?.message ?? "render_template failed" });
          subscribed = true;
          // With report_errors, a failing first render is reported before the result
          if (errors.length) finish({ error: errors.join("; ") });
        } else if (msg.type === "event") {
          const event = msg.event ?? {};
          if ("error" in event) {
            errors.push(`${event.level ?? "ERROR"}: ${event.error}`);
            if (subscribed) finish({ error: errors.join("; ") });
          } else if ("result" in event) {
            finish(errors.length ? { error: errors.join("; ") } : { result: event.result });
          }
        }
      }, () => finish({ error: "the connection closed" }));
      try {
        this.#send({ id, type: "render_template", template, strict: true, report_errors: true });
      } catch (err) {
        finish({ error: err.message });
      }
    });
  }

  close() {
    try {
      this.#ws.close();
    } catch {
      // already closed
    }
  }
}

async function fetchSnapshot(ha) {
  const snapshot = {};
  for (const type of REQUIRED_KEYS) snapshot[type] = await ha.call(type);
  for (const [key, { type, ...params }] of Object.entries(OPTIONAL_COMMANDS)) {
    try {
      snapshot[key] = await ha.call(type, params);
    } catch (err) {
      if (!(err instanceof CommandError)) throw err; // an error reply means "not available"
    }
  }
  return snapshot;
}

async function renderTemplates(ha, config) {
  const errors = [];
  const templates = collectTemplates(config);
  for (const { template, where } of templates) {
    const { error } = await ha.renderTemplate(template);
    if (error !== undefined) errors.push(`${where}: ${error}`);
  }
  return { count: templates.length, errors };
}

// The edit guard: compare what Home Assistant holds with the new config and the last run
function guardState(stored, config, last) {
  if (stored === null) return "empty";
  if (isDeepStrictEqual(stored, config)) return "unchanged";
  if (last !== null && isDeepStrictEqual(stored, last)) return "last-run";
  return "edited";
}

// The only save message this tool sends: url_path is always set, or HA would overwrite
// the default Overview dashboard
export function saveMessage(config) {
  return { type: "lovelace/config/save", url_path: DASHBOARD.url_path, config };
}

// ------------------------------------------------------------------ command line

const USAGE = `Build the SmartHeat "Heating" dashboard and save it to Home Assistant.

  node dashboard/heating_dashboard.mjs --url http://homeassistant.local:8123 --token-file ha.token.txt
    [--dry-run] [--out config.json] [--force] [--state-file <file>] [--snapshot <file>] [--save-snapshot <file>]

  --url <url>              Home Assistant's address (or set HA_URL)
  --token-file <file>      file holding an admin long-lived access token (or set HA_TOKEN)
  --dry-run                build and check the config and print it; save nothing
  --out <file>             write the config to this file
  --force                  save even if the dashboard was edited in the UI
                           (a failed check or template always stops the run)
  --state-file <file>      the last saved config, for the edit guard
                           (default dashboard/heating-dashboard.last.json)
  --snapshot <file>        build offline from a saved snapshot; saves nothing
  --save-snapshot <file>   also write the fetched snapshot to this file, for debugging
  -h, --help               show this help
`;

const DEFAULT_STATE_FILE = fileURLToPath(new URL("heating-dashboard.last.json", import.meta.url));
const OPTIONS = {
  url: { type: "string" },
  "token-file": { type: "string" },
  "dry-run": { type: "boolean" },
  out: { type: "string" },
  force: { type: "boolean" },
  "state-file": { type: "string" },
  snapshot: { type: "string" },
  "save-snapshot": { type: "string" },
  help: { type: "boolean", short: "h" },
};

const log = (line = "") => process.stderr.write(`${line}\n`);
// Relative to the current folder when the file is inside it, else absolute
function shortPath(file) {
  const relative = path.relative(process.cwd(), file);
  return relative && !relative.startsWith("..") && !path.isAbsolute(relative) ? relative : path.resolve(file);
}
const readJson = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
const writeJson = (file, value) => fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);

function readState(file) {
  if (!fs.existsSync(file)) return null;
  try {
    return readJson(file);
  } catch (err) {
    log(`Warning: cannot read ${shortPath(file)} (${err.message}); treating it as missing.`);
    return null;
  }
}

function readToken(file) {
  let token;
  try {
    token = fs.readFileSync(file, "utf8").trim();
  } catch (err) {
    throw new Error(`Cannot read the token file ${file}: ${err.code ?? err.message}.`);
  }
  if (!token) throw new Error(`The token file ${file} is empty.`);
  return token;
}

function reportBuild({ config, warnings, zones }) {
  if (!zones.length) log("Found no SmartHeat zones: the overview shows how to add one.");
  else log(`Found ${zones.length} SmartHeat ${zones.length === 1 ? "zone" : "zones"}:`);
  for (const z of zones) {
    const details = [z.system, z.area ? `area ${z.area}` : "no area"];
    if (z.state === "unavailable" || z.state === "unknown") details.push(z.state);
    log(`  ${z.name} (${z.climate}): ${details.join(", ")} -> ${BASE}/${z.path}`);
  }
  for (const warning of warnings) log(`Warning: ${warning}`);
  log(`Views: ${config.views.map((v) => v.path).join(", ")}`);
}

function reportProblems(title, problems) {
  if (!problems.length) return;
  log(`${title}:`);
  for (const problem of problems) log(`  ${problem}`);
}

function writeConfig(config, out) {
  if (out) {
    writeJson(out, config);
    log(`Wrote the config to ${shortPath(out)}.`);
  } else {
    process.stdout.write(`${JSON.stringify(config, null, 2)}\n`);
  }
}

async function runOffline(opts) {
  const snapshot = readJson(opts.snapshot);
  const built = buildDashboard(snapshot);
  reportBuild(built);
  const problems = checkConfig(built.config, snapshot);
  reportProblems("Check problems", problems);
  log(problems.length ? "The checks failed." : "The checks passed. Templates are only rendered when connected.");
  writeConfig(built.config, opts.out);
  log("Offline run: nothing was saved.");
  return problems.length ? 1 : 0;
}

async function runOnline(opts, env) {
  const url = opts.url ?? env.HA_URL;
  const tokenFile = opts["token-file"];
  if (!url || !(tokenFile || env.HA_TOKEN?.trim())) {
    log(`Give Home Assistant's address and a token: --url and --token-file, or HA_URL and HA_TOKEN.\n\n${USAGE}`);
    return 2;
  }
  // A bad address or token file is a command-line error, found before connecting
  let token;
  try {
    websocketUrl(url);
    token = tokenFile ? readToken(tokenFile) : env.HA_TOKEN.trim();
  } catch (err) {
    log(`${err.message}\n\n${USAGE}`);
    return 2;
  }
  const stateFile = opts["state-file"] ?? DEFAULT_STATE_FILE;
  const dryRun = Boolean(opts["dry-run"]);
  const ha = await HomeAssistant.connect(url, token);
  try {
    log(`Connected to Home Assistant${ha.version ? ` ${ha.version}` : ""}.`);
    const snapshot = await fetchSnapshot(ha);
    if (opts["save-snapshot"]) {
      writeJson(opts["save-snapshot"], snapshot);
      log(`Wrote the snapshot to ${shortPath(opts["save-snapshot"])}. It holds your Home Assistant data; keep it private.`);
    }
    const built = buildDashboard(snapshot);
    const { config } = built;
    reportBuild(built);
    const problems = checkConfig(config, snapshot);
    reportProblems("Check problems", problems);
    const rendered = await renderTemplates(ha, config);
    reportProblems("Template errors", rendered.errors);
    const failed = problems.length > 0 || rendered.errors.length > 0;
    if (!failed) log(`The checks passed and ${rendered.count} templates rendered without errors.`);
    if (opts.out || dryRun) writeConfig(config, opts.out);

    // A failed check or template stops every run; --force only skips the edit guard
    if (failed) {
      if (dryRun) log("Dry run: a real run would stop because of the problems above.");
      log("Nothing was saved. Fix the problems above and run again.");
      return 1;
    }
    const dashboards = await ha.call("lovelace/dashboards/list");
    const existing = (dashboards ?? []).find((d) => d.url_path === DASHBOARD.url_path);
    if (existing && existing.mode !== "storage") {
      log(`${BASE} is a ${existing.mode} dashboard; this tool only writes dashboards stored by the UI.`);
      return 1;
    }
    const last = readState(stateFile);
    let stored = null;
    if (existing) {
      try {
        stored = await ha.call("lovelace/config", { url_path: DASHBOARD.url_path });
      } catch (err) {
        if (err.code !== "config_not_found") throw err; // nothing saved yet
      }
    }
    const guard = guardState(stored, config, last);
    const edited = last === null
      ? `${BASE} already has a config, and ${shortPath(stateFile)} (what the last run saved) is missing, so it may hold edits made in the UI.`
      : `${BASE} was edited in the UI since the last run.`;

    if (dryRun) {
      if (!existing) log(`Dry run: a real run would create ${BASE} and save this config.`);
      else if (guard === "unchanged") log(`Dry run: ${BASE} already holds this config.`);
      else if (guard === "edited" && !opts.force) log(`Dry run: a real run would stop. ${edited} --force would overwrite the edits.`);
      else log(`Dry run: a real run would save this config to ${BASE}.`);
      log("Nothing was saved.");
      return 0;
    }
    if (guard === "edited" && !opts.force) {
      log(`Stopped: ${edited} Re-run with --force to overwrite the edits.`);
      return 1;
    }
    if (!existing) {
      await ha.call("lovelace/dashboards/create", DASHBOARD);
      log(`Created the dashboard ${BASE}.`);
    }
    if (guard === "unchanged") {
      log(`${BASE} already holds this config; nothing to save.`);
    } else {
      const { type, ...params } = saveMessage(config);
      await ha.call(type, params);
      log(`Saved the config to ${BASE}${guard === "edited" ? ", replacing the config that was there" : ""}.`);
    }
    writeJson(stateFile, config);
    log(`Open it at ${url.replace(/\/+$/, "")}${BASE}`);
    return 0;
  } finally {
    ha.close();
  }
}

/** Run the command line; resolves with the exit code. */
export async function main(argv = process.argv.slice(2), env = process.env) {
  let opts;
  try {
    opts = parseArgs({ args: argv, options: OPTIONS, allowPositionals: false }).values;
  } catch (err) {
    log(`${err.message}\n\n${USAGE}`);
    return 2;
  }
  if (opts.help) {
    process.stdout.write(USAGE);
    return 0;
  }
  try {
    if (opts.snapshot) {
      if (opts["save-snapshot"]) {
        log("--save-snapshot needs a connection to Home Assistant; it cannot be used with --snapshot.");
        return 2;
      }
      return await runOffline(opts);
    }
    return await runOnline(opts, env);
  } catch (err) {
    log(`Error: ${err.message}`);
    return 1;
  }
}

function isMain() {
  if (typeof import.meta.main === "boolean") return import.meta.main;
  try {
    return Boolean(process.argv[1]) && fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (isMain()) process.exitCode = await main();
