// Tests for the Heating dashboard generator: node --test "dashboard/*.test.mjs"
// All snapshots are small and invented: no real devices, places or people.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { after, describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  DASHBOARD,
  buildDashboard,
  checkConfig,
  collectTemplates,
  saveMessage,
  slugify,
} from "./heating_dashboard.mjs";

const SCRIPT = fileURLToPath(new URL("./heating_dashboard.mjs", import.meta.url));
const DOCS = "https://github.com/Kodestuen/HomeAssistant-SmartHeat";
const TIME = "2026-01-15T08:00:00+00:00";
const AREA_NAMES = { stue: "Stue", badevaerelse: "Badeværelse", kokken: "Køkken", kontor: "Kontor" };

// ------------------------------------------------------------------ synthetic snapshots

function newSnapshot({ weather = true, update = true, areas = ["stue", "badevaerelse", "kokken"] } = {}) {
  const snap = {
    get_states: [],
    "config/entity_registry/list": [],
    "config/device_registry/list": [],
    "config/area_registry/list": areas.map((id) => area(id)),
    "config_entries/get": [],
  };
  if (weather) {
    addEntry(snap, "01WEATHER00000000000000000", "met", "Home");
    addEntity(snap, "weather.forecast_test", "rainy",
      { temperature: 4.2, wind_speed: 5.1, supported_features: 3, friendly_name: "Forecast" },
      { platform: "met", unique_id: "test-forecast", config_entry_id: "01WEATHER00000000000000000" });
  }
  if (update) {
    // HACS names its device after the repository, like the zone devices
    addEntry(snap, "01HACS0000000000000000000", "hacs", "HACS");
    addDevice(snap, { id: "hacs-device", name: "SmartHeat", identifiers: [["hacs", "424242"]], config_entries: ["01HACS0000000000000000000"] });
    addEntity(snap, "update.smartheat_update", "off",
      { installed_version: "v0.2.0", latest_version: "v0.2.0", release_url: `${DOCS}/releases/v0.2.0`, friendly_name: "SmartHeat update" },
      { platform: "hacs", unique_id: "424242", config_entry_id: "01HACS0000000000000000000", device_id: "hacs-device", entity_category: "diagnostic" });
  }
  return snap;
}

function area(id, { floor_id = null } = {}) {
  return { area_id: id, name: AREA_NAMES[id] ?? id, floor_id, aliases: [], icon: null, labels: [], picture: null, temperature_entity_id: null, humidity_entity_id: null };
}

function addEntry(snap, entry_id, domain, title, { disabled_by = null } = {}) {
  snap["config_entries/get"].push({ entry_id, domain, title, source: "user", state: disabled_by ? "not_loaded" : "loaded", disabled_by, supports_options: true });
}

function addDevice(snap, { id, name, identifiers, config_entries = [], area_id = null, manufacturer = null, model = null, model_id = null }) {
  snap["config/device_registry/list"].push({ id, name, name_by_user: null, identifiers, config_entries, area_id, manufacturer, model, model_id, disabled_by: null, entry_type: null, via_device_id: null, labels: [] });
}

function registryRow(entity_id, { platform, unique_id, config_entry_id = null, device_id = null, entity_category = null, disabled_by = null, area_id = null }) {
  return { entity_id, platform, unique_id, config_entry_id, device_id, entity_category, disabled_by, hidden_by: null, area_id, has_entity_name: true, name: null, original_name: null, icon: null, labels: [], options: {} };
}

function addEntity(snap, entity_id, state, attributes, row = null) {
  snap.get_states.push({ entity_id, state, attributes, last_changed: TIME, last_updated: TIME, context: { id: "01CONTEXT", parent_id: null, user_id: null } });
  if (row) snap["config/entity_registry/list"].push(registryRow(entity_id, row));
}

// Disable an entity in the registry. Home Assistant drops its state when the integration
// reloads, about 30 s later; keepState is the snapshot taken before that.
function disable(snap, entity_id, { keepState = false } = {}) {
  snap["config/entity_registry/list"].find((r) => r.entity_id === entity_id).disabled_by = "user";
  if (!keepState) snap.get_states = snap.get_states.filter((s) => s.entity_id !== entity_id);
}

const TEMPERATURE = { unit_of_measurement: "°C", device_class: "temperature", state_class: "measurement" };
const TUNING = {
  underfloor: { hysteresis: 0.3, min_open_minutes: 15, min_closed_minutes: 15, coast_window_minutes: 240, max_return_temp: 45.0, sensor_timeout_minutes: 30, eco_temp: 19.0, away_temp: 16.0, frost_temp: 7.0 },
  radiator: { hysteresis: 0.3, min_open_minutes: 5, min_closed_minutes: 5, coast_window_minutes: 60, max_return_temp: 75.0, sensor_timeout_minutes: 30, eco_temp: 19.0, away_temp: 16.0, frost_temp: 7.0 },
};
// role, entity ID suffix (as Home Assistant names it), state, unit, state class
const DIAGNOSTICS = [
  ["learned_overshoot", "learned_overshoot", "0.31", "°C", "measurement"],
  ["heat_storage_factor", "heat_storage_factor", "1.6", null, "measurement"],
  ["holding_difference", "holding_difference", "4.4", "°C", "measurement"],
  ["learning_cycles", "learning_cycles", "6", null, "total_increasing"],
  ["last_peak_error", "last_peak_vs_target", "0.05", "°C", "measurement"],
  ["heat_up_rate", "heat_up_rate", "0.4", "°C/h", "measurement"],
  ["valve_duty", "valve_open_24_h", "35", "%", "measurement"],
  ["supply_return_difference", "supply_return_difference", "5.0", "°C", "measurement"],
];

let zoneCounter = 0;

// A SmartHeat zone: config entry, device, climate entity and diagnostic sensors, plus the
// valve and sensors it controls. version 1 is SmartHeat 0.1.0 (no configuration attributes,
// no model_id, no area); version 2 is SmartHeat 0.2.0.
function addZone(snap, {
  id, name, system = "underfloor", version = 2, area = null, deviceArea = true, supply = false,
  window = null, invert = false, valveDomain = "switch", state = "heat", restored = false,
  entryId = `01ZONE${String(++zoneCounter).padStart(20, "0")}`,
}) {
  const deviceId = `device-${entryId.toLowerCase()}`;
  addEntry(snap, entryId, "smartheat", name);
  addDevice(snap, {
    id: deviceId, name, identifiers: [["smartheat", entryId]], config_entries: [entryId], manufacturer: "SmartHeat",
    model: system === "underfloor" ? "Underfloor heating zone" : "Radiator zone",
    model_id: version >= 2 ? system : null, area_id: version >= 2 && deviceArea ? area : null,
  });

  // The valve and sensors come from other integrations; the return sensor has no registry row
  const inputs = {
    valve: `${valveDomain}.${id}_valve`,
    room: `sensor.${id}_temperature`,
    ret: `sensor.${id}_return_temperature`,
    supply: supply ? `sensor.${id}_supply_temperature` : null,
    window: window ? `binary_sensor.${id}_${window}` : null,
  };
  addEntity(snap, inputs.valve, valveDomain === "valve" ? "open" : "on", { friendly_name: `${name} valve` }, { platform: "demo", unique_id: `${id}-valve` });
  addEntity(snap, inputs.room, "21.2", { ...TEMPERATURE, friendly_name: `${name} temperature` }, { platform: "demo", unique_id: `${id}-room`, area_id: area });
  addEntity(snap, inputs.ret, "30.1", { ...TEMPERATURE, friendly_name: `${name} return` });
  if (supply) addEntity(snap, inputs.supply, "35.0", { ...TEMPERATURE, friendly_name: `${name} supply` }, { platform: "demo", unique_id: `${id}-supply` });
  if (window) addEntity(snap, inputs.window, "off", { device_class: window, friendly_name: `${name} ${window}` }, { platform: "demo", unique_id: `${id}-${window}` });

  let attributes = {
    hvac_modes: ["heat", "off"], min_temp: 5.0, max_temp: 28.0, target_temp_step: 0.5,
    preset_modes: ["none", "eco", "away", "frost"], current_temperature: 21.2, temperature: 21.5,
    hvac_action: "heating", preset_mode: "none", comfort_temperature: 21.5, reason: "heating",
    predicted_overshoot: 0.3, close_threshold: 21.2, coasting: false, return_temperature: 30.1,
    friendly_name: name, supported_features: 401,
  };
  if (supply) Object.assign(attributes, { supply_temperature: 35.0, supply_return_delta: 4.9, no_heat_source: false, loop_saturated: false });
  if (version >= 2) {
    Object.assign(attributes, {
      system_type: system, valve_entity_id: inputs.valve, room_sensor_entity_id: inputs.room,
      return_sensor_entity_id: inputs.ret, supply_sensor_entity_id: inputs.supply,
      window_sensor_entity_id: inputs.window, invert_valve: invert, trained: true, ...TUNING[system],
    });
  }
  // A zone that failed to load keeps its restored state
  if (restored) attributes = { friendly_name: name, supported_features: 401, restored: true };
  const zoneState = restored ? "unavailable" : state;
  const zoneRow = { platform: "smartheat", config_entry_id: entryId, device_id: deviceId };
  addEntity(snap, `climate.${id}`, zoneState, attributes, { ...zoneRow, unique_id: `${entryId}_climate` });
  for (const [role, suffix, value, unit, stateClass] of DIAGNOSTICS) {
    if (role === "supply_return_difference" && !supply) continue;
    const attrs = { state_class: stateClass, friendly_name: `${name} ${suffix}` };
    if (unit) attrs.unit_of_measurement = unit;
    addEntity(snap, `sensor.${id}_${suffix}`, restored ? "unavailable" : value, attrs, { ...zoneRow, unique_id: `${entryId}_${role}`, entity_category: "diagnostic" });
  }
  return { entryId, deviceId, climate: `climate.${id}`, inputs };
}

function addOutdoorHelper(snap) {
  const entryId = "01OUTDOOR00000000000000000";
  addEntry(snap, entryId, "template", "Outdoor temperature");
  addEntity(snap, "sensor.outdoor_temperature", "4.5", { ...TEMPERATURE, friendly_name: "Outdoor temperature" },
    { platform: "template", unique_id: entryId, config_entry_id: entryId });
}

// Two SmartHeat 0.2.0 zones with the outdoor helper. The bathroom is added first and sorts
// first by name, but the area registry lists the living room first.
function twoZoneSnapshot() {
  const snap = newSnapshot();
  addOutdoorHelper(snap);
  addZone(snap, { id: "badevaerelse", name: "Badeværelse", system: "radiator", area: "badevaerelse", valveDomain: "valve" });
  addZone(snap, { id: "stue", name: "Stue", area: "stue", supply: true, window: "door", invert: true });
  return snap;
}

// Build and check: every generated config must pass checkConfig
function build(snap) {
  const built = buildDashboard(snap);
  assert.deepEqual(checkConfig(built.config, snap), []);
  return built;
}

// A readable outline of a view: one line per section
function outline(view) {
  return view.sections.map((section) => {
    const cards = section.cards.map((c) => (c.type === "heading" ? `heading:${c.heading}` : c.name ? `${c.type}:${c.name}` : c.type));
    return `${section.column_span}${section.visibility ? ` if ${section.visibility[0].condition}` : ""}: ${cards.join(", ")}`;
  });
}

const sectionTitles = (view) => view.sections.map((s) => (s.cards[0].type === "heading" ? s.cards[0].heading : s.cards[0].type));
const findSection = (view, title) => view.sections.find((s) => s.cards[0].heading === title);
const viewByPath = (config, p) => config.views.find((v) => v.path === p);

// ------------------------------------------------------------------ building

describe("buildDashboard", () => {
  test("0 zones: notice, get started, outside and SmartHeat status (spec example A)", () => {
    const { config, warnings, zones } = build(newSnapshot());
    assert.deepEqual(warnings, []);
    assert.deepEqual(zones, []);
    assert.equal(config.title, "Heating");
    assert.deepEqual(config.views.map((v) => v.path), ["overview", "rooms"]);

    const overview = config.views[0];
    assert.deepEqual(overview.header, { layout: "responsive", badges_position: "bottom", badges_wrap: "wrap" });
    assert.deepEqual(overview.badges, [{
      type: "entity", entity: "weather.forecast_test", show_name: false, show_state: true, show_icon: true,
      state_content: ["state", "temperature"], tap_action: { action: "more-info" },
    }]);
    assert.deepEqual(outline(overview), [
      "3 if template: markdown",
      "2 if template: empty-state, markdown",
      "1: heading:Outside, tile:Weather, weather-forecast, heading:SmartHeat, tile:Software update, tile:Software update",
    ]);
    const [notice, start, side] = overview.sections;
    assert.equal(notice.visibility[0].value_template, "{{ states.climate | selectattr('entity_id', 'in', integration_entities('smartheat')) | rejectattr('entity_id', 'in', []) | list | count > 0 or [] | reject('in', states.climate | map(attribute='entity_id') | list) | list | count > 0 }}");
    assert.ok(notice.cards[0].content.startsWith("{%- set known = [] -%}\n{%- set names = {} -%}\n"));
    assert.equal(start.visibility[0].value_template, "{{ states.climate | selectattr('entity_id', 'in', integration_entities('smartheat')) | list | count == 0 }}");
    assert.deepEqual(start.cards[0].buttons.map((b) => b.tap_action), [
      { action: "navigate", navigation_path: "/_my_redirect/config_flow_start?domain=smartheat" },
      { action: "url", url_path: DOCS },
    ]);
    // supported_features 3: hourly feature on the tile, daily forecast card
    assert.deepEqual(side.cards[1].features, [{ type: "temperature-forecast", forecast_type: "hourly", hours_to_show: 24, show_labels: true }]);
    assert.equal(side.cards[2].forecast_type, "daily");
    assert.deepEqual(side.cards.slice(4).map((c) => [c.entity, c.visibility]), [
      ["update.smartheat_update", [{ condition: "state", entity: "update.smartheat_update", state_not: "on" }]],
      ["update.smartheat_update", [{ condition: "state", entity: "update.smartheat_update", state: "on" }]],
    ]);
    assert.deepEqual(config.views[1], {
      title: "All rooms", path: "rooms", icon: "mdi:home-thermometer", subview: true,
      back_path: "/dashboard-heating/overview", strategy: { type: "climate" },
    });
  });

  test("without weather or update entities the side column keeps only what exists", () => {
    const { config } = build(newSnapshot({ weather: false, update: false }));
    const overview = config.views[0];
    assert.equal(overview.badges, undefined);
    assert.deepEqual(outline(overview), ["3 if template: markdown", "2 if template: empty-state, markdown"]);
  });

  test("1 zone like SmartHeat 0.1.0: radiator wording, 24 h charts, no sensor sections", () => {
    const snap = newSnapshot();
    addZone(snap, { id: "kokken", name: "Køkken", system: "radiator", version: 1, area: "kokken" });
    const { config, warnings, zones } = build(snap);
    assert.deepEqual(warnings, []);
    assert.deepEqual(zones, [{
      name: "Køkken", entry_id: zones[0].entry_id, climate: "climate.kokken", path: "zone-kokken", system: "radiator",
      state: "heat", area: null, learned: 7, inputs: 0, tuning: 0,
    }]);
    assert.deepEqual(config.views.map((v) => v.path), ["overview", "zone-kokken", "rooms"]);

    const overview = config.views[0];
    assert.ok(overview.header.card.content.includes("{%- set zones = ['climate.kokken'] -%}"));
    assert.deepEqual(outline(overview).slice(1), [
      "2: heading:Rooms, tile",
      "1: heading:Outside, tile:Weather, weather-forecast, heading:SmartHeat, tile:Software update, tile:Software update, shortcut, shortcut",
    ]);
    assert.deepEqual(overview.sections[2].cards.slice(-2).map((c) => c.label), ["Add a zone", "Settings"]);

    const zone = viewByPath(config, "zone-kokken");
    assert.equal(zone.icon, "mdi:radiator");
    assert.deepEqual(sectionTitles(zone), [
      "thermostat", "Room vs target, last 24 h", "Controller", "What SmartHeat has learned",
      "Overshoot learning, last 14 days", "Valve open per day", "Settings",
    ]);
    const roomChart = zone.sections[1].cards[1];
    assert.equal(roomChart.hours_to_show, 24);
    assert.deepEqual(roomChart.entities, [{ entity: "climate.kokken", name: "Room" }]);
    const status = zone.header.card.content;
    assert.ok(status.includes("lets the radiators carry the room"));
    assert.ok(status.includes("The radiators hold enough heat"));
    assert.ok(status.includes("for this zone, so the valve stays closed."));
    assert.ok(!status.includes("[!WARNING]"), "no supply sensor, so no hot-water warning");
    assert.ok(!zone.sections[2].cards[1].content.includes("Supply water"));
    assert.deepEqual(findSection(zone, "Settings").cards[1].entities.map((r) => r.name), ["Reset learning", "Change zone settings", "Zone device"]);
    // The diagnostic sensors are found by unique_id, whatever their entity IDs are
    assert.deepEqual(findSection(zone, "What SmartHeat has learned").cards[1].entities.map((r) => r.entity), [
      "sensor.kokken_learned_overshoot", "sensor.kokken_last_peak_vs_target", "sensor.kokken_holding_difference",
      "sensor.kokken_heat_storage_factor", "sensor.kokken_heat_up_rate", "sensor.kokken_learning_cycles",
      "sensor.kokken_valve_open_24_h",
    ]);
  });

  test("2 zones like SmartHeat 0.2.0 with the outdoor helper", () => {
    const { config, zones } = build(twoZoneSnapshot());
    // Sorted by the area registry, not by name or creation order
    assert.deepEqual(zones.map((z) => [z.path, z.area]), [["zone-stue", "Stue"], ["zone-badevaerelse", "Badeværelse"]]);
    assert.deepEqual(config.views.map((v) => v.path), ["overview", "zone-stue", "zone-badevaerelse", "compare", "rooms"]);

    const overview = config.views[0];
    const main = overview.sections[1].cards;
    assert.deepEqual(main.filter((c) => c.type === "tile").map((c) => [c.entity, c.icon, c.tap_action.navigation_path]), [
      ["climate.stue", "mdi:heating-coil", "/dashboard-heating/zone-stue"],
      ["climate.badevaerelse", "mdi:radiator", "/dashboard-heating/zone-badevaerelse"],
    ]);
    assert.equal(main.at(-2).heading, "Whole house");
    assert.deepEqual(main.at(-1).cards.map((c) => [c.label, c.tap_action.data.preset_mode, c.tap_action.target.entity_id]), [
      ["Comfort", "none", ["climate.stue", "climate.badevaerelse"]],
      ["Eco", "eco", ["climate.stue", "climate.badevaerelse"]],
      ["Away", "away", ["climate.stue", "climate.badevaerelse"]],
    ]);
    const side = overview.sections[2].cards;
    assert.deepEqual(side.find((c) => c.entity === "sensor.outdoor_temperature").features, [{ type: "trend-graph", hours_to_show: 24 }]);
    assert.ok(side.some((c) => c.label === "Compare zones"));

    const stue = viewByPath(config, "zone-stue");
    assert.equal(stue.icon, "mdi:heating-coil");
    assert.deepEqual(sectionTitles(stue), [
      "thermostat", "Room vs target, last 48 h", "Controller", "Sensors and valve", "Water temperatures, last 48 h",
      "What SmartHeat has learned", "Overshoot learning, last 14 days", "Valve open per day",
      "Holding difference vs outdoor, last 60 days", "Settings",
    ]);
    assert.deepEqual(stue.sections[1].cards[1].entities, [{ entity: "binary_sensor.stue_door", name: "Door" }, { entity: "climate.stue", name: "Room" }]);
    assert.deepEqual(findSection(stue, "Sensors and valve").cards[1].entities, [
      { type: "simple-entity", entity: "switch.stue_valve", name: "Valve actuator (on = closed)", secondary_info: "last_changed", icon: "mdi:valve" },
      { entity: "sensor.stue_temperature", name: "Room temperature", icon: "mdi:home-thermometer", secondary_info: "last_updated" },
      { entity: "sensor.stue_return_temperature", name: "Return water", icon: "mdi:thermometer-water" },
      { entity: "sensor.stue_supply_temperature", name: "Supply water", icon: "mdi:water-thermometer" },
      { entity: "binary_sensor.stue_door", name: "Door", secondary_info: "last_changed" },
    ]);
    assert.deepEqual(findSection(stue, "Holding difference vs outdoor, last 60 days").cards[1].entities, [
      { entity: "sensor.stue_holding_difference", name: "Holding difference" },
      { entity: "sensor.outdoor_temperature", name: "Outdoor" },
    ]);
    const settings = findSection(stue, "Settings").cards[1].entities;
    assert.deepEqual(settings.slice(3).map((r) => r.attribute ?? r.label), [
      "Current tuning", "hysteresis", "min_open_minutes", "min_closed_minutes", "coast_window_minutes",
      "max_return_temp", "sensor_timeout_minutes", "eco_temp", "away_temp", "frost_temp",
    ]);
    assert.ok(stue.header.card.content.includes("lets the floor carry the room"));
    assert.ok(stue.header.card.content.includes("> No hot water is reaching the loop"));
    assert.ok(stue.sections[2].cards[1].content.includes("| Supply water |"));

    const bath = viewByPath(config, "zone-badevaerelse");
    const valveRow = findSection(bath, "Sensors and valve").cards[1].entities[0];
    assert.deepEqual(valveRow, { type: "simple-entity", entity: "valve.badevaerelse_valve", name: "Valve", secondary_info: "last_changed" });
    assert.deepEqual(findSection(bath, "Water temperatures, last 24 h").cards[1].entities.map((e) => e.name), ["Valve", "Return"]);

    const compare = viewByPath(config, "compare");
    assert.deepEqual(compare.sections.map((s) => s.cards[0].title), [
      "Last peak vs target, 14 days", "Learned overshoot, 14 days", "Valve open per day, 30 days",
      "Holding difference vs outdoor, 60 days", "Heat-up rate, 14 days", "Heat storage factor, 14 days",
    ]);
    assert.deepEqual(compare.sections[3].cards[0].entities, [
      { entity: "sensor.stue_holding_difference", name: "Stue" },
      { entity: "sensor.badevaerelse_holding_difference", name: "Badeværelse" },
      { entity: "sensor.outdoor_temperature", name: "Outdoor" },
    ]);
  });

  test("the Outdoor helper is used only with the state class Measurement", () => {
    // No state class, neither unit nor state class, and a state class without a daily mean
    for (const change of [{ state_class: null }, { state_class: null, unit_of_measurement: null }, { device_class: null, state_class: "total" }]) {
      const snap = twoZoneSnapshot();
      const helper = snap.get_states.find((s) => s.entity_id === "sensor.outdoor_temperature").attributes;
      for (const [key, value] of Object.entries(change)) {
        if (value === null) delete helper[key];
        else helper[key] = value;
      }
      const { config, warnings } = build(snap);
      assert.equal(warnings.length, 1);
      assert.match(warnings[0], /"Outdoor temperature" helper is left out because its state class is not Measurement/);
      assert.ok(!JSON.stringify(config).includes("sensor.outdoor_temperature"));
      assert.ok(!findSection(viewByPath(config, "zone-stue"), "Holding difference vs outdoor, last 60 days"));
      const holding = viewByPath(config, "compare").sections.find((s) => s.cards[0].title.startsWith("Holding difference"));
      assert.equal(holding.cards[0].title, "Holding difference, 60 days");
    }
  });

  test("the Rooms heading links to All rooms only when All rooms shows more than the zones", () => {
    const roomsLink = (snap) => build(snap).config.views[0].sections[1].cards[0].tap_action;
    const snap = twoZoneSnapshot();
    assert.equal(roomsLink(snap), undefined);
    // Like the climate view strategy, an area sensor counts only while it has a state
    const stue = snap["config/area_registry/list"].find((a) => a.area_id === "stue");
    stue.temperature_entity_id = "sensor.removed_temperature";
    assert.equal(roomsLink(snap), undefined);
    stue.temperature_entity_id = "sensor.stue_temperature";
    assert.deepEqual(roomsLink(snap), { action: "navigate", navigation_path: "/dashboard-heating/rooms" });
    // A climate entity that is not a zone shows there too
    stue.temperature_entity_id = null;
    addEntity(snap, "climate.heat_pump", "heat", { friendly_name: "Heat pump" }, { platform: "demo", unique_id: "heat-pump" });
    assert.deepEqual(roomsLink(snap), { action: "navigate", navigation_path: "/dashboard-heating/rooms" });
  });

  test("5 zones: the tiles drop the preset dropdown", () => {
    const snap = newSnapshot({ areas: [] });
    for (const [id, name] of [["a", "Arbejdsværelse"], ["b", "Bryggers"], ["c", "Contor"], ["d", "Dagligstue"], ["e", "Entré"]]) {
      addZone(snap, { id, name });
    }
    const { config } = build(snap);
    const tiles = config.views[0].sections[1].cards.filter((c) => c.type === "tile");
    assert.equal(tiles.length, 5);
    for (const tile of tiles) assert.deepEqual(tile.features, [{ type: "target-temperature" }]);
    // The thermostat on each zone page keeps both mode features
    assert.equal(config.views[1].sections[0].cards[0].features.length, 2);
  });

  test("a disabled sensor is dropped, a disabled zone is skipped, an unavailable zone still renders", () => {
    const snap = newSnapshot();
    addZone(snap, { id: "stue", name: "Stue", area: "stue" });
    addZone(snap, { id: "kontor", name: "Kontor" });
    addZone(snap, { id: "kokken", name: "Køkken", area: "kokken", restored: true });
    disable(snap, "sensor.stue_learned_overshoot", { keepState: true });
    disable(snap, "climate.kontor");
    addEntry(snap, "01LOFT00000000000000000000", "smartheat", "Loft", { disabled_by: "user" });
    const { config, warnings, zones } = build(snap);

    assert.equal(warnings.length, 3);
    assert.match(warnings[0], /"Kontor" has no usable climate entity/);
    assert.match(warnings[1], /"Køkken" is unavailable/);
    assert.match(warnings[2], /"Loft" is disabled/);
    assert.deepEqual(zones.map((z) => [z.path, z.state]), [["zone-stue", "heat"], ["zone-kokken", "unavailable"]]);
    const text = JSON.stringify(config);
    assert.ok(!text.includes("sensor.stue_learned_overshoot"));
    assert.ok(!text.includes("climate.kontor"));

    const stue = viewByPath(config, "zone-stue");
    assert.ok(!findSection(stue, "What SmartHeat has learned").cards[1].entities.some((r) => r.name === "Learned overshoot"));
    assert.deepEqual(findSection(stue, "Overshoot learning, last 14 days").cards[1].entities, [{ entity: "sensor.stue_last_peak_vs_target", name: "Last peak vs target" }]);
    const compare = viewByPath(config, "compare");
    const overshoot = compare.sections.find((s) => s.cards[0].title === "Learned overshoot, 14 days");
    assert.deepEqual(overshoot.cards[0].entities, [{ entity: "sensor.kokken_learned_overshoot", name: "Køkken" }]);

    // The restored zone has no attributes: the page stays, without the configuration sections
    const kitchen = viewByPath(config, "zone-kokken");
    assert.ok(kitchen.header.card.content.includes("## Not responding"));
    assert.ok(!sectionTitles(kitchen).includes("Sensors and valve"));
    assert.ok(sectionTitles(kitchen).includes("Settings"));
  });

  test("slugs: Danish letters, duplicates and names without Latin letters", () => {
    assert.equal(slugify("Badeværelse"), "badevaerelse");
    assert.equal(slugify("Køkken"), "kokken");
    assert.equal(slugify("Straße"), "strasse");
    assert.equal(slugify("  Café Ål, 1. sal "), "cafe-al-1-sal");
    assert.equal(slugify("Гостиная"), "");

    const snap = newSnapshot({ areas: [] });
    addZone(snap, { id: "bad", name: "Badeværelse", entryId: "01SLUG00000000000000000003" });
    addZone(snap, { id: "stue_b", name: "Stue", entryId: "01SLUG00000000000000000002" });
    addZone(snap, { id: "stue_a", name: "Stue", entryId: "01SLUG00000000000000000001" });
    addZone(snap, { id: "gostinaya", name: "Гостиная", entryId: "01SLUG00000000000000000004" });
    const { config, zones } = build(snap);
    assert.deepEqual(Object.fromEntries(zones.map((z) => [z.climate, z.path])), {
      "climate.bad": "zone-badevaerelse",
      "climate.stue_a": "zone-stue", // equal names: the lower entry_id comes first
      "climate.stue_b": "zone-stue-2",
      "climate.gostinaya": "zone-01slug00000000000000000004",
    });
    assert.deepEqual(config.views.map((v) => v.path).slice(0, 5), ["overview", "zone-badevaerelse", "zone-stue", "zone-stue-2", "zone-01slug00000000000000000004"]);
  });

  test("names with an apostrophe or a backslash are escaped in the Jinja literals", () => {
    const snap = newSnapshot({ areas: [] });
    addZone(snap, { id: "oles", name: "Ole's room" });
    addZone(snap, { id: "back", name: "Back\\slash" });
    const { config } = build(snap);
    const notice = config.views[0].sections[0].cards[0].content;
    assert.ok(notice.includes("{%- set known = ['climate.back', 'climate.oles'] -%}"));
    assert.ok(notice.includes("{%- set names = {'climate.back': 'Back\\\\slash', 'climate.oles': 'Ole\\'s room'} -%}"));
    // Reading the literals back the way Jinja does gives the names again
    const literals = [...notice.split("\n")[1].matchAll(/'((?:[^'\\]|\\.)*)'/g)].map((m) => m[1].replace(/\\(.)/g, "$1"));
    assert.deepEqual(literals, ["climate.back", "Back\\slash", "climate.oles", "Ole's room"]);
    assert.ok(config.views[0].header.card.content.startsWith("{%- set zones = ['climate.back', 'climate.oles'] -%}"));
    // Plain JSON text is not a template and keeps the name as it is
    const reset = findSection(viewByPath(config, "zone-ole-s-room"), "Settings").cards[1].entities[0];
    assert.equal(reset.tap_action.confirmation.text, "Forget what SmartHeat has learned about Ole's room? It starts again from the defaults.");
  });

  test("the zone name is the first non-empty of friendly name, device names and entry title", () => {
    const snap = newSnapshot({ areas: [] });
    addZone(snap, { id: "z1", name: "Entry title" });
    addZone(snap, { id: "z2", name: "Second" });
    const attrs = (id) => snap.get_states.find((s) => s.entity_id === id).attributes;
    const device = (n) => snap["config/device_registry/list"].filter((d) => d.manufacturer === "SmartHeat")[n];
    delete attrs("climate.z1").friendly_name;
    device(0).name = " ";
    attrs("climate.z2").friendly_name = "  ";
    device(1).name_by_user = " Guest room ";
    const { zones } = build(snap);
    assert.deepEqual(zones.map((z) => [z.climate, z.name, z.path]), [
      ["climate.z1", "Entry title", "zone-entry-title"],
      ["climate.z2", "Guest room", "zone-guest-room"],
    ]);
  });

  test("floors add headings and order the zones", () => {
    const snap = newSnapshot({ areas: [] });
    snap["config/floor_registry/list"] = [{ floor_id: "ground", name: "Ground floor", level: 0 }, { floor_id: "upper", name: "Upper floor", level: 1 }];
    snap["config/area_registry/list"] = [area("badevaerelse", { floor_id: "upper" }), area("stue", { floor_id: "ground" }), area("kontor")];
    addZone(snap, { id: "bad", name: "Badeværelse", area: "badevaerelse" });
    addZone(snap, { id: "kontor", name: "Kontor", area: "kontor" });
    addZone(snap, { id: "stue", name: "Stue", area: "stue" });
    const { config } = build(snap);
    assert.deepEqual(outline(config.views[0])[1], "2: heading:Rooms, heading:Ground floor, tile, heading:Upper floor, tile, heading:Other rooms, tile, heading:Whole house, grid");
    assert.deepEqual(config.views.slice(1, 4).map((v) => v.path), ["zone-stue", "zone-badevaerelse", "zone-kontor"]);
  });

  test("the room sensor's area is used when the zone device has none", () => {
    const snap = newSnapshot();
    addZone(snap, { id: "z1", name: "Alpha", area: null });
    addZone(snap, { id: "z2", name: "Beta", area: "stue", deviceArea: false });
    const { zones } = build(snap);
    assert.deepEqual(zones.map((z) => [z.name, z.area]), [["Beta", "Stue"], ["Alpha", null]]);
  });

  test("admin links are hidden from other users when the user list is known", () => {
    const snap = twoZoneSnapshot();
    snap["config/auth/list"] = [
      { id: "user-admin", name: "Admin", is_active: true, system_generated: false, group_ids: ["system-admin"] },
      { id: "user-family", name: "Family", is_active: true, system_generated: false, group_ids: ["system-users"] },
      { id: "user-system", name: "Supervisor", is_active: true, system_generated: true, group_ids: ["system-admin"] },
    ];
    const { config } = build(snap);
    const gate = [{ condition: "user", users: ["user-admin"] }];
    const side = config.views[0].sections[2].cards;
    assert.deepEqual(side.filter((c) => c.visibility?.[0].condition === "user").map((c) => [c.label, c.visibility]), [["Add a zone", gate], ["Settings", gate]]);
    assert.deepEqual(findSection(viewByPath(config, "zone-stue"), "Settings").visibility, gate);

    // Only admins: nothing to hide
    snap["config/auth/list"] = snap["config/auth/list"].filter((u) => u.id !== "user-family");
    assert.ok(!JSON.stringify(build(snap).config).includes('"condition":"user"'));
  });

  test("the same snapshot always gives the same config", () => {
    const snap = twoZoneSnapshot();
    const first = JSON.stringify(buildDashboard(snap));
    assert.equal(JSON.stringify(buildDashboard(structuredClone(snap))), first);
    // The order of states and registry rows does not matter
    const shuffled = structuredClone(snap);
    for (const key of ["get_states", "config/entity_registry/list", "config/device_registry/list", "config_entries/get"]) shuffled[key].reverse();
    assert.equal(JSON.stringify(buildDashboard(shuffled).config), JSON.stringify(JSON.parse(first).config));
  });

  test("a snapshot without a required list is refused", () => {
    const snap = newSnapshot();
    delete snap["config_entries/get"];
    assert.throws(() => buildDashboard(snap), /config_entries\/get/);
  });
});

// ------------------------------------------------------------------ checks

describe("checkConfig", () => {
  test("flags unusable entities, empty graphs, unknown keys, icons and links", () => {
    const snap = twoZoneSnapshot();
    const { config } = build(snap);
    const broken = structuredClone(config);
    const stue = viewByPath(broken, "zone-stue");
    stue.sections[0].cards[0].entity = "climate.does_not_exist";
    stue.sections[1].cards[1].entities = [];
    stue.sections[2].cards[0].icon = "mdi:not-an-icon";
    stue.badges[0].colour = "red";
    broken.views[0].sections[1].cards[1].tap_action.navigation_path = "/dashboard-heating/nowhere";
    broken.views[0].sections[2].cards.at(-1).tap_action.navigation_path = "/lovelace/0";
    viewByPath(broken, "zone-badevaerelse").sections[0].cards[0].entity = "switch.stue_valve";

    const problems = checkConfig(broken, snap);
    const expect = (pattern) => assert.ok(problems.some((p) => pattern.test(p)), `no problem matches ${pattern}:\n${problems.join("\n")}`);
    expect(/climate\.does_not_exist" is not usable/);
    expect(/history-graph has no entities/);
    expect(/icon "mdi:not-an-icon"/);
    expect(/unexpected key colour/);
    expect(/link to a missing view \/dashboard-heating\/nowhere/);
    expect(/link \/lovelace\/0 points outside \/dashboard-heating/);
    expect(/thermostat needs a climate entity/);
    assert.equal(problems.length, 7, problems.join("\n"));
  });

  test("flags a disabled entity, a bad trend graph, a bad weblink and duplicate paths", () => {
    const snap = twoZoneSnapshot();
    const { config } = build(snap);
    disable(snap, "sensor.stue_valve_open_24_h");
    const broken = structuredClone(config);
    broken.views[0].sections[2].cards.find((c) => c.entity === "sensor.outdoor_temperature").entity = "weather.forecast_test";
    findSection(viewByPath(broken, "zone-badevaerelse"), "Settings").cards[1].entities[2].url = "https://example.com/device";
    broken.views[2].path = "zone-stue";
    const problems = checkConfig(broken, snap);
    assert.ok(problems.some((p) => p.includes('"sensor.stue_valve_open_24_h" is not usable')));
    assert.ok(problems.some((p) => p.includes("trend-graph needs a sensor")));
    assert.ok(problems.some((p) => p.endsWith('.cards[1].entities[2]: bad link "https://example.com/device"')), problems.join("\n"));
    assert.ok(problems.some((p) => p.includes("duplicate path zone-stue")));
  });

  test("the save message always names the Heating dashboard", () => {
    const { config } = build(newSnapshot());
    assert.deepEqual(saveMessage(config), { type: "lovelace/config/save", url_path: "dashboard-heating", config });
    assert.deepEqual(DASHBOARD, {
      url_path: "dashboard-heating", title: "Heating", icon: "mdi:radiator",
      show_in_sidebar: true, require_admin: false, mode: "storage",
    });
  });

  test("collects every markdown and template-condition template once", () => {
    const { config } = build(twoZoneSnapshot());
    const templates = collectTemplates(config);
    // T2a, T2b and T3 on the overview; T4, T5 and S2 per zone (S2 is shared)
    assert.equal(templates.length, 3 + 2 * 2 + 1);
    assert.deepEqual(templates.slice(0, 3).map((t) => t.where), [
      "overview.header.card.content",
      "overview.sections[0].visibility[0].value_template",
      "overview.sections[0].cards[0].content",
    ]);
  });
});

// ------------------------------------------------------------------ command line

const TOKEN = "test-token-7d1f0c";
const tempDirs = [];
after(() => {
  for (const dir of tempDirs) fs.rmSync(dir, { recursive: true, force: true });
});

function tempDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "heating-dashboard-"));
  tempDirs.push(dir);
  return dir;
}

// Run the CLI in a child process; HA_URL and HA_TOKEN come only from `env`
function cli(args, env = {}, { cwd } = {}) {
  const childEnv = { ...process.env, ...env };
  for (const key of ["HA_URL", "HA_TOKEN"]) if (!(key in env)) delete childEnv[key];
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [SCRIPT, ...args], { env: childEnv, cwd });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
}

// WebSocket framing for the fake server: unmasked frames out, masked frames in
function encodeFrame(payload, opcode = 1) {
  const data = Buffer.from(payload);
  let header;
  if (data.length < 126) {
    header = Buffer.from([0x80 | opcode, data.length]);
  } else if (data.length < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(data.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(data.length), 2);
  }
  return Buffer.concat([header, data]);
}

function decodeFrame(buffer) {
  if (buffer.length < 2) return null;
  let length = buffer[1] & 0x7f;
  let offset = 2;
  if (length === 126) {
    if (buffer.length < 4) return null;
    length = buffer.readUInt16BE(2);
    offset = 4;
  } else if (length === 127) {
    if (buffer.length < 10) return null;
    length = Number(buffer.readBigUInt64BE(2));
    offset = 10;
  }
  const masked = (buffer[1] & 0x80) !== 0;
  const mask = masked ? buffer.subarray(offset, offset + 4) : null;
  if (masked) offset += 4;
  if (buffer.length < offset + length) return null;
  const payload = Buffer.from(buffer.subarray(offset, offset + length));
  if (mask) for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
  return { opcode: buffer[0] & 0x0f, payload, size: offset + length };
}

// Just enough of Home Assistant's WebSocket API for the generator, answering from a snapshot.
// saveError makes lovelace/config/save fail the way Home Assistant reports it.
async function startFakeHomeAssistant({ snapshot, dashboards = [], stored = null, templateError = null, saveError = null }) {
  const ha = { received: [], dashboards: structuredClone(dashboards), stored };
  const sockets = new Set();
  const server = http.createServer((req, res) => res.writeHead(404).end());
  server.on("upgrade", (req, socket, head) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    socket.on("error", () => {});
    if (req.url !== "/api/websocket") return socket.destroy();
    const accept = crypto.createHash("sha1").update(`${req.headers["sec-websocket-key"]}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    const send = (msg) => socket.write(encodeFrame(JSON.stringify(msg)));
    let authenticated = false;
    let buffer = Buffer.from(head);

    const handle = (msg) => {
      ha.received.push(msg);
      if (!authenticated) {
        if (msg.type === "auth" && msg.access_token === TOKEN) {
          authenticated = true;
          send({ type: "auth_ok", ha_version: "2026.10.1" });
        } else {
          send({ type: "auth_invalid", message: "Invalid access token or password" });
          socket.end();
        }
        return;
      }
      const result = (value) => send({ id: msg.id, type: "result", success: true, result: value });
      const error = (code, message) => send({ id: msg.id, type: "result", success: false, error: { code, message } });
      switch (msg.type) {
        case "render_template":
          if (templateError && msg.template.includes(templateError.match)) {
            // Like HA with report_errors: the failed first render is reported before the result
            send({ id: msg.id, type: "event", event: { error: templateError.message, level: "ERROR" } });
            result(null);
            send({ id: msg.id, type: "event", event: { error: templateError.message, level: "ERROR" } });
          } else {
            result(null);
            send({ id: msg.id, type: "event", event: { result: "rendered", listeners: { all: false, domains: ["climate"], entities: [], time: false } } });
          }
          return;
        case "unsubscribe_events":
          return result(null);
        case "manifest/get":
          return snapshot["manifest/get:smartheat"] ? result(snapshot["manifest/get:smartheat"]) : error("not_found", "Integration not found");
        case "lovelace/dashboards/list":
          return result(ha.dashboards);
        case "lovelace/dashboards/create": {
          const { id, type, ...fields } = msg;
          ha.dashboards.push({ id: "dashboard_heating", ...fields });
          return result({ id: "dashboard_heating", ...fields });
        }
        case "lovelace/config":
          if (!ha.dashboards.some((d) => d.url_path === msg.url_path)) return error("config_not_found", `Unknown config specified: ${msg.url_path}`);
          return ha.stored === null ? error("config_not_found", "No config found.") : result(ha.stored);
        case "lovelace/config/save":
          if (saveError) return error("error", saveError);
          ha.stored = msg.config;
          return result(null);
        default:
          return msg.type in snapshot ? result(snapshot[msg.type]) : error("unknown_command", "Unknown command.");
      }
    };

    socket.on("data", (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      for (let frame = decodeFrame(buffer); frame; frame = decodeFrame(buffer)) {
        buffer = buffer.subarray(frame.size);
        if (frame.opcode === 8) return socket.end(encodeFrame(Buffer.from([0x03, 0xe8]), 8));
        if (frame.opcode === 9) socket.write(encodeFrame(frame.payload, 10));
        if (frame.opcode === 1) handle(JSON.parse(frame.payload.toString("utf8")));
      }
    });
    send({ type: "auth_required", ha_version: "2026.10.1" });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  ha.url = `http://127.0.0.1:${server.address().port}`;
  ha.close = () => new Promise((resolve) => {
    for (const socket of sockets) socket.destroy();
    server.close(resolve);
  });
  ha.sent = (type) => ha.received.filter((m) => m.type === type);
  return ha;
}

function tokenFile(dir, token = TOKEN) {
  const file = path.join(dir, "ha.token.txt");
  fs.writeFileSync(file, `${token}\n`);
  return file;
}

describe("command line", () => {
  test("--snapshot builds offline and prints the config", async () => {
    const dir = tempDir();
    const snap = twoZoneSnapshot();
    const file = path.join(dir, "test.snapshot.json");
    fs.writeFileSync(file, JSON.stringify(snap));
    const run = await cli(["--snapshot", file, "--dry-run"]);
    assert.equal(run.code, 0, run.stderr);
    assert.deepEqual(JSON.parse(run.stdout), buildDashboard(snap).config);
    assert.match(run.stderr, /Found 2 SmartHeat zones/);
    assert.match(run.stderr, /nothing was saved/);

    const out = path.join(dir, "config.json");
    const written = await cli(["--snapshot", file, "--out", out], {}, { cwd: path.dirname(SCRIPT) });
    assert.equal(written.code, 0, written.stderr);
    assert.equal(written.stdout, "");
    assert.deepEqual(JSON.parse(fs.readFileSync(out, "utf8")), buildDashboard(snap).config);
    // A file outside the current folder is named by its full path, one inside it relatively
    assert.ok(written.stderr.includes(`Wrote the config to ${out}.`), written.stderr);
    const inside = await cli(["--snapshot", file, "--out", "inside.json"], {}, { cwd: dir });
    assert.equal(inside.code, 0, inside.stderr);
    assert.ok(inside.stderr.includes("Wrote the config to inside.json."), inside.stderr);
    assert.ok(fs.existsSync(path.join(dir, "inside.json")));
  });

  test("usage errors exit with code 2", async () => {
    assert.equal((await cli([])).code, 2);
    assert.equal((await cli(["--bogus"])).code, 2);
    assert.equal((await cli(["--snapshot", "x.json", "--save-snapshot", "y.json"])).code, 2);

    // A missing or empty token file and an address that is not http(s) are found before connecting
    const dir = tempDir();
    const state = ["--state-file", path.join(dir, "last.json")];
    const empty = path.join(dir, "empty.token.txt");
    fs.writeFileSync(empty, "\n");
    const missing = await cli(["--url", "http://127.0.0.1:9", "--token-file", path.join(dir, "missing.token.txt"), ...state]);
    assert.equal(missing.code, 2);
    assert.match(missing.stderr, /Cannot read the token file/);
    const blank = await cli(["--url", "http://127.0.0.1:9", "--token-file", empty, ...state]);
    assert.equal(blank.code, 2);
    assert.match(blank.stderr, /is empty/);
    const scheme = await cli(["--url", "ftp://127.0.0.1:9", "--token-file", tokenFile(dir), ...state]);
    assert.equal(scheme.code, 2);
    assert.match(scheme.stderr, /must start with http:\/\/ or https:\/\//);
    assert.ok(!fs.existsSync(path.join(dir, "last.json")));
  });

  test("first run: fetch, check, render, create the dashboard, save with url_path", async () => {
    const snap = twoZoneSnapshot();
    const expected = buildDashboard(snap).config;
    const ha = await startFakeHomeAssistant({ snapshot: snap });
    try {
      const dir = tempDir();
      const stateFile = path.join(dir, "last.json");
      const snapshotFile = path.join(dir, "saved.snapshot.json");
      const run = await cli(["--url", ha.url, "--token-file", tokenFile(dir), "--state-file", stateFile, "--save-snapshot", snapshotFile]);
      assert.equal(run.code, 0, run.stderr);

      assert.deepEqual(ha.received[0], { type: "auth", access_token: TOKEN });
      const ids = ha.received.slice(1).map((m) => m.id);
      assert.deepEqual(ids, ids.map((_, i) => i + 1), "ids count up from 1");
      assert.deepEqual(ha.sent("manifest/get").map((m) => m.integration), ["smartheat"]);
      assert.deepEqual(JSON.parse(fs.readFileSync(snapshotFile, "utf8")), snap);

      const renders = ha.sent("render_template");
      assert.deepEqual(renders.map((m) => m.template), collectTemplates(expected).map((t) => t.template));
      for (const render of renders) {
        assert.equal(render.strict, true);
        assert.equal(render.report_errors, true);
        assert.equal(ha.sent("unsubscribe_events").filter((m) => m.subscription === render.id).length, 1);
      }

      const [create] = ha.sent("lovelace/dashboards/create");
      const { id, type, ...fields } = create;
      assert.deepEqual(fields, DASHBOARD);
      const [save] = ha.sent("lovelace/config/save");
      assert.equal(save.url_path, "dashboard-heating");
      assert.deepEqual(save.config, expected);
      assert.ok(create.id < save.id);
      assert.deepEqual(JSON.parse(fs.readFileSync(stateFile, "utf8")), expected);
      assert.match(run.stderr, /Created the dashboard \/dashboard-heating/);
      assert.match(run.stderr, /Saved the config to \/dashboard-heating/);

      for (const text of [run.stdout, run.stderr, fs.readFileSync(stateFile, "utf8"), fs.readFileSync(snapshotFile, "utf8")]) {
        assert.ok(!text.includes(TOKEN), "the token is never printed or written");
      }
    } finally {
      await ha.close();
    }
  });

  test("the edit guard: unchanged saves nothing, a UI edit stops the run until --force", async () => {
    const snap = twoZoneSnapshot();
    const expected = buildDashboard(snap).config;
    const ha = await startFakeHomeAssistant({ snapshot: snap, dashboards: [{ id: "dashboard_heating", ...DASHBOARD }], stored: expected });
    try {
      const dir = tempDir();
      const stateFile = path.join(dir, "last.json");
      fs.writeFileSync(stateFile, JSON.stringify(expected));
      const args = ["--url", ha.url, "--token-file", tokenFile(dir), "--state-file", stateFile];

      const unchanged = await cli(args);
      assert.equal(unchanged.code, 0, unchanged.stderr);
      assert.match(unchanged.stderr, /already holds this config/);
      assert.equal(ha.sent("lovelace/config/save").length, 0);
      assert.equal(ha.sent("lovelace/dashboards/create").length, 0);

      // Someone renames a section in the UI, and the snapshot changes too
      const edited = structuredClone(expected);
      edited.views[0].sections[1].cards[0].heading = "Our rooms";
      ha.stored = edited;
      snap.get_states.find((s) => s.entity_id === "climate.stue").attributes.preset_modes = ["none", "eco"];
      const stopped = await cli(args);
      assert.equal(stopped.code, 1);
      assert.match(stopped.stderr, /edited in the UI since the last run/);
      assert.match(stopped.stderr, /--force/);
      assert.equal(ha.sent("lovelace/config/save").length, 0);

      const forced = await cli([...args, "--force"]);
      assert.equal(forced.code, 0, forced.stderr);
      assert.equal(ha.sent("lovelace/config/save").length, 1);
      assert.deepEqual(ha.stored, buildDashboard(snap).config);
      assert.deepEqual(JSON.parse(fs.readFileSync(stateFile, "utf8")), ha.stored);

      // Without the state file a different stored config is treated as edited
      fs.rmSync(stateFile);
      ha.stored = edited;
      const unknown = await cli(args);
      assert.equal(unknown.code, 1);
      assert.match(unknown.stderr, /is missing/);
      assert.ok(!fs.existsSync(stateFile));

      // ...but a stored config equal to the new one overwrites nothing: it is kept as the last run
      ha.stored = buildDashboard(snap).config;
      const adopted = await cli(args);
      assert.equal(adopted.code, 0, adopted.stderr);
      assert.match(adopted.stderr, /already holds this config/);
      assert.equal(ha.sent("lovelace/config/save").length, 1);
      assert.deepEqual(JSON.parse(fs.readFileSync(stateFile, "utf8")), ha.stored);
    } finally {
      await ha.close();
    }
  });

  test("an existing dashboard with nothing saved yet is saved without creating it again", async () => {
    const snap = twoZoneSnapshot();
    const expected = buildDashboard(snap).config;
    const ha = await startFakeHomeAssistant({ snapshot: snap, dashboards: [{ id: "dashboard_heating", ...DASHBOARD }] });
    try {
      const dir = tempDir();
      const stateFile = path.join(dir, "last.json");
      const run = await cli(["--url", ha.url, "--token-file", tokenFile(dir), "--state-file", stateFile]);
      assert.equal(run.code, 0, run.stderr);
      // lovelace/config answers config_not_found, so there is nothing to guard
      assert.deepEqual(ha.sent("lovelace/config").map((m) => m.url_path), ["dashboard-heating"]);
      assert.equal(ha.sent("lovelace/dashboards/create").length, 0);
      const saves = ha.sent("lovelace/config/save");
      assert.equal(saves.length, 1);
      assert.equal(saves[0].url_path, "dashboard-heating");
      assert.deepEqual(saves[0].config, expected);
      assert.deepEqual(JSON.parse(fs.readFileSync(stateFile, "utf8")), expected);
    } finally {
      await ha.close();
    }
  });

  test("a YAML dashboard at the same address is refused", async () => {
    // How lovelace/dashboards/list reports a dashboard from configuration.yaml
    const yaml = { mode: "yaml", filename: "heating.yaml", title: "Heating", icon: "mdi:radiator", show_in_sidebar: true, require_admin: false, url_path: "dashboard-heating" };
    const ha = await startFakeHomeAssistant({ snapshot: twoZoneSnapshot(), dashboards: [yaml] });
    try {
      const dir = tempDir();
      const stateFile = path.join(dir, "last.json");
      const run = await cli(["--url", ha.url, "--token-file", tokenFile(dir), "--state-file", stateFile]);
      assert.equal(run.code, 1);
      assert.match(run.stderr, /\/dashboard-heating is a yaml dashboard/);
      assert.equal(ha.sent("lovelace/dashboards/create").length, 0);
      assert.equal(ha.sent("lovelace/config/save").length, 0);
      assert.ok(!fs.existsSync(stateFile));
    } finally {
      await ha.close();
    }
  });

  test("a failed save exits with 1 and writes no state file", async () => {
    const ha = await startFakeHomeAssistant({ snapshot: twoZoneSnapshot(), saveError: "Saving not supported in recovery mode" });
    try {
      const dir = tempDir();
      const stateFile = path.join(dir, "last.json");
      const run = await cli(["--url", ha.url, "--token-file", tokenFile(dir), "--state-file", stateFile]);
      assert.equal(run.code, 1);
      assert.match(run.stderr, /lovelace\/config\/save failed: Saving not supported in recovery mode \(error\)/);
      assert.equal(ha.sent("lovelace/config/save").length, 1);
      assert.ok(!fs.existsSync(stateFile));
    } finally {
      await ha.close();
    }
  });

  test("a template error stops every run, --force included, before anything is created or saved", async () => {
    const snap = twoZoneSnapshot();
    const ha = await startFakeHomeAssistant({ snapshot: snap, templateError: { match: "set known", message: "UndefinedError: 'names' is undefined" } });
    try {
      const dir = tempDir();
      const stateFile = path.join(dir, "last.json");
      const args = ["--url", ha.url, "--token-file", tokenFile(dir), "--state-file", stateFile];
      // --force only skips the edit guard
      for (const extra of [[], ["--force"]]) {
        const run = await cli([...args, ...extra]);
        assert.equal(run.code, 1);
        assert.match(run.stderr, /overview\.sections\[0\]\.cards\[0\]\.content: ERROR: UndefinedError: 'names' is undefined/);
        assert.match(run.stderr, /Nothing was saved/);
        assert.doesNotMatch(run.stderr, /--force/);
      }
      assert.equal(ha.sent("lovelace/dashboards/create").length, 0);
      assert.equal(ha.sent("lovelace/config/save").length, 0);
      assert.equal(ha.sent("unsubscribe_events").length, ha.sent("render_template").length);
      assert.ok(!fs.existsSync(stateFile));

      // A dry run tells that a real run would stop
      const dry = await cli([...args, "--dry-run"]);
      assert.equal(dry.code, 1);
      assert.match(dry.stderr, /Dry run: a real run would stop because of the problems above/);
      assert.doesNotMatch(dry.stderr, /would create|would save/);
    } finally {
      await ha.close();
    }
  });

  test("--dry-run with HA_URL and HA_TOKEN prints the config and changes nothing", async () => {
    const snap = twoZoneSnapshot();
    const ha = await startFakeHomeAssistant({ snapshot: snap });
    try {
      const dir = tempDir();
      const run = await cli(["--dry-run", "--state-file", path.join(dir, "last.json")], { HA_URL: ha.url, HA_TOKEN: TOKEN });
      assert.equal(run.code, 0, run.stderr);
      assert.deepEqual(JSON.parse(run.stdout), buildDashboard(snap).config);
      assert.match(run.stderr, /a real run would create \/dashboard-heating/);
      assert.equal(ha.sent("lovelace/dashboards/create").length, 0);
      assert.equal(ha.sent("lovelace/config/save").length, 0);
      assert.ok(!fs.existsSync(path.join(dir, "last.json")));
    } finally {
      await ha.close();
    }
  });

  test("a refused token or an unreachable server fails cleanly", async () => {
    const ha = await startFakeHomeAssistant({ snapshot: newSnapshot() });
    const dir = tempDir();
    const state = ["--state-file", path.join(dir, "last.json")];
    try {
      const refused = await cli(["--url", ha.url, "--token-file", tokenFile(dir, "wrong-token-123"), ...state]);
      assert.equal(refused.code, 1);
      assert.match(refused.stderr, /refused the token/);
      assert.ok(!refused.stderr.includes("wrong-token-123"));
    } finally {
      await ha.close();
    }
    const closed = await cli(["--url", ha.url, "--token-file", tokenFile(dir), ...state]);
    assert.equal(closed.code, 1);
    assert.match(closed.stderr, /Cannot connect|closed the connection/);
    assert.ok(!fs.existsSync(path.join(dir, "last.json")));
  });
});
