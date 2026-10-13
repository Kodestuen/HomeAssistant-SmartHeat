"""Valve control for a SmartHeat zone.

The controller is a pure function of its inputs plus a little remembered state, so it
can be driven by Home Assistant, a unit test or a thermal simulation alike. Times are
plain float seconds (e.g. Unix timestamps), temperatures are °C.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .learning import OvershootModel

REASON_HEATING = "heating"
REASON_IDLE = "idle"
REASON_PREDICTIVE_CLOSE = "predictive_close"
REASON_COASTING = "coasting"
REASON_MIN_OPEN = "min_open_time"
REASON_MIN_CLOSED = "min_closed_time"
REASON_OFF = "off"
REASON_WINDOW = "window_open"
REASON_MAX_RETURN = "max_return_temp"
REASON_ROOM_SENSOR_WAIT = "room_sensor_unavailable"
REASON_ROOM_SENSOR_LOST = "room_sensor_lost"
REASON_FROST = "frost_protection"

PEAK_DROP = 0.1  # °C fall below the peak that ends a coast
NO_HEAT_DELAY_S = 300.0  # wait this long after opening before judging the supply
NO_HEAT_MARGIN = 5.0  # supply must be at least this much warmer than the room
SATURATION_DELTA = 2.0  # supply - return below this means the loop is saturated
SATURATION_TIME_S = 1800.0
DUTY_WINDOW_S = 86400.0
MIN_HEAT_UP_S = 600.0  # shortest heating period used for the heat-up rate
_EPS = 1e-6


@dataclass
class ControllerConfig:
    """Tuning for one zone. Times in seconds, temperatures in °C."""

    hysteresis: float = 0.3
    min_open_s: float = 900.0
    min_closed_s: float = 900.0
    coast_window_s: float = 14400.0
    max_return: float = 45.0
    sensor_timeout_s: float = 1800.0
    frost_temp: float = 7.0


@dataclass
class Inputs:
    """Everything the controller looks at in one update."""

    room: float | None
    return_temp: float | None
    target: float
    heating_enabled: bool = True
    supply: float | None = None
    window_open: bool = False
    valve_open: bool | None = None  # actual valve state, None if unknown
    valve_changed_at: float | None = None  # when the valve last changed state


@dataclass
class Decision:
    """What the valve should do, and why."""

    valve_open: bool
    reason: str
    predicted_overshoot: float | None = None
    close_threshold: float | None = None


@dataclass
class Coast:
    """A closed valve waiting for the room to peak after a predictive close."""

    started: float
    room_close: float
    return_close: float | None
    target: float
    peak: float
    valid: bool = True


class Controller:
    """Decides when the valve opens and closes, and feeds the learning."""

    def __init__(self, config: ControllerConfig, model: OvershootModel) -> None:
        self.config = config
        self.model = model
        self.valve_open: bool | None = None
        self.valve_changed_at: float | None = None
        self.coast: Coast | None = None
        self.last_decision: Decision | None = None
        self.no_heat_source = False
        self.loop_saturated = False
        self.supply_return_delta: float | None = None
        self._pending_close_reason: str | None = None
        self._prev: Inputs | None = None
        self._room_lost_since: float | None = None
        self._open_started: float | None = None
        self._open_room: float | None = None
        self._valley: list[float] | None = None  # [lowest room, x first, x last]
        self._valley_done = False
        self._saturated_since: float | None = None
        self._open_intervals: list[list[float | None]] = []

    @property
    def coasting(self) -> bool:
        """Return True while the room is still rising after a predictive close."""
        return self.coast is not None

    def reset_learning(self) -> None:
        """Forget everything learned and drop the cycle in progress."""
        self.model.reset()
        self.coast = None

    def update(self, inp: Inputs, now: float) -> Decision:
        """Take in the current inputs and decide what the valve should do."""
        if inp.room is None:
            if self._room_lost_since is None:
                self._room_lost_since = now
        else:
            self._room_lost_since = None

        self._observe_valve(inp, now)
        self._track_supply(inp, now)
        self._track_valley(inp)
        self._track_coast(inp, now)

        decision = self._decide(inp, now)
        if decision.valve_open:
            self._pending_close_reason = None
        elif self.valve_open:
            # remembered so the close can be recognised as predictive once it happens
            self._pending_close_reason = decision.reason
        self.last_decision = decision
        self._prev = inp
        return decision

    def duty_cycle(self, now: float) -> float:
        """Return the share of the last 24 h the valve was open, in percent."""
        start = now - DUTY_WINDOW_S
        self._open_intervals = [
            iv for iv in self._open_intervals if iv[1] is None or iv[1] > start
        ]
        open_s = sum(
            min(now, now if end is None else end) - max(start, begin)
            for begin, end in self._open_intervals
        )
        return min(max(100.0 * open_s / DUTY_WINDOW_S, 0.0), 100.0)

    def _observe_valve(self, inp: Inputs, now: float) -> None:
        if inp.valve_open is None:
            return
        changed_at = now if inp.valve_changed_at is None else inp.valve_changed_at
        # The update that decided the change saw the room as it was when it happened.
        ref = (
            self._prev
            if self._prev is not None and self._prev.room is not None
            else inp
        )
        if self.valve_open is None:
            # first sight of the valve: nothing to learn from yet
            self.valve_open = inp.valve_open
            self.valve_changed_at = inp.valve_changed_at
            if inp.valve_open:
                self._start_open_period(ref, changed_at)
            return
        if inp.valve_open == self.valve_open:
            if inp.valve_changed_at is not None:
                self.valve_changed_at = inp.valve_changed_at
            return
        self.valve_open = inp.valve_open
        self.valve_changed_at = changed_at
        if inp.valve_open:
            # reopened before the room peaked: that cycle can't be learned from
            self.coast = None
            self._start_open_period(ref, changed_at)
        else:
            self._end_open_period(ref, changed_at)

    def _start_open_period(self, ref: Inputs, at: float) -> None:
        self._open_started = at
        self._open_room = ref.room
        self._valley = None
        self._valley_done = False
        self._open_intervals.append([at, None])

    def _end_open_period(self, ref: Inputs, at: float) -> None:
        if self._open_intervals and self._open_intervals[-1][1] is None:
            self._open_intervals[-1][1] = at
        had_heat = not self.no_heat_source
        if (
            had_heat
            and self._open_started is not None
            and self._open_room is not None
            and ref.room is not None
            and at - self._open_started >= MIN_HEAT_UP_S
        ):
            hours = (at - self._open_started) / 3600.0
            self.model.add_heat_up_rate((ref.room - self._open_room) / hours)
        reason, self._pending_close_reason = self._pending_close_reason, None
        if reason == REASON_PREDICTIVE_CLOSE and ref.room is not None:
            self.coast = Coast(
                started=at,
                room_close=ref.room,
                return_close=ref.return_temp,
                target=ref.target,
                peak=ref.room,
                valid=had_heat,
            )
        self._open_started = None
        self._open_room = None

    def _track_supply(self, inp: Inputs, now: float) -> None:
        if inp.supply is not None and inp.return_temp is not None:
            self.supply_return_delta = inp.supply - inp.return_temp
        else:
            self.supply_return_delta = None
        if not self.valve_open or self._open_started is None:
            self.no_heat_source = False
            self.loop_saturated = False
            self._saturated_since = None
            return
        if (
            inp.supply is not None
            and inp.room is not None
            and now - self._open_started >= NO_HEAT_DELAY_S
        ):
            self.no_heat_source = inp.supply < inp.room + NO_HEAT_MARGIN
        if (
            self.supply_return_delta is not None
            and not self.no_heat_source
            and self.supply_return_delta < SATURATION_DELTA
        ):
            if self._saturated_since is None:
                self._saturated_since = now
            self.loop_saturated = now - self._saturated_since >= SATURATION_TIME_S
        else:
            self._saturated_since = None
            self.loop_saturated = False

    def _track_valley(self, inp: Inputs) -> None:
        """Learn the holding difference at the room's low point after opening.

        The room keeps cooling until the emitter has warmed enough to cover the heat
        loss; the return-room difference at that low point is what holding needs.
        Sensors report in steps, so the low point is a plateau: use its middle.
        """
        if (
            not self.valve_open
            or self._valley_done
            or inp.room is None
            or inp.return_temp is None
        ):
            return
        x = inp.return_temp - inp.room
        valley = self._valley
        if valley is None or inp.room < valley[0] - _EPS:
            self._valley = [inp.room, x, x]
        elif inp.room <= valley[0] + _EPS:
            valley[2] = x
        elif inp.room >= valley[0] + PEAK_DROP - _EPS:
            self._valley_done = True
            if not self.no_heat_source:
                self.model.add_holding_delta((valley[1] + valley[2]) / 2)

    def _track_coast(self, inp: Inputs, now: float) -> None:
        coast = self.coast
        if coast is None:
            return
        if inp.target != coast.target or not inp.heating_enabled or inp.window_open:
            # conditions changed: nothing to learn, and no reason to hold the valve shut
            self.coast = None
            return
        if inp.room is None:
            coast.valid = False
        else:
            coast.peak = max(coast.peak, inp.room)
        elapsed = now - coast.started
        peaked = (
            inp.room is not None
            and elapsed >= self.config.coast_window_s / 8
            and inp.room <= coast.peak - PEAK_DROP + _EPS
        )
        if not peaked and elapsed < self.config.coast_window_s:
            return
        if coast.valid:
            self.model.add_cycle(
                overshoot=coast.peak - coast.room_close,
                room_close=coast.room_close,
                return_close=coast.return_close,
                peak_error=coast.peak - coast.target,
            )
        self.coast = None

    def _frost_guard(self, inp: Inputs, open_now: bool, elapsed: float) -> bool:
        cfg = self.config
        active = (
            open_now
            and self.last_decision is not None
            and self.last_decision.reason == REASON_FROST
        )
        if inp.room is not None:
            return inp.room < cfg.frost_temp + (cfg.hysteresis if active else 0.0)
        # Room sensor down: a closed loop's return drifts toward room temperature,
        # so use it as a stand-in and heat in min-open-time pulses.
        if active:
            return elapsed < cfg.min_open_s
        return (
            not open_now
            and inp.return_temp is not None
            and inp.return_temp < cfg.frost_temp
        )

    def _decide(self, inp: Inputs, now: float) -> Decision:
        cfg = self.config
        open_now = bool(self.valve_open)
        elapsed = (
            math.inf if self.valve_changed_at is None else now - self.valve_changed_at
        )

        # Safety rules first; they ignore the minimum open/closed times.
        if inp.return_temp is not None and inp.return_temp > cfg.max_return:
            return Decision(False, REASON_MAX_RETURN)
        if self._frost_guard(inp, open_now, elapsed):
            return Decision(True, REASON_FROST)
        if not inp.heating_enabled:
            return Decision(False, REASON_OFF)
        if inp.window_open:
            return Decision(False, REASON_WINDOW)
        if inp.room is None:
            if now - self._room_lost_since >= cfg.sensor_timeout_s:
                return Decision(False, REASON_ROOM_SENSOR_LOST)
            return Decision(open_now, REASON_ROOM_SENSOR_WAIT)

        predicted = self.model.predict(inp.room, inp.return_temp)
        close_at = inp.target - predicted
        if open_now:
            if inp.room < close_at:
                return Decision(True, REASON_HEATING, predicted, close_at)
            if elapsed < cfg.min_open_s:
                return Decision(True, REASON_MIN_OPEN, predicted, close_at)
            return Decision(False, REASON_PREDICTIVE_CLOSE, predicted, close_at)

        idle_reason = REASON_COASTING if self.coast is not None else REASON_IDLE
        if inp.room >= inp.target - cfg.hysteresis:
            return Decision(False, idle_reason, predicted, close_at)
        if self.coast is not None:
            return Decision(False, REASON_COASTING, predicted, close_at)
        if elapsed < cfg.min_closed_s:
            return Decision(False, REASON_MIN_CLOSED, predicted, close_at)
        return Decision(True, REASON_HEATING, predicted, close_at)
