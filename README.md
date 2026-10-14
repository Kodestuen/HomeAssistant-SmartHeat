# SmartHeat

SmartHeat is a Home Assistant integration for water-based heating zones (underfloor heating or radiators) that are controlled by an on/off valve. It works like a thermostat, but it learns how far the room keeps warming after the valve closes, and closes the valve early by exactly that much. The room then peaks at the target instead of overshooting it.

It was inspired by [Better Thermostat](https://github.com/KartoffelToby/better_thermostat), but it controls a valve directly and uses the water temperature to understand how much heat is still stored in the floor or radiator.

Underfloor zones can instead try [tickle pulses](#tickle-pulses-experimental), an experimental control method: short openings at fixed times, each ended when the return water is warm enough.

## How it works

One SmartHeat zone uses:

| Entity | Required | Purpose |
|---|---|---|
| Valve (`switch`, `input_boolean` or `valve`) | yes | Lets hot water into the zone |
| Room temperature sensor | yes | What the zone controls |
| Return temperature sensor | for underfloor heating | Water temperature at the end of the loop, on the return pipe; optional for radiators ([see below](#radiators-without-a-return-sensor)) |
| Supply temperature sensor | no | Water going in; used to notice when no hot water is available |
| Window or door sensor | no | The valve stays closed while it is open |
| Outdoor temperature (a sensor or a weather entity) | no | Underfloor zones only; used by [tickle pulses](#tickle-pulses-experimental) |

The control rules of the thermostat, which every zone starts with:

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

The trade-off: because the room peaks *at* the target, its average is a little below the target (about 0.2–0.3 °C in this simulation). The [aim offset](#aim-offset) can lift it.

To print these numbers yourself, run `python tests/unit/test_simulation.py`.

**Loop simulation.** `tests/unit/test_simulation_loop.py` runs an underfloor zone on a more detailed floor: two loops with a 7-minute transit that pass only part of their heat to the screed, a wax actuator that takes 4 minutes to open, a return sensor on the pipe at the manifold, and a 35 °C supply. With all sensors working, the room stays 0.36–0.37 °C from the target on average during the day. Then the room sensor is lost for 24 hours, in three weather runs. With the valve closed, as in SmartHeat 0.3.0, the room cools to 9.9, 4.8 and 4.8 °C. With the duty hold it stays at 20.2–23.5, 18.4–19.4 and 17.6–20.3 °C. The same floor also runs the tickle pulses: see [their results](#loop-simulation-results). To print these numbers, run `python tests/unit/test_simulation_loop.py`.

### Aim offset

The room peaks at the target, so its average sits below it: 0.18–0.21 °C in the loop simulation, and 0.20–0.27 °C on the two floor models of the [tickle pulses' study](#pros-and-cons). The setting **Aim offset** moves the whole cycle up, or down if negative: the thermostat opens, closes and learns as if the offset were added to the target, while the target shown stays the same. In the loop simulation, +0.15 °C for an underfloor zone lifts the room's daytime average to 0.01–0.03 °C below the target. Its mean distance from the target drops from 0.36–0.37 to 0.31–0.34 °C (10–14 % less), with about as many valve openings (5–6 a day) and less than 1 % more heat. For radiators, leave it at 0 unless the room feels cool.

### Radiators without a return sensor

The return sensor is required for underfloor heating and optional for radiators. A radiator holds little heat, so closing early by the average overshoot works well enough: in the simulation, radiators without a return sensor peak 0.13 °C from the target on average and at most 0.57 °C above it, against 0.08 / 0.17 °C with one and 0.36 / 0.89 °C with a plain thermostat. A floor holds much more heat, and how much varies, so without the return temperature its worst peaks stay close to a plain thermostat's (0.97 against 1.22 °C).

A radiator zone without a return sensor does without:

- the learned β and h: it always closes early by the average overshoot
- the safety close at the maximum return temperature
- the frost protection backup: if the room sensor is gone, the valve closes after the room sensor timeout and stays closed until the sensor is back
- loop saturation detection (supply and return almost equally warm); a supply sensor alone still notices when no hot water is available
- the heat storage factor, holding difference and supply-return difference sensors

## Tickle pulses (experimental)

An underfloor zone can run on tickle pulses instead of the thermostat. They are experimental: so far they have only been tested in simulation, and a zone uses them only once you choose them under **Configure → Control method**. Radiator zones always use the thermostat.

**The idea.** Instead of long heating periods, the valve opens for short pulses at fixed times, 45 minutes apart. Each pulse lets warm water into the floor until the water coming back, the return, is warm enough. Then the valve closes until the next pulse time. How warm is warm enough depends on the target, the outdoor temperature and the room, plus an offset that SmartHeat calibrates slowly so that the room settles at the target.

**Why the literal version fails.** Taken literally, the idea stops each pulse when the return is a fixed margin above the *room* temperature. While water flows, though, the return reads the floor's temperature plus about 37 % of the difference between the supply and the floor. So if the stop point follows the room, a room that is 1 °C too warm lets the floor run 1.6 °C warmer. The floor then gives about 120 W more heat, while the room loses only 25–40 W more. That is positive feedback: the warmer the room, the more it gets heated. In simulation this kept the room 0.90–1.75 °C from the target on average, against 0.36–0.37 °C for the thermostat, with swings of +3.0 / −4.6 °C. No interval from 30 to 120 minutes, adaptive interval, faster calibration or outdoor temperature fixed it.

**What works instead:**

- The return target follows the *set* target, not the measured room.
- The room still counts, as a brake and to calibrate: a room that is too warm lowers the return target a lot (3 °C per °C), a pulse is skipped when the room is 0.3 °C warm, and a slow offset is calibrated from the room's error. Ending pulses on the return alone kept the room 0.63–1.81 °C from the target.
- A fixed interval of 45 minutes, with a minimum pulse of 12 minutes. A pulse that hasn't finished by the next pulse time carries on. "Optimal" intervals that adapt made things worse.

Most of the gain comes from the short, frequent pulses with a slow correction, not from the return sensor as such. A plain time-based pulse control on the room did almost as well, 0.244 against 0.224 °C, but moved the valve 33 times a day against 17: ending each pulse at the return target is what limits the dose. A Danfoss patent family (priority 2006, expired) describes nearly the same scheme.

### The rules

1. **Safety first**, as for the thermostat: the valve closes above the maximum return temperature, while the window is open and when the zone is off, and it opens for frost protection. These override everything below, including the minimum pulse.
2. **Pulse times.** A pulse may start every 45 minutes (**Pulse interval**), each zone at minutes of its own, so that the zones don't all ask for heat at once. After a restart the valve waits for the next pulse time. A target rise of 0.5 °C or more, such as a morning schedule, adds one pulse time: a few minutes later in each zone, at most once per interval, and only once the valve has been closed for 10 minutes.
3. **Start.** At a pulse time the valve opens, unless the room is 0.3 °C or more above the target. In mild weather it also stays closed when the room is at the target and the valve was open less than a minimum pulse per interval over the last day: a whole pulse would overheat the room.
4. **Minimum pulse.** The valve then stays open for at least 12 minutes (**Minimum pulse**): long enough for the actuator to open and for the water that was in the floor to reach the return sensor. Only a room 0.6 °C above the target ends it sooner.
5. **Stop.** After that, the valve closes once the return reaches the return target

   ```
   T = target + c + 0.15 · (target − outdoor) − 3 · (room − target)
   ```

   or, without an outdoor temperature, `T = target + c − 4 · (room − target)`. T stays at least 2 °C below the maximum return temperature.
6. **Carry on.** A pulse that hasn't reached T by the next pulse time carries on, unless the room is 0.3 °C warm by then. There is no longest pulse, so a cold floor can heat through a cold spell or a morning warm-up.
7. **Calibration.** The return offset c moves 0.1 °C an hour for each °C the room is off the target: down while the room is too warm, up while it is too cold. It moves only while the room is within 1 °C of the target in comfort or eco, both sensors work, no fallback or safety rule is active, no window has closed in the last hour, and the loop has hot water and isn't saturated (supply and return almost equally warm). While the **Room sensor silent after** check is on, the room sensor must also have reported in the last hour, as for the thermostat's average. It starts from the holding difference h that the thermostat learned, once it has 3 or more cycles, so that T starts near target + h. Without that it starts at 4.35 °C, or 7.5 °C without an outdoor temperature. c is saved, so it survives restarts.
8. **Outdoor temperature.** Optional: a temperature sensor or a weather entity. The last reading is saved and used for up to 48 hours. After that, or without an outdoor entity, the outdoor term is left out and c takes it over, so that T doesn't jump.

**When a sensor is lost** the zone keeps heating on the same pulse times. A fallback starts once a sensor has been missing for 2 minutes (until then the valve stays as it is, and a pulse time that passes meanwhile counts once the wait is over), and it opens the valve only at a pulse time:

- **Return sensor missing**, or not responding: timed pulses. Each opens for a share of the interval that follows the room, starting from the share of the time the valve was open over the last day (`fallback: return_sensor`). A return sensor counts as not responding when it hasn't changed by 0.05 °C within the minimum pulse + 15 minutes of an opening, until it changes again.
- **Room sensor missing**, or silent for the **Room sensor silent after** time: pulses end at T without the room's term, and last at most the last day's share of each interval. c waits for the room (`fallback: room_sensor`). As in the duty hold, the share of the day before can't follow the weather. It keeps a day that turns warmer from overheating the room, but a day that turns colder runs cool: in the [loop simulation](#loop-simulation-results) the room then sat 1.7–2.8 °C below the target during the day, against 1.5–2.3 °C with the duty hold.
- **Both missing**: the valve opens for the last day's share of each interval (`fallback: sensors`).
- Timed openings are rounded as in the thermostat's duty hold: under 2.5 minutes they are left out, under 5 minutes they become 5, and when less than 5 minutes would be left closed, the valve stays open for the whole interval.

### Defaults

| Setting | Default | Can be changed |
|---|---|---|
| Pulse interval | 45 min | 30–120 min |
| Minimum pulse | 12 min | 5–30 min, and at most the interval − 15 min |
| Outdoor temperature | none | a temperature sensor or a weather entity |
| A pulse is skipped when the room is | 0.3 °C above the target | no |
| Return target per °C the room is too warm | −3 °C (−4 °C without an outdoor temperature) | no |
| Return target per °C between target and outdoors | +0.15 °C (0 without an outdoor temperature) | no |
| Calibration of c | 0.1 °C an hour per °C of room error, within 1 °C of the target | no |
| Highest return target | maximum return temperature − 2 °C | no |
| Target rise that adds a pulse time | 0.5 °C | no |
| Outdoor reading kept | 48 h | no |
| Sensor missing before a fallback | 2 min | no |

The thermostat's hysteresis and minimum open time still drive frost protection. The aim offset, minimum closed time, coast window and room sensor timeout are not used.

### Pros and cons

All numbers come from simulation: two independent floor models, each with 8 scenarios and 22 stress cases, and the [loop simulation](#loop-simulation-results) in this repository. The defaults were chosen on the same models, so treat them as a prediction until a real floor confirms them.

**Pros**

- Less comfort error: about 40 % less in cold weather and 20–40 % less in mild weather. In the loop simulation the room stays 0.14–0.16 °C from the target on average at a 0 °C mean, against 0.36–0.37 °C for the thermostat and 0.31–0.34 °C for the thermostat with a +0.15 °C aim offset. At a +10 °C mean it is 0.22–0.26 °C against 0.26–0.34 °C.
- The room is at the target on average, where the thermostat's room sits 0.2–0.27 °C below it.
- Fewer large deviations: 95 % of the time the room is within 0.71 °C of the target, against 0.89 °C.
- It keeps heating when a sensor fails. In the loop simulation a day without the room sensor kept the room at 17.8–22.0 °C (17.5–24.2 °C at a +10 °C mean), and a day without the return sensor at 18.1–22.0 °C.
- Half the valve movements of a time-based pulse control that does as well.
- It copes with many loop and sensor details without retuning: transit times of 3–12 minutes, actuators that take from 10 seconds to 9 minutes, 0.5 °C sensor steps, sensor noise, rooms of 8–40 m², and 11 Home Assistant restarts in 12 days.
- The return water is 0.3–0.65 °C cooler, which helps with a district heating return tariff and with condensing boilers.
- A morning warm-up is a little faster: 201 against 228 minutes to reach 20.8 °C.

**Cons**

- Three to four times more valve movements: 15–27 a day against 4–6, about 3,000–5,000 per zone in a heating season. No actuator maker publishes how many their actuators last.
- Every pulse asks for heat, so a heat pump or boiler may start more often. This was not simulated.
- About 1–1.5 % more heat, because the room is held at the target instead of a little below it.
- It needs a good return sensor (see below).
- It works best with an outdoor temperature. Without one it still beats the thermostat in cold weather (0.22–0.25 °C from the target), but in mild weather it does no better (0.27–0.34 °C against 0.26–0.34 °C) and keeps the room 0.09–0.15 °C warm.
- It is weak when the supply temperature follows a heating curve, as with many heat pumps: 0.24 / 0.35 °C on the two models, against 0.40 / 0.44 °C for the thermostat and 0.15 / 0.24 °C for a plain time-based pulse control. On one model a thermostat aimed 0.25 °C higher did as well.
- No control method fixes an undersized loop, a supply that is too cold in a cold spell, or strong sun: every one tested was 0.5–0.9 °C off there, and 600 W of sun still overshot by 1.0–1.6 °C.
- More rules, settings and ways to fail than the thermostat.

### When not to use it

- The supply temperature follows a heating curve, as with many heat pumps. The tickle pulses were tuned on a fixed supply temperature, such as one set by a mixing shunt.
- The return sensor is coarse or slow, like many Zigbee sensors at their defaults: steps of 0.5–1 °C, or one report an hour.
- The heat source must not start often. Each pulse asks for heat, up to 15–27 times a day per zone.
- The return sensor also measures other loops, for example one on the manifold's common return. SmartHeat doesn't let a zone on tickle pulses share its return sensor with another SmartHeat zone, whichever of the two is set up last.

### The return sensor

- On the loop's own return pipe, strapped on and insulated; not on the manifold's common return.
- Reporting every minute or faster, in steps of 0.1–0.2 °C. A DS18B20 on a Shelly add-on with a 0.1 °C report threshold works, for example.
- The **Minimum pulse** must cover the time from the opening until warm water reaches it.

### Trying it

1. Run the zone on the thermostat for a week first, so it learns the holding difference h. The pulses start from it: in simulation, the error in the first days was 0.20 °C instead of 0.27 °C. Without that start, a floor whose pipes pass their heat to the screed very well may run about 0.2 °C warm for the first 2–3 weeks, while c settles.
2. Under the zone's **Configure**, set **Control method** to **Tickle pulses (experimental)**. Choose an **Outdoor temperature** if you have one: a sensor, or a weather entity such as `weather.forecast_home`.
3. Check the **Minimum pulse**. Watch a few pulses in the return temperature's history: the time from the opening until the return starts to rise, plus about 2 minutes, is enough.
4. Count the valve openings per day with a **History stats** helper on the valve (**Settings → Devices & services → Helpers → Create helper → History stats**: type Count, state `on`, or `open` for a `valve` entity, start `{{ today_at() }}`, end `{{ now() }}`). It survives restarts, unlike SmartHeat's own Valve open (24 h) sensor.
5. Compare at least two weeks with the thermostat, also with an aim offset of +0.15 °C: how far the room is from the target, its average, the openings per day, and how often the heat source starts. The gain is largest in cold weather, so compare in December to February, or split the results by outdoor temperature.
6. Run the [dashboard generator](#heating-dashboard) again after switching. A tickle zone's page shows the pulses, the return target and the return offset.

To go back, set the control method to the thermostat again. The thermostat's learned values are kept while the zone runs on pulses.

### Loop simulation results

`python tests/unit/test_simulation_loop.py` prints these numbers. The mean error is the mean distance between the room and the target during the day, from day 4 on, in three weather runs. At a 0 °C mean outdoor temperature:

| Control method | Mean error | Room − target on average | Valve openings a day |
|---|---|---|---|
| Thermostat | 0.36 / 0.36 / 0.37 °C | −0.20 / −0.18 / −0.21 °C | 5.4 / 5.6 / 6.2 |
| Thermostat, aim offset +0.15 °C | 0.31 / 0.32 / 0.34 °C | −0.03 / −0.02 / −0.01 °C | 5.6 / 5.5 / 6.2 |
| Tickle pulses | 0.14 / 0.16 / 0.15 °C | −0.01 / 0.00 / 0.00 °C | 20.2 / 20.6 / 21.1 |
| Tickle pulses without outdoor temperature | 0.23 / 0.25 / 0.22 °C | −0.02 / −0.02 / 0.00 °C | 18.0 / 16.2 / 18.6 |

At a +10 °C mean, when the floor needs little heat:

| Control method | Mean error | Room − target on average | Valve openings a day |
|---|---|---|---|
| Thermostat | 0.34 / 0.31 / 0.26 °C | −0.04 / −0.16 / −0.14 °C | 5.1 / 5.9 / 6.9 |
| Thermostat, aim offset +0.15 °C | 0.34 / 0.25 / 0.22 °C | +0.16 / +0.02 / +0.04 °C | 5.2 / 6.4 / 7.1 |
| Tickle pulses | 0.26 / 0.22 / 0.22 °C | +0.03 / +0.03 / 0.00 °C | 11.8 / 9.9 / 13.6 |
| Tickle pulses without outdoor temperature | 0.34 / 0.27 / 0.33 °C | +0.11 / +0.09 / +0.15 °C | 10.8 / 9.9 / 11.4 |

The true room temperature through a day without a sensor, lowest–highest:

| Control method | Weather run 1 | Weather run 2 | Weather run 3 |
|---|---|---|---|
| Thermostat, valve closes (SmartHeat 0.3.0) | 9.9–20.7 °C | 4.8–19.4 °C | 4.8–19.2 °C |
| Thermostat, duty hold | 20.2–23.5 °C | 18.4–19.4 °C | 17.6–20.3 °C |
| Tickle pulses, room sensor lost | 20.1–22.0 °C | 17.9–19.9 °C | 17.8–20.0 °C |
| Tickle pulses, room sensor lost, +10 °C mean | 20.8–24.2 °C | 17.5–20.3 °C | 18.3–20.3 °C |
| Tickle pulses, return sensor lost | 19.0–22.0 °C | 18.8–21.1 °C | 18.1–21.4 °C |

Without the room sensor, the valve opens for at most the share of the day before, which can't follow the weather. In run 1 the day without the sensor turns warmer, in runs 2 and 3 colder. During that day the room averaged +0.55 / −2.82 / −1.72 °C from the target on tickle pulses, against +2.02 / −2.31 / −1.53 °C with the duty hold (+2.82 / −3.07 / −1.48 °C on tickle pulses at a +10 °C mean). The cap on the tickle's pulses keeps a warm day from overheating the room: without it, the study's review saw this floor reach 25.7 °C at a +10 °C mean. The price is a cool room on a day that turns colder.

Each zone has pulse times of its own, and these runs use one zone's. Over six pulse times, this one among them, the room stayed at 17.7–22.2 °C without the room sensor (17.5–24.7 °C at a +10 °C mean) and at 18.0–22.2 °C without the return sensor.

With 11 Home Assistant restarts in the 12 days, the tickle's mean error changes by less than 0.01 °C.

## Installation

SmartHeat installs through [HACS](https://hacs.xyz):

1. In HACS, open the menu ⋮ → **Custom repositories**, add `https://github.com/Kodestuen/HomeAssistant-SmartHeat`, and pick the type **Integration**.
2. Install **SmartHeat** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → SmartHeat**. Each zone is added separately.

## Settings

The system type sets the defaults. Every setting can be changed later under the zone's **Configure** button, along with the entities. A radiator zone can add or remove its return sensor there too.

| Setting | Underfloor | Radiators |
|---|---|---|
| Control method | thermostat | thermostat (only) |
| Hysteresis (opens this far below target) | 0.3 °C | 0.3 °C |
| Aim offset (the thermostat aims this far above the target) | 0 °C | 0 °C |
| Minimum open / closed time | 15 / 15 min | 5 / 5 min |
| Coast window (longest wait for the peak) | 240 min | 60 min |
| Maximum return temperature (with a return sensor) | 45 °C | 75 °C |
| Room sensor timeout | 30 min | 30 min |
| Room sensor silent after (0 = off) | 6 h | – |
| Outdoor temperature (tickle pulses) | none | – |
| Pulse interval (tickle pulses) | 45 min | – |
| Minimum pulse (tickle pulses) | 12 min | – |
| Frost protection | 7 °C | 7 °C |
| Eco / away preset | 19 / 16 °C | 19 / 16 °C |
| Inverted valve (normally open actuator) | off | off |

Tickle pulses use the hysteresis and minimum open time only for frost protection. They don't use the aim offset, minimum closed time, coast window or room sensor timeout. New zones always start on the thermostat.

## Entities

Each zone is a device with one climate entity. When you add a zone, its device is placed in the room sensor's area (the sensor's own area, or else that of its device). This happens only once, so you can move the zone to another area afterwards.

- **Modes:** heat and off.
- **Presets:** comfort (`none`), eco, away, frost protection. The `comfort_temperature` attribute holds the comfort target, also while another preset is active.
- **Attributes:** these explain what the controller is doing.
  - `reason`: why the valve is open or closed right now. Tickle pulses have their own: `pulse_flush` (the minimum pulse runs), `pulse` (open until the return reaches the return target), `pulse_extended` (a pulse that carried on past a pulse time), `return_reached`, `pulse_skipped` (the room was warm enough), `waiting` (for the next pulse time), `timed_pulse` (a timed fallback opening), and `return_sensor_unavailable` or `room_sensor_unavailable` for the first 2 minutes without a sensor. The safety reasons are the same as the thermostat's.
  - `fallback`: the lost sensor the valve works around, otherwise null. `room_sensor` while an underfloor zone follows the last day's average because its room sensor is lost ([rule 6](#how-it-works)), or while tickle pulses go on without the room; on tickle pulses also `return_sensor` and `sensors` ([fallbacks](#the-rules)). `fallback_duty` is the share of each interval that a timed fallback or the duty hold opens the valve, in percent, rounded as in rule 6: 0 means the valve stays closed. On tickle pulses without the room it is the most the valve opens, since a pulse may end sooner at the return target. It is null when no fallback runs.
  - `room_silent` (underfloor zones): true while the room sensor counts as lost because it hasn't sent a new reading for the **Room sensor silent after** time
  - `predicted_overshoot` and `close_threshold` (which includes the aim offset); null on tickle pulses
  - `trained`: true once β and h are learned; the prediction then uses them whenever the return temperature is known, and the plain average otherwise. Always false without a return sensor.
  - `coasting`: always false on tickle pulses
  - `return_temperature` (null without a return sensor)
  - with a supply sensor, also: `supply_temperature` and `no_heat_source`, plus `supply_return_delta` and `loop_saturated` when the zone also has a return sensor
  - on tickle pulses, also: `return_threshold` (the return target T in °C, null while no pulse uses one, for example during a safety rule or a timed fallback), `next_pulse` (the next pulse time, in UTC, including the extra one after a target rise), `outside_temperature` (the outdoor temperature in use, also a held one; null without one) and `calibrating` (true while the return offset is being adjusted)
- **Configuration attributes:** how the zone is set up, for dashboards and templates. They only change with the settings, so the recorder leaves them out. It also leaves out `return_threshold`, `next_pulse` and `outside_temperature`: the Return target sensor and the outdoor entity keep their history, and the pulse times follow a fixed schedule.
  - `system_type`: `underfloor` or `radiator`
  - `strategy`: the control method, `thermostat` or `tickle`; always `thermostat` for radiators
  - `valve_entity_id`, `room_sensor_entity_id`, `return_sensor_entity_id`, `supply_sensor_entity_id` and `window_sensor_entity_id` (null when the zone has no such sensor), plus `outside_sensor_entity_id` for underfloor zones
  - `invert_valve`
  - the tuning, under the option names: `hysteresis`, `aim_offset`, `min_open_minutes`, `min_closed_minutes`, `coast_window_minutes`, `max_return_temp`, `sensor_timeout_minutes`, `eco_temp`, `away_temp` and `frost_temp`, plus `room_sensor_silent_hours`, `pulse_interval_minutes` and `pulse_flush_minutes` for underfloor zones

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
| Return offset | c of the [tickle pulses](#tickle-pulses-experimental); only on tickle pulses |
| Return target | T, the return temperature that ends the current pulse; only on tickle pulses, unknown while no pulse uses one |

Removing the supply or return sensor in the settings also removes the sensors that need it. Adding it back brings them back under the same entity IDs, so their history continues. In the same way, switching a zone back to the thermostat removes the Return offset and Return target sensors, and switching to tickle pulses again brings them back. The thermostat's sensors stay while a zone runs on tickle pulses, with the values the thermostat learned.

The service `smartheat.reset_learning` makes a zone forget what its control method has learned. On the thermostat it forgets the overshoot. On tickle pulses it only starts the return offset afresh, from the holding difference h the thermostat learned; the thermostat's own values stay, for when the zone switches back. Both keep the share of the time the valve was open over the last day, which an underfloor zone needs if a sensor is lost.

## Heating dashboard

`dashboard/heating_dashboard.mjs` builds a **Heating** dashboard for your zones and saves it in Home Assistant. It finds the zones with their sensors and areas, and makes an overview with a tile per zone, a page per zone with its charts and settings, and, with two or more zones, a page that compares them. It needs Node.js 22 or newer and a long-lived access token of an admin user:

```
node dashboard/heating_dashboard.mjs --url http://homeassistant.local:8123 --token-file ha.token.txt
```

- Add `--dry-run` to see the dashboard without saving anything.
- `HA_URL` and `HA_TOKEN` can replace `--url` and `--token-file`. The token is never printed or saved, and files ending in `.token.txt` are ignored by git.
- If the dashboard was edited in Home Assistant since the last run, the script stops instead of overwriting the edits. `--force` overwrites them.
- Run it again after adding, removing or renaming a zone, after changing a zone's sensors or control method, or after upgrading SmartHeat.

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

The control logic (`controller.py`, and `tickle.py` for the tickle pulses) and the learning (`learning.py`) are pure Python, so they can be tested without Home Assistant:

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
