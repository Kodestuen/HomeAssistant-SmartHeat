"""Closed-loop simulation of an underfloor zone with a realistic water loop.

test_simulation.py lets the water leave the floor at the floor's temperature, at once.
This plant is closer to a real floor:

- two loops with 28 l of water at 4 l/min, so a 7 min transit, that pass only part of
  their heat to the screed: the water leaves well above the screed temperature;
- a wax actuator that waits 2 min before it starts to open and takes 2 more to open
  fully, and 2.5 + 2 min to close;
- a return sensor strapped to the pipe at the manifold: it lags 90 s, and when the flow
  stops the pipe drifts toward the room temperature + 2 °C;
- a room sensor that lags 10 min; both report in 0.1 °C steps;
- a constant 35 °C supply, and the weather and night setback of test_simulation.py.

The zone runs SmartHeat's real controller with the installed underfloor defaults. The
tests take the room sensor away for 24 h: SmartHeat 0.3.0 closed the valve for good
and the room cooled far below the target; the duty hold keeps it warm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
import math

import pytest

if __name__ == "__main__":
    import conftest  # noqa: F401  registers smartheat_core when run as a script

from test_simulation import TARGET, target_at, weather

from smartheat_core.controller import Controller, ControllerConfig, Inputs
from smartheat_core.learning import OvershootModel, ZoneCalibration

DAY = 86400
CONTROL_STEP_S = 60
PHYSICS_STEP_S = 20
SUPPLY = 35.0
WATER_HEAT = 4186.0  # J/K per litre
DAYS = 12
MEASURE_FROM_DAY = 4
OUTAGE = (6 * DAY, 7 * DAY)  # the room sensor is unavailable from day 6, 00:00
PHASES = (0.0, 1.3, 2.6)  # where in its 5-day cycle the weather starts


class FloorPlant:
    """Screed and room, the water in the floor loops, and a wax actuator.

    Capacities in J/K, conductances and flows in W/K, times in seconds. The water runs
    through 12 mixed segments in the screed, then the pipe to the manifold, which holds
    5 % of it.
    """

    c_screed = 1.8e6  # 5 cm screed of a 20 m² room
    c_room = 1.0e6
    u_screed_room = 200.0
    u_room_outside = 40.0
    full_flow = 280.0  # 2 loops at 2 l/min
    ua_water_screed = 280.0  # NTU 1 at full flow
    segments = 12
    # The wax actuator: once powered, no flow for 2 min, then full flow 2 min later;
    # once the power is cut, full flow for 2.5 min more, then none 2 min later
    open_wait_s = 120.0
    stroke_s = 120.0
    close_wait_s = 150.0
    tail_drift_s = 1200.0  # without flow, the pipe at the manifold drifts ...
    tail_offset = 2.0  # ... toward the room temperature + this

    def __init__(self, outside: float) -> None:
        water = 28.0 * WATER_HEAT
        self.c_tail = 0.05 * water
        self.c_segment = 0.95 * water / self.segments
        self.ua_segment = self.ua_water_screed / self.segments
        # Actuator travel in strokes: the flow starts after the opening wait
        self.travel = 0.0
        self.max_travel = 1 + (self.open_wait_s + self.close_wait_s) / self.stroke_s
        # A steady room at 20.5 °C in this weather, with standing water
        self.room = 20.5
        self.screed = self.room + (
            self.u_room_outside * (self.room - outside) / self.u_screed_room
        )
        self.water = [self.screed] * self.segments
        self.tail = 22.5

    def step(self, energized: bool, dt: float, outside: float) -> None:
        move = dt / self.stroke_s
        self.travel = min(
            max(self.travel + (move if energized else -move), 0.0), self.max_travel
        )
        flow = min(max(self.travel - self.open_wait_s / self.stroke_s, 0.0), 1.0)
        carried = flow * self.full_flow * dt
        exchanged = self.ua_segment * dt
        screed, room = self.screed, self.room
        # Implicit upwind steps: stable at any flow, and they conserve energy
        upstream, total = SUPPLY, 0.0
        for i, temperature in enumerate(self.water):
            temperature = (
                self.c_segment * temperature + carried * upstream + exchanged * screed
            ) / (self.c_segment + carried + exchanged)
            self.water[i] = upstream = temperature
            total += temperature
        to_screed = exchanged * (total - self.segments * screed)
        drift = self.c_tail / self.tail_drift_s * dt
        self.tail = (
            self.c_tail * self.tail
            + carried * upstream
            + drift * (room + self.tail_offset)
        ) / (self.c_tail + carried + drift)
        screed_to_room = self.u_screed_room * (screed - room)
        loss = self.u_room_outside * (room - outside)
        self.screed += (to_screed - screed_to_room * dt) / self.c_screed
        self.room += (screed_to_room - loss) * dt / self.c_room


@dataclass
class Sensor:
    """Follows the true temperature with a lag, and reports it in 0.1 °C steps."""

    value: float
    lag_s: float

    def follow(self, true_value: float, dt: float) -> None:
        self.value += (true_value - self.value) * (1 - math.exp(-dt / self.lag_s))

    def read(self) -> float:
        return round(self.value, 1)


@dataclass
class Result:
    comfort_errors: list[float] = field(default_factory=list)  # room - target, daytime
    valve_changes: list[float] = field(default_factory=list)
    outage_min: float = math.inf  # the true room while the sensor is lost
    outage_max: float = -math.inf

    @property
    def mean_abs_error(self) -> float:
        return sum(abs(e) for e in self.comfort_errors) / len(self.comfort_errors)


def zone_config(hold: bool) -> ControllerConfig:
    """The installed underfloor defaults, with or without what 0.3.1 adds."""
    return ControllerConfig(
        hysteresis=0.3,
        min_open_s=900,
        min_closed_s=900,
        coast_window_s=4 * 3600,
        max_return=45.0,
        lost_room_hold=hold,
        room_silent_s=6 * 3600 if hold else None,
    )


@cache
def simulate(phase: float, hold: bool, outage: bool = False) -> Result:
    """Run the zone for 12 days; measure the true room from day 4 on."""
    plant = FloorPlant(weather(phase))
    room_sensor = Sensor(plant.room, lag_s=600)
    return_sensor = Sensor(plant.tail, lag_s=90)
    controller = Controller(
        zone_config(hold),
        OvershootModel(initial_overshoot=0.4),
        ZoneCalibration() if hold else None,
    )
    result = Result()
    valve, changed_at = False, -1e9

    for now in range(CONTROL_STEP_S, DAYS * DAY + 1, CONTROL_STEP_S):
        # The last minute, in physics steps
        for t in range(now - CONTROL_STEP_S + PHYSICS_STEP_S, now + 1, PHYSICS_STEP_S):
            plant.step(valve, PHYSICS_STEP_S, weather(t / DAY + phase))
            room_sensor.follow(plant.room, PHYSICS_STEP_S)
            return_sensor.follow(plant.tail, PHYSICS_STEP_S)

        lost = outage and OUTAGE[0] <= now < OUTAGE[1]
        target = target_at(now)
        decision = controller.update(
            Inputs(
                room=None if lost else room_sensor.read(),
                return_temp=return_sensor.read(),
                target=target,
                valve_open=valve,
                valve_changed_at=changed_at,
                room_updated_at=now,  # reports every minute
            ),
            now,
        )
        if decision.valve_open != valve:
            valve, changed_at = decision.valve_open, now
            result.valve_changes.append(now)

        if lost:
            result.outage_min = min(result.outage_min, plant.room)
            result.outage_max = max(result.outage_max, plant.room)
        hour = (now % DAY) / 3600
        if now > MEASURE_FROM_DAY * DAY and target == TARGET and hour >= 8:
            result.comfort_errors.append(plant.room - TARGET)
    return result


@pytest.mark.parametrize("phase", PHASES)
def test_room_sensor_outage_without_hold_cools_the_room(phase):
    """SmartHeat 0.3.0 closed the valve for good: the defect the hold fixes."""
    assert simulate(phase, hold=False, outage=True).outage_min < 11.0


@pytest.mark.parametrize("phase", PHASES)
def test_duty_hold_keeps_the_room_warm_through_an_outage(phase):
    result = simulate(phase, hold=True, outage=True)
    assert result.outage_min >= 17.0
    assert result.outage_max <= 24.0


@pytest.mark.parametrize("phase", PHASES)
def test_hold_changes_nothing_while_the_sensors_work(phase):
    with_hold = simulate(phase, hold=True)
    without = simulate(phase, hold=False)
    assert with_hold.valve_changes == without.valve_changes
    assert with_hold.comfort_errors == without.comfort_errors


# python tests/unit/test_simulation_loop.py prints the README numbers
if __name__ == "__main__":
    print(f"{'weather phase':36}" + "".join(f"{p:>12}" for p in PHASES))
    errors = [simulate(p, hold=True).mean_abs_error for p in PHASES]
    print(
        f"{'occupied mean |room - target| (K)':36}"
        + "".join(f"{e:12.2f}" for e in errors)
    )
    print("24 h room sensor outage, room (C):")
    for label, hold in (
        ("  valve closes (0.3.0)", False),
        ("  duty hold (0.3.1)", True),
    ):
        runs = [simulate(p, hold=hold, outage=True) for p in PHASES]
        print(
            f"{label:36}"
            + "".join(f"{r.outage_min:7.1f}-{r.outage_max:4.1f}" for r in runs)
        )
