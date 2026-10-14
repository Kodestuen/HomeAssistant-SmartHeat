# Heating dashboard

`heating_dashboard.mjs` builds a **Heating** dashboard for your SmartHeat zones and saves it to Home Assistant at `/dashboard-heating`. It reads what Home Assistant knows about the zones (their entities, devices, areas and floors, plus the weather), so there is nothing to configure. Run it again when your zones change.

## What the dashboard shows

**With no zones yet**, the page shows a "No heating zones yet" card with an **Add a zone** button, a checklist of what each zone needs, the weather and SmartHeat's update status. When the first zone starts running, the empty state hides itself and a notice asks you to run the generator again.

**With zones:**

- **Overview.** A summary such as "Heating in Stue", with warnings when a room sensor is lost or no hot water arrives. One tile per zone shows the room temperature, whether it is heating, the target and the preset; tap it to open the zone. With two or more zones, whole-house **Comfort**, **Eco** and **Away** buttons set every zone at once. The side column has the weather, the outdoor temperature (see the helper below), SmartHeat updates and links.
- **Zone pages**, one per zone: a sentence saying what the controller is doing, the thermostat, the room temperature against the target, the controller's numbers, the valve and sensors, the water temperatures, what SmartHeat has learned with charts over 14 and 30 days, the holding difference against the outdoor temperature, and settings with **Reset learning**.
- **Compare zones**, with two or more zones: the learning and valve charts of all zones side by side.
- **All rooms**: Home Assistant's own climate view of every room. It updates itself, so a new zone appears there before you run the generator again.

A notice at the top appears whenever the zones running in Home Assistant differ from the ones the page was built for.

The "Sensors and valve" and "Water temperatures" sections need SmartHeat 0.2.0 or newer. With 0.1.0 they are left out, and the rest of the page works.

Since SmartHeat 0.3.0, a radiator zone may have no return sensor. Its page then leaves out everything that needs the return temperature: the return water temperature, the supply-return difference, the loop saturated status, the maximum return temperature setting, and the heat storage factor and holding difference with their charts. Its controller table always shows the prediction as "Average of past cycles", and the water temperatures chart appears only when the zone has a supply sensor.

Since SmartHeat 0.3.1, an underfloor zone keeps heating when its room sensor is lost: its valve opens for the same share of every 45 minutes as it was open, on average, over the last day. The overview then warns "room sensor lost, heating by the last day's average", and the zone page shows that share and the decision "Room sensor lost: heating by average". If the valve was hardly open over the last day, as in mild weather, the share is 0 %: the valve stays closed until the sensor is back, and the overview warns "room sensor lost, valve closed" instead. A radiator zone still closes its valve when its room sensor is lost.

The controller table of an underfloor zone has a Room sensor row: Has a reading, No reading, or Silent when the sensor has not sent a new reading for the "Room sensor silent after (0 = off)" time shown under Current tuning. Helpers such as template sensors, which report only when one of their sources changes, and entities without a unique ID are never counted as silent.

Since SmartHeat 0.4.0, a thermostat zone shows its **aim offset** under Current tuning: the thermostat aims that much above the target, or below it if the offset is negative. The sentence at the top of the zone page mentions an offset that is not 0, and the "Warm enough" sentence includes it in the temperature where the valve opens again.

An underfloor zone can also run on the experimental **tickle pulses** (Configure → Control method). Its page then follows the pulses:

- The sentence at the top says what the pulse is doing: filling the loop, heating until the return water reaches the return target, or waiting for the next pulse, with its time.
- The controller table shows the control method, the return target, the return offset, the outdoor temperature in use, the next pulse, whether SmartHeat is calibrating the offset, and any fallback. The thermostat's prediction rows are left out.
- The water temperatures chart adds the return target, so you can see each pulse end there. "Return offset, last 14 days" replaces the overshoot chart. What SmartHeat has learned starts with the return offset and return target; the thermostat's values stay as they were.
- Sensors and valve shows the zone's outdoor temperature: a sensor's state, or a weather entity's temperature.
- Current tuning shows the pulse interval and the minimum pulse. The hysteresis and minimum open time stay, because frost protection still uses them. The other thermostat settings and the aim offset are left out.
- Reset learning only resets the return offset. It starts again from what the thermostat learned.

A tickle zone keeps heating when it loses a sensor. The overview then warns "room sensor lost, pulsing on the return temperature", "return sensor lost, timed pulses" or "room and return sensors lost, heating by the last day's average". The zone page says how much of each interval the valve opens; without the room sensor that is the most it opens, as the return target may end a pulse sooner. Without the room sensor the share is that of the last day, so if the valve was hardly open over the last day, it stays closed until the sensor is back, and the overview warns "room sensor lost, valve closed" or "room and return sensors lost, valve closed" instead.

## Requirements

- Node.js 22 or newer. No npm packages are needed.
- A long-lived access token of an admin user. In Home Assistant, open your profile, then **Security → Long-lived access tokens → Create token**. Save it in a file such as `ha.token.txt` in the repository root (files ending in `.token.txt` are git-ignored), or put it in the `HA_TOKEN` environment variable.

## Usage

Check first, then save:

```
node dashboard/heating_dashboard.mjs --url http://homeassistant.local:8123 --token-file ha.token.txt --dry-run
node dashboard/heating_dashboard.mjs --url http://homeassistant.local:8123 --token-file ha.token.txt
```

| Flag | Meaning |
|---|---|
| `--url <url>` | Home Assistant's address. `HA_URL` works too. |
| `--token-file <file>` | The file with the token. `HA_TOKEN` works too. The token is never printed or written anywhere. |
| `--dry-run` | Build and check the config and print it. Nothing is saved, and the dashboard is not created. |
| `--out <file>` | Also write the config to this file. A dry run or a `--snapshot` run then writes it there instead of printing it. |
| `--force` | Save even when the edit guard stops the run. A failed check or template still stops it. |
| `--state-file <file>` | Where the last saved config is kept for the edit guard. The default is `dashboard/heating-dashboard.last.json`. File names ending in `.last.json` are git-ignored. |
| `--snapshot <file>` | Build and check offline from a saved snapshot, and print the config. Nothing is saved. |
| `--save-snapshot <file>` | Also write the data fetched from Home Assistant to this file, for debugging. It holds your Home Assistant data, so keep it private. File names ending in `.snapshot.json` are git-ignored. |

A run goes like this:

1. Connect and read the states and registries.
2. Build the dashboard and check it: every entity exists and is enabled, only known cards and options are used, and every icon and link is valid.
3. Render every template through Home Assistant in strict mode. If a check or a template fails, the run stops here and saves nothing, even with `--force`.
4. Create the dashboard if it does not exist yet. An existing dashboard keeps its title, icon and sidebar setting.
5. Apply the edit guard, save the config and write the state file.

The script prints the zones it found, any warnings and whether it saved. It exits with 0 on success, 1 when something failed or the edit guard stopped it, and 2 for a wrong command line, such as a missing flag, an unreadable token file or an address that does not start with `http://` or `https://`.

## The edit guard

Each run replaces the whole dashboard, so changes made in the UI would be lost. To prevent that, the script keeps a copy of what it saved in `dashboard/heating-dashboard.last.json`. Before saving, it compares that copy with what Home Assistant holds. If they differ, the dashboard was edited in the UI, and the run stops. Run it again with `--force` to overwrite those edits.

The guard also stops a run when the state file is missing but the dashboard already has a config, for example on another computer. If Home Assistant already holds exactly the new config, nothing is overwritten: the run saves nothing and only writes the state file, even when it was missing.

## When to run it again

- A zone is added, removed or renamed. The notice on the page reminds you.
- A zone's options change. After switching the control method, the notice on the page reminds you too.
- SmartHeat is upgraded.
- The Outdoor temperature helper is created.
- A diagnostic sensor is enabled or disabled.
- Areas or floors are reordered, or a zone moves to another area.

## Optional: an Outdoor temperature helper

With an outdoor temperature sensor, the overview gets an outdoor temperature tile, and each zone page with a return sensor gets a chart of the holding difference against the outdoor temperature. Create the helper early: the chart uses long-term statistics, which take days to build up.

1. Go to **Settings → Devices & services → Helpers → Create helper → Template → Sensor**.
2. Name it `Outdoor temperature`. The generator finds the helper by this name.
3. State template: `{{ state_attr('weather.forecast_home', 'temperature') }}`. Use your own weather entity if it has another name.
4. Unit of measurement `°C`, device class **Temperature**, state class **Measurement**. The state class is required: the charts use the daily averages that Home Assistant keeps only for measurements. Without it, the generator leaves the helper out and warns.
5. Under **Additional options**, set the availability template to `{{ state_attr('weather.forecast_home', 'temperature') is number }}`.
6. Leave the device empty. Linking the helper to the weather device would change its entity ID.
7. Submit, then run the generator again.

## Optional: room temperatures in All rooms

In each room's area settings, choose the zone's room sensor as the **Temperature sensor**. All rooms then shows a temperature trend per room, and after the next run the Rooms heading on the overview links to All rooms.

## Notes

- Everyone in the household can use the dashboard. The **Add a zone** and **Settings** shortcuts on the overview and each zone's Settings section with Reset learning are shown to admins only, because Home Assistant's settings pages only open for admins. The **Add a zone** button on the "No heating zones yet" card is shown to everyone.
- All labels are in English.
- Run the tests with `node --test "dashboard/*.test.mjs"`. They use invented data and a small fake Home Assistant, so they need no Home Assistant.
