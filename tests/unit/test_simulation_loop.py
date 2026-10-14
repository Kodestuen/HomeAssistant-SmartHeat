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

The zone runs SmartHeat's real controllers with the installed underfloor defaults. The
tests take the room sensor away for 24 h: SmartHeat 0.3.0 closed the valve for good
and the room cooled far below the target; the duty hold keeps it warm. They also aim
the thermostat 0.15 °C above the target, which brings the room's average closer to it.

The tickle pulses run on the same floor, with the outdoor temperature from the weather
or without it, also in mild weather (a +10 °C mean), through a day without the room or
the return sensor, and through 11 restarts of Home Assistant. The days without a sensor
and the runs without an outdoor temperature are checked at several pulse times.
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
from smartheat_core.tickle import TickleConfig, TickleController

DAY = 86400
CONTROL_STEP_S = 60
PHYSICS_STEP_S = 20
SUPPLY = 35.0
WATER_HEAT = 4186.0  # J/K per litre
DAYS = 12
MEASURE_FROM_DAY = 4
OUTAGE = (6 * DAY, 7 * DAY)  # a sensor is unavailable from day 6, 00:00
PHASES = (0.0, 1.3, 2.6)  # where in its 5-day cycle the weather starts
AIM = 0.15  # the aim offset the README suggests for underfloor zones
MILD = 10.0  # °C added to the weather in mild runs: a +10 °C mean instead of 0 °C
GRID_PHASE_S = 779.0  # the tickle zone's grid; its points don't fall on 06:00
# Each zone pulses at minutes of its own. The outage and no-outdoor results vary with
# them, so they are checked on several grids: spread over the interval, with points
# on a control step and between two.
GRID_PHASES = (0.0, 300.0, GRID_PHASE_S, 1350.0, 1950.0, 2400.0)
# Home Assistant restarts a little over a day apart, each at another time of day
RESTARTS = tuple(int((k + 0.5) * DAYS * DAY / 11) // 60 * 60 for k in range(11))


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
    outage_errors: list[float] = field(default_factory=list)  # ... while it is lost
    valve_changes: list[float] = field(default_factory=list)
    outage_min: float = math.inf  # the true room while a sensor is lost
    outage_max: float = -math.inf

    @property
    def mean_abs_error(self) -> float:
        return sum(abs(e) for e in self.comfort_errors) / len(self.comfort_errors)

    @property
    def mean_error(self) -> float:
        """How far the room sits above the target on average; below if negative."""
        return sum(self.comfort_errors) / len(self.comfort_errors)

    @property
    def outage_mean_error(self) -> float:
        """The same for the day without a sensor."""
        return sum(self.outage_errors) / len(self.outage_errors)

    @property
    def openings_per_day(self) -> float:
        """Valve openings a day from day 4 on. The valve starts closed."""
        opened = self.valve_changes[::2]
        measured = sum(1 for t in opened if t > MEASURE_FROM_DAY * DAY)
        return measured / (DAYS - MEASURE_FROM_DAY)


def zone_config(hold: bool, aim: float = 0.0) -> ControllerConfig:
    """The installed underfloor defaults, with or without what 0.3.1 adds."""
    return ControllerConfig(
        hysteresis=0.3,
        aim_offset=aim,
        min_open_s=900,
        min_closed_s=900,
        coast_window_s=4 * 3600,
        max_return=45.0,
        lost_room_hold=hold,
        room_silent_s=6 * 3600 if hold else None,
    )


def tickle_config(outdoor: bool, grid: float = GRID_PHASE_S) -> TickleConfig:
    """The tickle's defaults: a pulse may start every 45 min, and runs 12 min at least.

    The settings the thermostat shares with it are the installed underfloor defaults.
    """
    return TickleConfig(
        hysteresis=0.3,
        min_open_s=900,
        min_closed_s=900,
        coast_window_s=4 * 3600,
        max_return=45.0,
        room_silent_s=6 * 3600,
        hold_interval_s=2700,
        grid_phase_s=grid,
        flush_s=720,
        has_outside_sensor=outdoor,
    )


def simulate(
    phase: float,
    hold: bool = True,
    outage: bool = False,
    aim: float = 0.0,
    tickle: bool = False,
    outdoor: bool = True,
    mean: float = 0.0,
    return_outage: bool = False,
    restarts: bool = False,
    grid: float = GRID_PHASE_S,
) -> Result:
    """Run the zone for 12 days; measure the true room from day 4 on.

    - hold: the thermostat with what 0.3.1 adds; aim: its aim offset
    - tickle: the tickle pulses instead, with an outdoor sensor if outdoor, on the
      grid with this phase
    - mean: °C added to the weather
    - outage: no room sensor for a day; return_outage: no return sensor
    - restarts: Home Assistant restarts 11 times
    """
    return _simulate(
        phase, hold, outage, aim, tickle, outdoor, mean, return_outage, restarts, grid
    )


@cache
def _simulate(
    phase: float,
    hold: bool,
    outage: bool,
    aim: float,
    tickle: bool,
    outdoor: bool,
    mean: float,
    return_outage: bool,
    restarts: bool,
    grid: float,
) -> Result:
    def outside(t: float) -> float:
        return weather(t / DAY + phase) + mean

    plant = FloorPlant(outside(0))
    room_sensor = Sensor(plant.room, lag_s=600)
    return_sensor = Sensor(plant.tail, lag_s=90)
    model = OvershootModel(initial_overshoot=0.4)
    calibration = ZoneCalibration() if hold or tickle else None

    def start() -> Controller:
        if tickle:
            return TickleController(tickle_config(outdoor, grid), model, calibration)
        return Controller(zone_config(hold, aim), model, calibration)

    controller = start()
    result = Result()
    valve, changed_at = False, -1e9
    restart_at = list(RESTARTS) if restarts else []

    for now in range(CONTROL_STEP_S, DAYS * DAY + 1, CONTROL_STEP_S):
        # The last minute, in physics steps
        for t in range(now - CONTROL_STEP_S + PHYSICS_STEP_S, now + 1, PHYSICS_STEP_S):
            plant.step(valve, PHYSICS_STEP_S, outside(t))
            room_sensor.follow(plant.room, PHYSICS_STEP_S)
            return_sensor.follow(plant.tail, PHYSICS_STEP_S)

        if restart_at and now >= restart_at[0]:
            # The zone starts again from what it saved; the valve keeps its state, but
            # reports it afresh
            del restart_at[0]
            model = OvershootModel.from_dict(model.to_dict(), 0.4)
            if calibration is not None:
                calibration = ZoneCalibration.from_dict(calibration.to_dict())
            controller = start()
            changed_at = now
        lost = OUTAGE[0] <= now < OUTAGE[1] and (outage or return_outage)
        target = target_at(now)
        decision = controller.update(
            Inputs(
                room=None if lost and outage else room_sensor.read(),
                return_temp=None if lost and return_outage else return_sensor.read(),
                target=target,
                valve_open=valve,
                valve_changed_at=changed_at,
                room_updated_at=now,  # reports every minute
                outside=outside(now),
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
            if lost:
                result.outage_errors.append(plant.room - TARGET)
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


@pytest.mark.parametrize("phase", PHASES)
def test_aim_offset_brings_the_room_closer_to_the_target(phase):
    """The room peaks at the target, so on average it sits below it."""
    plain = simulate(phase, hold=True)
    aimed = simulate(phase, hold=True, aim=AIM)
    assert plain.mean_error < 0
    assert aimed.mean_abs_error < plain.mean_abs_error
    assert abs(aimed.mean_error) < abs(plain.mean_error)


@pytest.mark.parametrize("phase", PHASES)
def test_tickle_holds_the_room_closer_to_the_target(phase):
    """At a 0 °C mean the tickle's error is at least 40 % smaller."""
    thermostat = simulate(phase)
    tickle = simulate(phase, tickle=True)
    assert tickle.mean_abs_error <= 0.6 * thermostat.mean_abs_error
    assert abs(tickle.mean_error) <= 0.1  # centred on the target
    assert 10 <= tickle.openings_per_day <= 30


@pytest.mark.parametrize("grid", GRID_PHASES)
def test_tickle_without_an_outdoor_temperature(grid):
    """c follows the weather only slowly, so the error is larger than with the term.

    Measured over the three weather runs together, as the study did: a single run
    varies more with the zone's pulse times, from 0.54 to 0.77 of the thermostat's.
    """
    tickle = [simulate(p, tickle=True, outdoor=False, grid=grid) for p in PHASES]
    error = sum(r.mean_abs_error for r in tickle)
    assert error <= 0.75 * sum(simulate(p).mean_abs_error for p in PHASES)


def test_tickle_gains_less_in_mild_weather():
    """At a +10 °C mean the floor needs little heat: even a short pulse is a lot.

    The study measured this over the three weather runs together.
    """
    thermostat = [simulate(p, mean=MILD) for p in PHASES]
    tickle = [simulate(p, tickle=True, mean=MILD) for p in PHASES]
    error = sum(r.mean_abs_error for r in tickle)
    assert error <= 0.9 * sum(r.mean_abs_error for r in thermostat)
    assert all(abs(r.mean_error) <= 0.1 for r in tickle)


@pytest.mark.parametrize("grid", GRID_PHASES)
@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize(("mean", "highest"), [(0.0, 23.0), (MILD, 25.0)])
def test_tickle_keeps_the_room_warm_without_the_room_sensor(grid, phase, mean, highest):
    """Without the room, a pulse lasts at most the last day's share of an interval.

    In mild weather that share is shorter than the minimum pulse, so it alone sets
    the openings, as in the duty hold. On a day that turns warm the room overheats a
    little: to 24.2-24.7 °C over these grids, against 24.3-24.5 °C with the duty hold
    and 25.6-25.7 °C without the cap. The highest rooms come on grids whose points
    fall on a control step: there an opening that ends just after a step lasts until
    the next one, a minute longer each time.
    """
    result = simulate(phase, outage=True, tickle=True, mean=mean, grid=grid)
    assert result.outage_min >= 17.0
    assert result.outage_max <= highest


@pytest.mark.parametrize("grid", GRID_PHASES)
@pytest.mark.parametrize("phase", PHASES)
def test_tickle_keeps_the_room_warm_without_the_return_sensor(grid, phase):
    result = simulate(phase, tickle=True, return_outage=True, grid=grid)
    assert result.outage_min >= 17.0
    assert result.outage_max <= 22.5


@pytest.mark.parametrize("phase", PHASES)
def test_restarts_hardly_change_the_tickle(phase):
    restarted = simulate(phase, tickle=True, restarts=True)
    error = simulate(phase, tickle=True).mean_abs_error
    assert abs(restarted.mean_abs_error - error) <= 0.03


# python tests/unit/test_simulation_loop.py prints the README numbers
if __name__ == "__main__":
    controllers = (
        ("thermostat", {}),
        (f"thermostat, aim offset {AIM:+.2f}", {"aim": AIM}),
        ("tickle", {"tickle": True}),
        ("tickle without outdoor temperature", {"tickle": True, "outdoor": False}),
    )
    rows = (
        ("occupied mean |room - target| (K)", lambda r: f"{r.mean_abs_error:12.2f}"),
        ("occupied mean room - target (K)", lambda r: f"{r.mean_error:+12.2f}"),
        ("valve openings a day", lambda r: f"{r.openings_per_day:12.1f}"),
    )

    def ranges(runs: list[Result]) -> str:
        return "".join(f"{r.outage_min:7.1f}-{r.outage_max:4.1f}" for r in runs)

    for mean in (0.0, MILD):
        print(
            f"{f'weather phase, {mean:+.0f} C mean':36}"
            + "".join(f"{p:>12}" for p in PHASES)
        )
        for title, show in rows:
            print(title)
            for label, options in controllers:
                runs = [simulate(p, mean=mean, **options) for p in PHASES]
                print(f"  {label:34}" + "".join(show(r) for r in runs))
        print()
    print("24 h room sensor outage, room (C):")
    for label, options in (
        ("valve closes (0.3.0)", {"hold": False}),
        ("duty hold (0.3.1)", {}),
        ("tickle", {"tickle": True}),
        ("tickle, +10 C mean", {"tickle": True, "mean": MILD}),
    ):
        runs = [simulate(p, outage=True, **options) for p in PHASES]
        print(f"  {label:34}{ranges(runs)}")
    print("The same day, occupied mean room - target (K):")
    for label, options in (
        ("duty hold (0.3.1)", {}),
        ("tickle", {"tickle": True}),
        ("tickle, +10 C mean", {"tickle": True, "mean": MILD}),
    ):
        runs = [simulate(p, outage=True, **options) for p in PHASES]
        print(f"  {label:34}" + "".join(f"{r.outage_mean_error:+12.2f}" for r in runs))
    print("24 h return sensor outage, room (C):")
    runs = [simulate(p, tickle=True, return_outage=True) for p in PHASES]
    print(f"  {'tickle':34}{ranges(runs)}")
    grids = ", ".join(f"{g:.0f}" for g in GRID_PHASES)
    print(f"The tickle's outages on the grids at {grids} s, room (C):")
    for label, options in (
        ("room sensor lost", {"outage": True}),
        ("room sensor lost, +10 C mean", {"outage": True, "mean": MILD}),
        ("return sensor lost", {"return_outage": True}),
    ):
        runs = [
            simulate(p, tickle=True, grid=g, **options)
            for g in GRID_PHASES
            for p in PHASES
        ]
        lowest = min(r.outage_min for r in runs)
        highest = max(r.outage_max for r in runs)
        print(f"  {label:34}{lowest:7.1f}-{highest:4.1f}")
    print("11 restarts, occupied mean |room - target| (K):")
    runs = [simulate(p, tickle=True, restarts=True) for p in PHASES]
    print(f"  {'tickle':34}" + "".join(f"{r.mean_abs_error:12.2f}" for r in runs))
