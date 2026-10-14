# SmartHeat

SmartHeat is a Home Assistant integration for water-based heating zones (underfloor heating or radiators) that are controlled by an on/off valve. It works like a thermostat, but it learns how far the room keeps warming after the valve closes, and closes the valve early by exactly that much. The room then peaks at the target instead of overshooting it.

It was inspired by [Better Thermostat](https://github.com/KartoffelToby/better_thermostat), but it controls a valve directly and uses the water temperature to understand how much heat is still stored in the floor or radiator.

## How it works

One SmartHeat zone uses:

| Entity | Required | Purpose |
|---|---|---|
| Valve (`switch`, `input_boolean` or `valve`) | yes | Lets hot water into the zone |
| Room temperature sensor | yes | What the zone controls |
| Return temperature sensor | for underfloor heating | Water temperature at the end of the loop, on the return pipe; optional for radiators ([see below](#radiators-without-a-return-sensor)) |
| Supply temperature sensor | no | Water going in; used to notice when no hot water is available |
| Window or door sensor | no | The valve stays closed while it is open |

The control rules:

1. **Open** when the room is more than the hysteresis (default 0.3 °C) below the target.
2. **Close early** when the room reaches *target − predicted overshoot*.
3. **Coast**: after an early close, the valve stays closed until the room has peaked, even if the room is still below the opening threshold. The peak is then used for learning.
4. **Safety first**. These rules override the minimum open and closed times:
   - The valve closes when the return temperature exceeds the maximum (to protect the floor), when the window is open, or when the zone is off.
   - Below the frost protection temperature the valve opens, even when the zone is off. If the room sensor is gone, the return temperature stands in for it (when the zone has a return sensor).
5. **Minimum open and closed times** protect thermal actuators, which need a few minutes to move.
6. **Room sensor lost**. When the room sensor becomes unavailable, the valve first stays as it is. Once the sensor has been unavailable for the room sensor timeout (30 min):
   - An underfloor zone keeps heating by the last day's average. Every 45 minutes it opens the valve for the share of the time the valve was open over the last day, measured while the sensor worked: at 40 %, that is 18 minutes. Each zone opens at its own minutes, so zones don't all ask for heat at once. A floor stores a lot of heat and its room needs about the same heat from one day to the next, so this keeps the room near its usual temperature. Closing the valve instead lets the room cool all the way down: in the [loop simulation](#simulation-results), a day without the room sensor cools the room to 4.8–9.9 °C with the valve closed, while with the hold it stays between 17.6 and 23.5 °C. The safety rules still come first, and openings that start without the room sensor teach the overshoot model nothing.
   - The time is rounded so that the valve doesn't move for less than 5 minutes: an opening under 2.5 minutes is left out, one under 5 minutes becomes 5 minutes, and if less than 5 minutes would be left closed, the valve stays open. The share is not adjusted to the weather, or to a new target or preset, until the sensor is back.
   - A radiator zone closes the valve until the sensor is back.
   - An underfloor zone also counts its room sensor as lost when it hasn't sent a new reading for 6 hours (the setting **Room sensor silent after**; 0 turns it off). Some sensors keep showing their last value after their battery or radio has failed, and the zone would then heat to a reading that no longer changes. The sensor counts as lost from its last reading: with the default settings the room sensor timeout has passed by then, and the zone heats by the average at once. While this check is on, the average also leaves out the time when the last reading is more than an hour old.
   - Some integrations, for example MQTT (including Zigbee2MQTT) and deCONZ, only update a sensor when its value changes. A sensor with coarse steps in a very steady room can then look silent: raise the setting, or set it to 0, for such a sensor. Not checked are helpers such as template, min/max, filter, statistics, group, derivative and compensation sensors, which may stay silent while the temperature is steady, and entities without a unique ID, such as states set through the API. After a restart of Home Assistant the 6 hours start over, as sensors write their state again then.

### What is learned

After the valve closes, the floor or radiator keeps giving off heat. Call x the difference *return temperature − room temperature*.

To merely hold the room temperature, the emitter has to stay some **h** degrees above the room. h depends on the heat loss, and therefore on the weather. Only heat above that level pushes the room up. With the emitter cooling exponentially once the flow stops, the room rises by

```
overshoot = β · ((x − h) − h · ln(x / h))        for x > h, otherwise 0
```

where **β** is how much heat the emitter stores relative to the room. A thick screed floor has a large β, and a radiator a small one. Both are learned:

- **h (holding difference)**: right after the valve opens, the room keeps cooling until the emitter just covers the heat loss. The return-minus-room difference at that low point is h. Water is flowing at that moment, so the return sensor reads the emitter and not a cold pipe.
- **β (heat storage factor)**: learned from the overshoot actually measured after each early close.

Until three cycles have taught β, SmartHeat uses a plain average of the measured overshoot, and without a return sensor it always does. It starts from 0.4 °C for underfloor heating and 0.2 °C for radiators. Learning is skipped for cycles that are disturbed: the target changes, the window opens, the zone is switched off, there was no hot water, or the valve opened while the room sensor was lost.

### Simulation results

`tests/unit/test_simulation.py` runs SmartHeat against a thermal model of a 20 m² room. The model has:

- realistic sensors (lag, 0.1 °C steps, a return pipe that cools when the flow stops)
- a night setback from 21 °C to 19 °C
- changing weather

After the first days of learning, the room peaks this far from the target on average, and at most this far above it (mean / worst):

| | Plain thermostat | SmartHeat | SmartHeat without return sensor |
|---|---|---|---|
| Underfloor heating | 0.60 / 1.22 °C | 0.15 / 0.35 °C | 0.19 / 0.97 °C |
| Radiators | 0.36 / 0.89 °C | 0.08 / 0.17 °C | 0.13 / 0.57 °C |

The learned values match the model: β ≈ 2.0 for the floor (the true capacity ratio is 1.8), and h ≈ 4.1 °C (true 4.2 °C).

The trade-off: because the room peaks *at* the target, its average is a little below the target (about 0.2–0.3 °C in the simulation). The [aim offset](#aim-offset) can lift it.

To print these numbers yourself, run `python tests/unit/test_simulation.py`.

**Loop simulation.** `tests/unit/test_simulation_loop.py` runs an underfloor zone on a more detailed floor: two loops with a 7-minute transit that pass only part of their heat to the screed, a wax actuator that takes 4 minutes to open, a return sensor on the pipe at the manifold, and a 35 °C supply. With all sensors working, the room stays 0.36–0.37 °C from the target on average during the day. Then the room sensor is lost for 24 hours, in three weather runs. With the valve closed, as in SmartHeat 0.3.0, the room cools to 9.9, 4.8 and 4.8 °C. With the duty hold it stays at 20.2–23.5, 18.4–19.4 and 17.6–20.3 °C. To print these numbers, run `python tests/unit/test_simulation_loop.py`.

### Aim offset

The room peaks at the target, so its average sits about 0.2–0.27 °C below it. The setting **Aim offset** moves the whole cycle up, or down if negative: the thermostat opens, closes and learns as if the offset were added to the target, while the target shown stays the same. In the loop simulation, +0.15 °C for an underfloor zone lifts the room's daytime average from 0.18–0.21 °C below the target to 0.01–0.03 °C below it. Its mean distance from the target drops from 0.36–0.37 to 0.31–0.34 °C (10–14 % less), with about as many valve openings (5–6 a day) and less than 1 % more heat. For radiators, leave it at 0 unless the room feels cool.

### Radiators without a return sensor

The return sensor is required for underfloor heating and optional for radiators. A radiator holds little heat, so closing early by the average overshoot works well enough: in the simulation, radiators without a return sensor peak 0.13 °C from the target on average and at most 0.57 °C above it, against 0.08 / 0.17 °C with one and 0.36 / 0.89 °C with a plain thermostat. A floor holds much more heat, and how much varies, so without the return temperature its worst peaks stay close to a plain thermostat's (0.97 against 1.22 °C).

A radiator zone without a return sensor does without:

- the learned β and h: it always closes early by the average overshoot
- the safety close at the maximum return temperature
- the frost protection backup: if the room sensor is gone, the valve closes after the room sensor timeout and stays closed until the sensor is back
- loop saturation detection (supply and return almost equally warm); a supply sensor alone still notices when no hot water is available
- the heat storage factor, holding difference and supply-return difference sensors

## Installation

SmartHeat installs through [HACS](https://hacs.xyz):

1. In HACS, open the menu ⋮ → **Custom repositories**, add `https://github.com/Kodestuen/HomeAssistant-SmartHeat`, and pick the type **Integration**.
2. Install **SmartHeat** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → SmartHeat**. Each zone is added separately.

## Settings

The system type sets the defaults. Every setting can be changed later under the zone's **Configure** button, along with the entities. A radiator zone can add or remove its return sensor there too.

| Setting | Underfloor | Radiators |
|---|---|---|
| Hysteresis (opens this far below target) | 0.3 °C | 0.3 °C |
| Aim offset (the thermostat aims this far above the target) | 0 °C | 0 °C |
| Minimum open / closed time | 15 / 15 min | 5 / 5 min |
| Coast window (longest wait for the peak) | 240 min | 60 min |
| Maximum return temperature (with a return sensor) | 45 °C | 75 °C |
| Room sensor timeout | 30 min | 30 min |
| Room sensor silent after (0 = off) | 6 h | – |
| Frost protection | 7 °C | 7 °C |
| Eco / away preset | 19 / 16 °C | 19 / 16 °C |
| Inverted valve (normally open actuator) | off | off |

## Entities

Each zone is a device with one climate entity. When you add a zone, its device is placed in the room sensor's area (the sensor's own area, or else that of its device). This happens only once, so you can move the zone to another area afterwards.

- **Modes:** heat and off.
- **Presets:** comfort (`none`), eco, away, frost protection. The `comfort_temperature` attribute holds the comfort target, also while another preset is active.
- **Attributes:** these explain what the controller is doing.
  - `reason`: why the valve is open or closed right now
  - `fallback`: `room_sensor` while an underfloor zone follows the last day's average because its room sensor is lost ([rule 6](#how-it-works)), otherwise null. `fallback_duty` is then the share of each 45 minutes that the valve opens, in percent, rounded as in rule 6: 0 means the valve stays closed.
  - `room_silent` (underfloor zones): true while the room sensor counts as lost because it hasn't sent a new reading for the **Room sensor silent after** time
  - `predicted_overshoot` and `close_threshold` (which includes the aim offset)
  - `trained`: true once β and h are learned; the prediction then uses them whenever the return temperature is known, and the plain average otherwise. Always false without a return sensor.
  - `coasting`
  - `return_temperature` (null without a return sensor)
  - with a supply sensor, also: `supply_temperature` and `no_heat_source`, plus `supply_return_delta` and `loop_saturated` when the zone also has a return sensor
- **Configuration attributes:** how the zone is set up, for dashboards and templates. They only change with the settings, so the recorder leaves them out.
  - `system_type`: `underfloor` or `radiator`
  - `valve_entity_id`, `room_sensor_entity_id`, `return_sensor_entity_id`, `supply_sensor_entity_id` and `window_sensor_entity_id` (null when the zone has no such sensor)
  - `invert_valve`
  - the tuning, under the option names: `hysteresis`, `aim_offset`, `min_open_minutes`, `min_closed_minutes`, `coast_window_minutes`, `max_return_temp`, `sensor_timeout_minutes`, `eco_temp`, `away_temp` and `frost_temp`, plus `room_sensor_silent_hours` for underfloor zones

Each zone also has diagnostic sensors. Graph them to watch the learning:

| Sensor | Meaning |
|---|---|
| Learned overshoot | Average overshoot measured after closing |
| Heat storage factor | β; only with a return sensor |
| Holding difference | h; only with a return sensor |
| Learning cycles | Number of cycles learned from |
| Last peak vs target | How far the last peak landed from the target it aimed at (the target plus the aim offset); should settle near 0 |
| Heat-up rate | Room warm-up rate while the valve is open (°C/h) |
| Valve open (24 h) | Share of the last 24 hours the valve was open |
| Supply-return difference | Only with both a supply and a return sensor |

Removing the supply or return sensor in the settings also removes the sensors that need it. Adding it back brings them back under the same entity IDs, so their history continues.

The service `smartheat.reset_learning` makes a zone forget the overshoot it has learned. It keeps the share of the time the valve was open over the last day, which an underfloor zone needs if its room sensor is lost.

## Heating dashboard

`dashboard/heating_dashboard.mjs` builds a **Heating** dashboard for your zones and saves it in Home Assistant. It finds the zones with their sensors and areas, and makes an overview with a tile per zone, a page per zone with its charts and settings, and, with two or more zones, a page that compares them. It needs Node.js 22 or newer and a long-lived access token of an admin user:

```
node dashboard/heating_dashboard.mjs --url http://homeassistant.local:8123 --token-file ha.token.txt
```

- Add `--dry-run` to see the dashboard without saving anything.
- `HA_URL` and `HA_TOKEN` can replace `--url` and `--token-file`. The token is never printed or saved, and files ending in `.token.txt` are ignored by git.
- If the dashboard was edited in Home Assistant since the last run, the script stops instead of overwriting the edits. `--force` overwrites them.
- Run it again after adding, removing or renaming a zone, after changing a zone's sensors, or after upgrading SmartHeat.

All options are described in [dashboard/README.md](dashboard/README.md).

## Trying it without hardware

You can drive a test zone from fake entities:

1. Create an `input_boolean` helper to act as the valve.
2. Create the temperatures as states through the REST API, for example:

   ```
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"state": "20.4", "attributes": {"unit_of_measurement": "°C", "device_class": "temperature"}}' \
     http://homeassistant.local:8123/api/states/sensor.smartheat_test_room
   ```

3. Add a SmartHeat zone on these entities.
4. Change the values and watch the valve and the `reason` attribute.

States created this way disappear when Home Assistant restarts.

## Development

The control logic (`controller.py`) and the learning (`learning.py`) are pure Python, so they can be tested without Home Assistant:

```
python -m venv .venv
.venv/bin/pip install pytest        # .venv\Scripts\pip on Windows
.venv/bin/python -m pytest tests/unit
```

The Home Assistant tests in `tests/ha` need the HA test harness, which needs Python 3.14. They run in GitHub Actions together with hassfest and the HACS validation:

```
pip install -r requirements_test.txt
pytest
```
