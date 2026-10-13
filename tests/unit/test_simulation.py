"""Closed-loop simulation: SmartHeat against a simple thermal model of a room.

The plant has two heat stores: the emitter (floor screed or radiator, including the
water in it) and the room. Hot water flowing through the open valve heats the emitter,
the emitter heats the room, and the room loses heat to the outside.

The sensors behave like real ones: they lag, report in 0.1 °C steps, and the return
sensor only reads the emitter while water flows. When the flow stops, the pipe at the
manifold drifts toward its surroundings instead.

The scenario has a night setback (19 °C at night, 21 °C from 06:00 to 22:00) and
weather that changes over the days, which is when an on/off thermostat overshoots.
The tests compare the learned controller with a plain thermostat that closes at the
target.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

import pytest

if __name__ == "__main__":
    import conftest  # noqa: F401  registers smartheat_core when run as a script

from smartheat_core.controller import Controller, ControllerConfig, Inputs
from smartheat_core.learning import OvershootModel

TARGET = 21.0
NIGHT_TARGET = 19.0
CONTROL_STEP_S = 60
PHYSICS_STEP_S = 10


@dataclass
class Plant:
    """Two-node thermal model. Capacities in J/K, conductances in W/K."""

    c_emitter: float
    c_room: float
    u_emitter_room: float
    u_room_outside: float
    flow: float  # mass flow times heat capacity of water
    supply: float
    outside: float
    t_emitter: float = 20.0
    t_room: float = 20.0

    def step(self, valve_open: bool, dt: float, outside: float) -> None:
        q_in = self.flow * (self.supply - self.t_emitter) if valve_open else 0.0
        q_emit = self.u_emitter_room * (self.t_emitter - self.t_room)
        q_loss = self.u_room_outside * (self.t_room - outside)
        self.t_emitter += (q_in - q_emit) * dt / self.c_emitter
        self.t_room += (q_emit - q_loss) * dt / self.c_room


@dataclass
class Sensors:
    """Room sensor with a 10 min lag; return sensor on the pipe at the manifold."""

    room: float | None = None
    ret: float | None = None

    def read(self, plant: Plant, flowing: bool, dt: float) -> tuple[float, float]:
        if self.room is None:
            self.room = self.ret = plant.t_room
        self.room += (plant.t_room - self.room) * (1 - math.exp(-dt / 600))
        if flowing:
            goal, tau = plant.t_emitter - 1.0, 300
        else:
            goal, tau = plant.t_room + 2.0, 1200
        self.ret += (goal - self.ret) * (1 - math.exp(-dt / tau))
        return round(self.room, 1), round(self.ret, 1)


# ~20 m² room, 0 °C outside on average
UNDERFLOOR = Plant(
    c_emitter=1.8e6,  # 5 cm screed
    c_room=1.0e6,
    u_emitter_room=200.0,
    u_room_outside=40.0,
    flow=140.0,  # 2 l/min
    supply=45.0,
    outside=0.0,
)
RADIATOR = Plant(
    c_emitter=6.0e4,  # 10 l water plus steel
    c_room=1.0e6,
    u_emitter_room=50.0,
    u_room_outside=40.0,
    flow=84.0,  # 1.2 l/min
    supply=65.0,
    outside=0.0,
)
UNDERFLOOR_CONFIG = ControllerConfig(
    min_open_s=900, min_closed_s=900, coast_window_s=4 * 3600, max_return=45.0
)
RADIATOR_CONFIG = ControllerConfig(
    min_open_s=300, min_closed_s=300, coast_window_s=3600, max_return=75.0
)


def weather(day: float) -> float:
    """Outside temperature offset: a 5-day weather cycle plus day and night."""
    return 6 * math.sin(2 * math.pi * day / 5) + 3 * math.sin(2 * math.pi * day)


def target_at(now: float) -> float:
    hour = (now % 86400) / 3600
    return TARGET if 6 <= hour < 22 else NIGHT_TARGET


class FixedModel(OvershootModel):
    """A model that never learns: a plain thermostat when the overshoot is 0."""

    def add_cycle(self, *args, **kwargs) -> None:
        pass


@dataclass
class Result:
    model: OvershootModel
    peak_errors: list[float] = field(default_factory=list)  # room peak - target
    comfort_errors: list[float] = field(default_factory=list)  # room - target, daytime

    @property
    def mean_abs_peak_error(self) -> float:
        return sum(abs(e) for e in self.peak_errors) / len(self.peak_errors)

    @property
    def worst_peak_error(self) -> float:
        return max(self.peak_errors)

    @property
    def mean_comfort_error(self) -> float:
        return sum(self.comfort_errors) / len(self.comfort_errors)


def simulate(
    plant: Plant,
    config: ControllerConfig,
    model: OvershootModel,
    days: float = 10,
    measure_from_day: float = 4,
) -> Result:
    """Run the closed loop; measure the true room peak of every closed period."""
    plant = replace(plant)
    sensors = Sensors()
    controller = Controller(config, model)
    result = Result(model)
    valve, changed_at = False, -1e9
    closed: list[float] | None = None  # [target, peak] while the valve is closed

    for step in range(int(days * 86400 / CONTROL_STEP_S)):
        now = step * CONTROL_STEP_S
        day = now / 86400
        outside = plant.outside + weather(day)
        for _ in range(CONTROL_STEP_S // PHYSICS_STEP_S):
            plant.step(valve, PHYSICS_STEP_S, outside)
        room, ret = sensors.read(plant, valve, CONTROL_STEP_S)
        target = target_at(now)

        decision = controller.update(
            Inputs(
                room=room,
                return_temp=ret,
                target=target,
                valve_open=valve,
                valve_changed_at=changed_at,
            ),
            now,
        )

        measuring = day >= measure_from_day
        if closed is not None:
            if target != closed[0]:
                closed = None  # setback started or ended: not a comparable peak
            else:
                closed[1] = max(closed[1], plant.t_room)
        if decision.valve_open != valve:
            if (
                decision.valve_open
                and closed is not None
                and measuring
                and closed[0] == TARGET
            ):
                result.peak_errors.append(closed[1] - TARGET)
            closed = None if decision.valve_open else [target, plant.t_room]
            valve, changed_at = decision.valve_open, now
        if measuring and target == TARGET and (now % 86400) >= 8 * 3600:
            result.comfort_errors.append(plant.t_room - TARGET)

    return result


CASES = [
    pytest.param(UNDERFLOOR, UNDERFLOOR_CONFIG, 0.4, id="underfloor"),
    pytest.param(RADIATOR, RADIATOR_CONFIG, 0.2, id="radiator"),
]


@pytest.mark.parametrize(("plant", "config", "initial"), CASES)
def test_learning_beats_plain_thermostat(plant, config, initial):
    plain = simulate(plant, config, FixedModel(initial_overshoot=0.0))
    learned = simulate(plant, config, OvershootModel(initial_overshoot=initial))

    assert len(learned.peak_errors) >= 10
    assert learned.model.trained
    assert learned.mean_abs_peak_error < plain.mean_abs_peak_error / 2
    assert learned.worst_peak_error < plain.worst_peak_error / 2
    # closing early must not leave the room cold
    assert learned.mean_comfort_error > -0.4


# python tests/unit/test_simulation.py prints the README table
if __name__ == "__main__":
    for case in CASES:
        plant, config, initial = case.values
        for label, model in (
            ("plain", FixedModel(initial_overshoot=0.0)),
            ("learned", OvershootModel(initial_overshoot=initial)),
        ):
            r = simulate(plant, config, model)
            m = r.model
            print(
                f"{case.id:10} {label:8} cycles={len(r.peak_errors):3} "
                f"peak error mean abs={r.mean_abs_peak_error:.2f} worst={r.worst_peak_error:+.2f} "
                f"comfort mean={r.mean_comfort_error:+.2f}  "
                f"beta={m.beta} n={m.beta_samples} h={m.holding_delta} flat={m.flat_overshoot:.2f}"
            )
