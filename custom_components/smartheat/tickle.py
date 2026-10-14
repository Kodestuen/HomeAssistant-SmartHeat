"""Tickle pulses: short, frequent openings of an underfloor zone's valve.

A pulse may start at each point of the zone's grid, every 45 minutes by default. The
valve then stays open for at least the flush time, 12 minutes by default, so that the
return sensor reads water that has passed through the floor. After that the valve
closes once the return reaches the return target

    T = target + c + a * (target - outdoor) - kp * (room - target)

with a = 0.15 and kp = 3, or a = 0 and kp = 4 without an outdoor temperature. The
target sets the level, not the measured room: a target that followed the room would
heat a warm room even more. A room that is too warm lowers T a lot, and the offset c
is calibrated slowly from the room's error. A pulse is skipped when the room is warm,
and a pulse still open at the next grid point carries on.

When a sensor is lost, the zone keeps heating on the same grid: by the return alone,
by the room alone, or by the share of the time the valve was open over the last day.

Experimental, and for underfloor zones only. Pure Python, like controller.py.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .controller import (
    DUTY_FRESH_S,
    FALLBACK_ROOM_SENSOR,
    HOLD_MIN_S,
    NO_HEAT_DELAY_S,
    REASON_ROOM_SENSOR_WAIT,
    Controller,
    ControllerConfig,
    Decision,
    Inputs,
    hold_on_time,
)
from .learning import OUTSIDE_SLOPE, OvershootModel, ZoneCalibration, plausible_outside

REASON_PULSE_FLUSH = "pulse_flush"
REASON_PULSE = "pulse"
REASON_PULSE_EXTENDED = "pulse_extended"
REASON_RETURN_REACHED = "return_reached"
REASON_PULSE_SKIPPED = "pulse_skipped"
REASON_WAITING = "waiting"
REASON_TIMED_PULSE = "timed_pulse"
REASON_RETURN_SENSOR_WAIT = "return_sensor_unavailable"

FALLBACK_RETURN_SENSOR = "return_sensor"  # timed pulses by the room alone
FALLBACK_SENSORS = "sensors"  # timed pulses by the last day's open share

FLUSH_S = 720.0
# a is OUTSIDE_SLOPE, kept in learning.py with the return offset that goes with it
ROOM_GAIN = 3.0  # kp: T falls this much per °C the room is above the target
ROOM_GAIN_NO_OUTSIDE = 4.0
RETURN_MARGIN = 2.0  # T stays this far below the maximum return temperature
SKIP_MARGIN = 0.3  # a room this far above the target skips a pulse, or ends it
FLUSH_SKIP_MARGIN = 0.6  # ... and this far above, it ends the flush too
CALIBRATION_RATE = 0.1 / 3600  # c moves 0.1 °C an hour per °C of room error
CALIBRATION_BAND = 1.0  # c is calibrated only while the room is this close to target
CALIBRATION_STEP_MAX_S = 300.0  # longest gap between updates counted
WINDOW_SETTLE_S = 3600.0  # c waits this long after a window closes
HEAT_RISE = 1.0  # a pulse that raises the return this much shows the water is hot
RISE_TICK = 0.5  # a target rise this big adds a grid point ...
RISE_TICK_CLOSED_S = 600.0  # ... once the valve has been closed this long ...
RISE_TICK_SPREAD_S = 600.0  # ... after a delay within this, different for each zone
OUTSIDE_HOLD_S = 48 * 3600.0  # the last outdoor temperature is used this long
SENSOR_WAIT_S = 120.0  # a sensor missing this long starts a fallback
RETURN_SILENT_S = 900.0  # a return that hasn't moved this long after the flush ...
RETURN_MOVE = 0.05  # ... by this much counts as missing
FALLBACK_GAIN = 0.5  # timed pulses by the room: open share per °C below the target
FALLBACK_INTEGRAL_S = 8 * 3600.0  # ... and the integral time
_EPS = 1e-6


@dataclass
class TickleConfig(ControllerConfig):
    """Tuning for a zone on tickle pulses.

    hold_interval_s is the pulse interval: a pulse may start at each point of the
    zone's grid, and the fallbacks open on the same grid. Of the thermostat's settings
    it also uses the maximum return, the room sensor silent time, and for the frost
    guard the frost temperature, the hysteresis and the minimum open time. The aim
    offset, minimum closed time, coast window and room sensor timeout aren't used.
    """

    flush_s: float = FLUSH_S  # the shortest pulse
    has_outside_sensor: bool = False  # without one, T has no outdoor term


@dataclass
class _Pulse:
    """A pulse: asked for at a grid point, or an opening seen."""

    anchor: float | None = None  # when the valve was seen opening; None until then
    reanchored: bool = False  # timed afresh after the valve was unavailable
    extended: bool = False  # carried on past a grid point
    first_return: float | None = None  # the first return reading after the opening
    first_return_at: float | None = None  # when it was read
    low_return: float | None = None  # the lowest since
    return_moved: bool = False  # the return has moved since the opening


class TickleController(Controller):
    """Opens the valve in pulses on a fixed grid, each ended at the return target."""

    # The tickle doesn't close predictively, so it has nothing to teach the model. The
    # model stays as the thermostat learned it: for its h, and for a switch back.
    learns_overshoot = False
    config: TickleConfig

    def __init__(
        self,
        config: TickleConfig,
        model: OvershootModel,
        calibration: ZoneCalibration | None = None,
    ) -> None:
        super().__init__(
            config,
            model,
            ZoneCalibration() if calibration is None else calibration,
        )
        self.calibrating = False  # the last update moved the return offset
        self.outside_temperature: float | None = None  # in use, perhaps held
        self._pulse: _Pulse | None = None
        self._closed_reason = REASON_WAITING  # why the valve stays closed for now
        self._fallback: str | None = None  # the rules in charge; None for the pulses
        self._tick_waiting = False  # a grid point passed while a sensor was awaited
        self._extra_tick_at: float | None = None  # a target rise's grid point is due
        self._extra_tick_point: float | None = None  # the interval that had one
        self._return_lost_since: float | None = None
        self._stuck_return: float | None = None  # a reading that didn't move in a pulse
        self._integral: float | None = None  # of the timed pulses by the room
        self._room_share: float | None = None  # their share of this interval
        self._no_heat = False  # no hot water, until a pulse warms the return
        self._window_closed_at: float | None = None
        self._last_update: float | None = None
        self._valve_gap = False  # the valve was unavailable since its last report

    @property
    def return_offset(self) -> float | None:
        """Return the calibrated offset c, °C."""
        return self.calibration.return_offset

    @property
    def return_target(self) -> float | None:
        """Return the return target T of the last decision, None if it had none."""
        decision = self.last_decision
        return None if decision is None else decision.return_threshold

    def reset_learning(self) -> None:
        """Start the return offset afresh, from the thermostat's learned h.

        Nothing else is forgotten. The tickle doesn't teach the overshoot model, which
        the thermostat needs if the zone switches back, and the fallbacks need the
        valve's open share.
        """
        self.calibration.reset_offset()
        if self._prev is not None:
            self._settle_offset(self._prev)

    def _aim(self, inp: Inputs) -> Inputs:
        # The return target centres the room by itself: no aim offset
        return inp

    def _observe_valve(self, inp: Inputs, now: float) -> None:
        if inp.valve_open is None:
            self._valve_gap = True  # unavailable: the valve may move unseen
            return
        gap, self._valve_gap = self._valve_gap, False
        was_open = self.valve_open
        super()._observe_valve(inp, now)
        pulse = self._pulse
        if (
            gap
            and was_open
            and inp.valve_open
            and pulse is not None
            and pulse.anchor is not None
            and not pulse.reanchored
        ):
            # The valve may have closed while it was unavailable, and the water stood:
            # the flush starts again from its return. Only once in a pulse, so a valve
            # that keeps dropping out can't keep the flush going.
            back = now if inp.valve_changed_at is None else inp.valve_changed_at
            back = min(back, now)
            if back > pulse.anchor:
                pulse.anchor = back
                pulse.reanchored = True

    def _start_open_period(self, ref: Inputs, at: float) -> None:
        super()._start_open_period(ref, at)
        # A pulse is timed from the opening seen, whoever opened the valve. A report
        # of the same state never moves it.
        if self._pulse is None:
            self._pulse = _Pulse()
        self._pulse.anchor = at

    def _end_open_period(self, ref: Inputs, at: float) -> None:
        super()._end_open_period(ref, at)
        if self._pulse is not None:  # closed by hand, or by itself
            self._end_pulse(REASON_WAITING)

    def _decide(self, inp: Inputs, now: float) -> Decision:
        open_now = bool(self.valve_open)
        elapsed = (
            math.inf if self.valve_changed_at is None else now - self.valve_changed_at
        )
        last, self._last_update = self._last_update, now
        # A long gap, e.g. while Home Assistant was down, counts as 5 minutes at most,
        # and a clock step backwards as none
        dt = 0.0 if last is None else min(max(now - last, 0.0), CALIBRATION_STEP_MAX_S)
        self.calibrating = False
        self.outside_temperature = self._outside(inp, now)
        self._settle_offset(inp)
        ret = self._track_return(inp, now)
        self._track_heat(inp, now)
        prev = self._prev
        if prev is not None:
            if prev.window_open and not inp.window_open:
                self._window_closed_at = now
            rise = inp.target - prev.target
            if rise >= RISE_TICK - _EPS and self._extra_tick_at is None:
                # Delayed by the zone's own few minutes, so that one schedule for the
                # whole house doesn't open every zone at once
                delay = self.config.grid_phase_s % RISE_TICK_SPREAD_S
                self._extra_tick_at = now + delay
        if self._tick:
            self._extra_tick_at = None  # the grid point does what it would have done
        next_pulse = self.grid.next(now)

        if (safety := self._safety(inp, open_now, elapsed)) is not None:
            # The safety rules override the flush too, and grid points passing
            # meanwhile are dropped. Afterwards a closed valve waits for the next one.
            self._extra_tick_at = None
            self._tick_waiting = False
            if safety.valve_open:
                self._closed_reason = REASON_WAITING  # for when the frost guard ends
            else:
                self._end_pulse(REASON_WAITING)
            return replace(safety, next_pulse=next_pulse)
        room_wait = inp.room is None and now - self._room_lost_since < SENSOR_WAIT_S
        return_wait = ret is None and now - self._return_lost_since < SENSOR_WAIT_S
        if room_wait or return_wait:
            # A sensor that has only just gone, e.g. while Home Assistant starts, may be
            # back in a moment: the valve stays as it is until then. A grid point that
            # passes meanwhile counts once the wait is over.
            self._tick_waiting = self._tick_waiting or self._tick
            reason = REASON_ROOM_SENSOR_WAIT if room_wait else REASON_RETURN_SENSOR_WAIT
            return Decision(open_now, reason, next_pulse=next_pulse)
        if self._tick_waiting:
            self._tick, self._tick_waiting = True, False
        if ret is None:
            return self._timed_pulses(inp, open_now, now, next_pulse)
        if inp.room is None:
            return self._room_lost(inp, ret, open_now, now, next_pulse)
        return self._pulses(inp, ret, open_now, elapsed, dt, now, next_pulse)

    def _pulses(
        self,
        inp: Inputs,
        ret: float,
        open_now: bool,
        elapsed: float,
        dt: float,
        now: float,
        next_pulse: float,
    ) -> Decision:
        """Pulse to the return target with both sensors: the normal rules."""
        self._take_over(None)
        self._integral = None
        self._calibrate(inp, dt, now)
        threshold = self._threshold(inp, inp.room)
        room, target = inp.room, inp.target
        warm = room >= target + SKIP_MARGIN - _EPS
        tick = self._tick or self._extra_tick(open_now, elapsed, now)
        pulse = self._pulse
        if pulse is not None and pulse.anchor is None and tick:
            pulse = self._pulse = None  # the valve never opened: decide afresh
        if pulse is None:
            if open_now or not tick:
                valve_open, reason = False, self._closed_reason
            elif warm or (
                room >= target - _EPS
                and self.calibration.duty * self.grid.interval_s < self.config.flush_s
            ):
                # Warm enough. Or at the target in mild weather, when the last day's
                # share of an interval is shorter than a pulse, which would overheat.
                valve_open, reason = False, REASON_PULSE_SKIPPED
            else:
                self._pulse = _Pulse()  # timed from the opening, once it is seen
                valve_open, reason = True, REASON_PULSE_FLUSH
        else:
            if self._tick:
                pulse.extended = True
            if pulse.anchor is None or now - pulse.anchor < self.config.flush_s:
                # Only a room well above the target cuts the flush short
                valve_open = room < target + FLUSH_SKIP_MARGIN - _EPS
                reason = REASON_PULSE_FLUSH if valve_open else REASON_PULSE_SKIPPED
            elif ret >= threshold - _EPS:
                valve_open, reason = False, REASON_RETURN_REACHED
            elif self._tick and warm:
                valve_open, reason = False, REASON_PULSE_SKIPPED
            else:
                # No longest pulse: a cold floor may heat on through a cold spell
                valve_open = True
                reason = REASON_PULSE_EXTENDED if pulse.extended else REASON_PULSE
        if not valve_open:
            self._end_pulse(reason)
        return Decision(
            valve_open,
            reason,
            return_threshold=threshold,
            next_pulse=self._next_pulse(open_now, elapsed, now, next_pulse),
        )

    def _room_lost(
        self, inp: Inputs, ret: float, open_now: bool, now: float, next_pulse: float
    ) -> Decision:
        """Pulse to the return target without the room's term.

        Without the room nothing skips a pulse, and nothing ends one that never
        reaches T, so the valve opens at most for the share of each interval that it
        was open over the last day, as in the duty hold. c waits for the room.
        """
        self._take_over(FALLBACK_ROOM_SENSOR)
        self._integral = None
        on_s = hold_on_time(self._frozen_share(), self.grid.interval_s)
        end = self.grid.last(now) + on_s
        threshold = self._threshold(inp, None)
        pulse = self._pulse
        if pulse is not None and pulse.anchor is None and self._tick:
            pulse = self._pulse = None  # the valve never opened: decide afresh
        if pulse is None:
            # As in the duty hold, a grid point seen too late is let go
            if not open_now and self._tick and end - now >= HOLD_MIN_S / 2:
                self._pulse = _Pulse()
                valve_open, reason = True, REASON_PULSE_FLUSH
            else:
                valve_open, reason = False, self._closed_reason
        elif now >= end:
            valve_open, reason = False, REASON_WAITING  # this interval's share is used
        else:
            if self._tick:
                pulse.extended = True
            if pulse.anchor is None or now - pulse.anchor < self.config.flush_s:
                valve_open, reason = True, REASON_PULSE_FLUSH
            elif ret >= threshold - _EPS:
                valve_open, reason = False, REASON_RETURN_REACHED
            else:
                valve_open = True
                reason = REASON_PULSE_EXTENDED if pulse.extended else REASON_PULSE
        if not valve_open:
            self._end_pulse(reason)
        return Decision(
            valve_open,
            reason,
            fallback=FALLBACK_ROOM_SENSOR,
            # The most it opens, as rounded: the return target may end a pulse sooner
            fallback_share=on_s / self.grid.interval_s,
            return_threshold=threshold,
            next_pulse=next_pulse,
        )

    def _timed_pulses(
        self, inp: Inputs, open_now: bool, now: float, next_pulse: float
    ) -> Decision:
        """Open for a share of each interval, without the return sensor.

        With the room, the share follows the room. Without it too, it is the share of
        the time the valve was open over the last day.
        """
        if inp.room is None:
            self._integral = None
            share, fallback = self._frozen_share(), FALLBACK_SENSORS
        else:
            share, fallback = self._room_pi(inp), FALLBACK_RETURN_SENSOR
        self._take_over(fallback)
        if self.last_decision is None or not self.last_decision.valve_open:
            # A valve let close opens again only at a grid point, also when the share
            # grows, e.g. as the room sensor comes back
            self._hold_pulse = None
        valve_open, on_s = self._timed_opening(share, open_now, now)
        if not valve_open:
            self._end_pulse(REASON_WAITING)
        return Decision(
            valve_open,
            REASON_TIMED_PULSE if valve_open else REASON_WAITING,
            fallback=fallback,
            fallback_share=on_s / self.grid.interval_s,  # as rounded
            next_pulse=next_pulse,
        )

    def _room_pi(self, inp: Inputs) -> float:
        """Return the share of this interval to open by the room alone.

        0.5 per °C below the target, plus an integral that starts at the last day's
        share and moves at each grid point with an 8 h integral time. It stops while
        the share is at a limit and the room pushes it further. The share is set for
        the whole interval: at its grid point, or when the fallback starts.
        """
        error = inp.target - inp.room
        if self._integral is None:
            self._integral = self.calibration.duty
            self._room_share = None
        if self._tick:
            share = self._integral + FALLBACK_GAIN * error
            if not ((share >= 1.0 and error > 0) or (share <= 0.0 and error < 0)):
                step = FALLBACK_GAIN * error * self.grid.interval_s
                step /= FALLBACK_INTEGRAL_S
                self._integral = min(max(self._integral + step, 0.0), 1.0)
            self._room_share = None
        if self._room_share is None:
            share = self._integral + FALLBACK_GAIN * error
            self._room_share = min(max(share, 0.0), 1.0)
        return self._room_share

    def _end_pulse(self, reason: str) -> None:
        """End the pulse; the valve stays closed for this reason until a grid point."""
        self._pulse = None
        self._closed_reason = reason

    def _take_over(self, fallback: str | None) -> None:
        """Note which rules decide: the pulses (None), or a fallback.

        When one takes over from another, why the valve was closed no longer holds,
        e.g. a pulse skipped for a warm room once the room sensor is lost: a closed
        valve waits for the next grid point.
        """
        if fallback != self._fallback:
            self._fallback = fallback
            self._closed_reason = REASON_WAITING

    def _extra_tick(self, open_now: bool, elapsed: float, now: float) -> bool:
        """Return True if a target rise's extra grid point is due now.

        It comes at most once per interval, and only once the valve has been closed
        for 10 minutes. Until then it waits, unless a grid point comes first.
        """
        due, point = self._extra_tick_at, self.grid.last(now)
        if (
            due is None
            or now < due
            or open_now
            or elapsed < RISE_TICK_CLOSED_S
            or self._extra_tick_point == point
        ):
            return False
        self._extra_tick_at = None
        self._extra_tick_point = point
        return True

    def _next_pulse(
        self, open_now: bool, elapsed: float, now: float, point: float
    ) -> float:
        """Return when the next pulse may start.

        That is the next grid point, or a target rise's extra grid point if it comes
        first: once its delay is over and the valve has been closed for 10 minutes.
        """
        due = self._extra_tick_at
        if due is None or open_now or self._extra_tick_point == self.grid.last(now):
            # None is due, the valve has yet to close, or this interval had one
            return point
        return min(max(due, now + RISE_TICK_CLOSED_S - elapsed), point)

    def _threshold(self, inp: Inputs, room: float | None) -> float:
        """Return the return target T; without a room reading, without its term."""
        target, outside = inp.target, self.outside_temperature
        threshold = target + self.calibration.return_offset
        gain = ROOM_GAIN_NO_OUTSIDE
        if outside is not None:
            threshold += OUTSIDE_SLOPE * (target - outside)
            gain = ROOM_GAIN
        if room is not None:
            threshold -= gain * (room - target)
        return min(threshold, self.config.max_return - RETURN_MARGIN)

    def _calibrate(self, inp: Inputs, dt: float, now: float) -> None:
        """Move the return offset slowly against the room's error.

        Only in normal pulsing near the target, on a fresh room reading, with hot water
        in the loop, and not in a preset that isn't meant to be held: otherwise the
        error says little about c.
        """
        error = inp.room - inp.target
        reported = inp.room_updated_at
        self.calibrating = (
            inp.calibrate
            and abs(error) <= CALIBRATION_BAND + _EPS
            # While the room sensor's reports are checked, a reading it hasn't
            # reported for an hour may have stopped, as in the open share
            and (
                not self.config.room_silent_s
                or reported is None
                or now - reported <= DUTY_FRESH_S
            )
            # A freshly aired room is cooler than the floor's heat says
            and (
                self._window_closed_at is None
                or now - self._window_closed_at >= WINDOW_SETTLE_S
            )
            and not self._no_heat
            and not self.loop_saturated
        )
        if self.calibrating:
            error = min(max(error, -CALIBRATION_BAND), CALIBRATION_BAND)
            self.calibration.adjust_offset(-CALIBRATION_RATE * error * dt)

    def _outside(self, inp: Inputs, now: float) -> float | None:
        """Return the outdoor temperature in use: the reading, or the last one seen.

        A reading that can't be real, such as a faulty sensor's -127 °C, counts as
        missing.
        """
        if not self.config.has_outside_sensor:
            return None
        calibration = self.calibration
        if plausible_outside(inp.outside):
            # Saved with the calibration, so it also bridges a restart
            calibration.see_outside(inp.outside, now)
            return inp.outside
        seen_at = calibration.last_outside_at
        if (
            calibration.last_outside is not None
            and seen_at is not None
            # Not a time ahead of the clock either, e.g. saved while it was wrong
            and 0.0 <= now - seen_at <= OUTSIDE_HOLD_S
        ):
            return calibration.last_outside
        return None

    def _settle_offset(self, inp: Inputs) -> None:
        """Start the return offset, or convert it when the outdoor term comes or goes."""
        slope = 0.0 if self.outside_temperature is None else OUTSIDE_SLOPE
        calibration = self.calibration
        if calibration.return_offset is None:
            calibration.seed_offset(
                self.model, slope, inp.target, self.outside_temperature
            )
        elif calibration.offset_slope != slope:
            calibration.set_offset_slope(slope)

    def _track_return(self, inp: Inputs, now: float) -> float | None:
        """Return the return reading, or None while it is missing or not responding.

        A return sensor that hasn't moved within the flush + 15 minutes of an opening
        isn't reading the loop's water, or has stopped: it counts as missing until its
        reading changes. The maximum return rule still uses the reading itself.
        """
        reading = inp.return_temp
        pulse = self._pulse
        if (
            self._stuck_return is not None
            and reading is not None
            and abs(reading - self._stuck_return) >= RETURN_MOVE - _EPS
        ):
            self._stuck_return = None
            if pulse is not None:
                pulse.return_moved = True
        if (
            reading is not None
            and self.valve_open
            and pulse is not None
            and pulse.anchor is not None
        ):
            if pulse.first_return is None:
                pulse.first_return = pulse.low_return = reading
                pulse.first_return_at = now
            else:
                pulse.low_return = min(pulse.low_return, reading)
                if abs(reading - pulse.first_return) >= RETURN_MOVE - _EPS:
                    pulse.return_moved = True
            # Watched from the opening, or from the first reading if that came later
            watched = now - max(pulse.anchor, pulse.first_return_at)
            if (
                not pulse.return_moved
                and self._stuck_return is None
                and watched >= self.config.flush_s + RETURN_SILENT_S
            ):
                self._stuck_return = reading
                # Missing since the opening: no need to wait any longer
                self._return_lost_since = pulse.anchor
        if reading is None or self._stuck_return is not None:
            if self._return_lost_since is None:
                self._return_lost_since = now
            return None
        self._return_lost_since = None
        return reading

    def _track_heat(self, inp: Inputs, now: float) -> None:
        """Remember a lack of hot water until a pulse warms the return again.

        The supply check is only made while the valve is open and forgets as it
        closes, but a boiler that has stopped stays stopped between pulses.
        """
        if self.no_heat_source:
            self._no_heat = True
            return
        pulse = self._pulse
        if (
            self._no_heat
            and pulse is not None
            and pulse.low_return is not None
            and inp.return_temp is not None
            and inp.return_temp - pulse.low_return >= HEAT_RISE - _EPS
            # once the supply has been checked in this opening, too
            and self._open_started is not None
            and now - self._open_started >= NO_HEAT_DELAY_S
        ):
            self._no_heat = False
