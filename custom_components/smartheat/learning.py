"""Overshoot learning for SmartHeat.

After the valve closes, the heat stored in the floor or radiator keeps warming the
room. The return temperature tells how warm the emitter is: x = T_return - T_room.

To merely hold the room temperature, the emitter has to stay some h degrees above the
room (h depends on the heat loss, so on the weather). Only heat above that level
pushes the room up. With the emitter cooling exponentially once the flow stops, the
room rises by

    overshoot = beta * ((x - h) - h * ln(x / h))      for x > h, else 0

where beta is the heat capacity of the emitter relative to the room. Both h and beta
are learned:

- h from the low point of the room right after the valve opens: at that moment the
  emitter just covers the heat loss, so x there equals h. Water is flowing, so the
  return sensor reads the emitter, not a cold pipe.
- beta from the overshoot actually measured after each predictive close.

Until enough cycles exist, a flat average of the measured overshoot is used instead.

Besides the overshoot model, a zone keeps a calibration: how much of the time its valve
is open, averaged over about a day. It lets the zone keep heating when the room sensor
is lost. A zone on tickle pulses also keeps its return offset c there, and the last
outdoor temperature it saw.

Pure Python (no Home Assistant imports) so it can be unit tested anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

ALPHA = 0.2  # EWMA weight of a new sample
MIN_BETA_SAMPLES = 3  # samples needed before the model is trusted
MAX_OVERSHOOT = 1.5  # °C, upper bound for predictions and samples
BETA_MAX = 20.0  # sanity limit for beta
MIN_SHAPE = 0.05  # cycles with less excess heat than this can't teach beta
MIN_HOLDING_DELTA = 0.2  # °C, smaller valley readings are sensor artefacts
INITIAL_DUTY = 0.4  # open share assumed before the valve has been watched
DUTY_TAU_S = 86400.0  # time constant of the open share average
DUTY_REVISION_STEP = 0.01  # the open share must move this much to count as a change
# The tickle's return offset c, in °C. With an outdoor term, c starts lower by that
# term at the reference.
OUTSIDE_SLOPE = 0.15  # the outdoor term: T rises this much per °C below the target
OFFSET_DEFAULT = 7.5  # the start without an outdoor term
OFFSET_REFERENCE = 21.0  # target - outdoor at which both forms of c give the same T
OFFSET_MIN = -10.0
OFFSET_MAX = 25.0
SEED_MIN_CYCLES = 3  # cycles the thermostat must have learned before h seeds c
SEED_BELOW = 3.0  # a seed from h stays within the default - 3 °C ...
SEED_ABOVE = 5.0  # ... and the default + 5 °C
OFFSET_REVISION_STEP = 0.05  # c must move this much to count as a change
OUTSIDE_LIMIT = 60.0  # °C; an outdoor reading further from 0 °C is a sensor fault
OUTSIDE_REVISION_STEP = 0.5  # the outdoor reading must move this much, ...
OUTSIDE_REVISION_S = 3600.0  # ... or be this much newer, to count as a change


def excess_heat(x: float, h: float) -> float:
    """Return the shape term (x - h) - h ln(x / h), or 0 if x <= h."""
    if h <= 0 or x <= h:
        return 0.0
    return (x - h) - h * math.log(x / h)


def _ewma(old: float, sample: float) -> float:
    return old + ALPHA * (sample - old)


@dataclass
class OvershootModel:
    """Learned overshoot behaviour of one zone."""

    initial_overshoot: float
    flat_overshoot: float | None = None
    beta: float | None = None
    beta_samples: int = 0
    holding_delta: float | None = None  # h: return minus room needed to hold the room
    cycles: int = 0
    heat_up_rate: float | None = None  # °C per hour while the valve is open
    last_overshoot: float | None = None
    last_peak_error: float | None = None  # room peak minus target, last cycle
    revision: int = field(default=0, compare=False)  # bumped on every change

    def __post_init__(self) -> None:
        if self.flat_overshoot is None:
            self.flat_overshoot = self.initial_overshoot

    @property
    def trained(self) -> bool:
        """Return True when predictions come from the learned beta and h."""
        return (
            self.beta is not None
            and self.beta_samples >= MIN_BETA_SAMPLES
            and self.holding_delta is not None
        )

    def predict(self, room: float, return_temp: float | None) -> float:
        """Predict how far the room will rise if the valve closes now."""
        if return_temp is None or not self.trained:
            value = self.flat_overshoot
        else:
            value = self.beta * excess_heat(return_temp - room, self.holding_delta)
        return min(max(value, 0.0), MAX_OVERSHOOT)

    def add_cycle(
        self,
        overshoot: float,
        room_close: float,
        return_close: float | None,
        peak_error: float | None = None,
    ) -> None:
        """Learn from one completed close-and-coast cycle."""
        overshoot = min(max(overshoot, 0.0), MAX_OVERSHOOT)
        self.cycles += 1
        self.last_overshoot = overshoot
        self.last_peak_error = peak_error
        self.flat_overshoot = _ewma(self.flat_overshoot, overshoot)
        if return_close is not None and self.holding_delta is not None:
            shape = excess_heat(return_close - room_close, self.holding_delta)
            if shape >= MIN_SHAPE:
                sample = min(overshoot / shape, BETA_MAX)
                self.beta_samples += 1
                if self.beta is None:
                    self.beta = sample
                elif self.beta_samples <= MIN_BETA_SAMPLES:
                    # plain mean of the first samples, so the first one gets no extra weight
                    self.beta += (sample - self.beta) / self.beta_samples
                else:
                    self.beta = _ewma(self.beta, sample)
        self.revision += 1

    def add_holding_delta(self, delta: float) -> None:
        """Learn h from the return-room difference at the room's low point."""
        if delta < MIN_HOLDING_DELTA:
            return
        if self.holding_delta is None:
            self.holding_delta = delta
        else:
            self.holding_delta = _ewma(self.holding_delta, delta)
        self.revision += 1

    def add_heat_up_rate(self, rate: float) -> None:
        """Learn the room warm-up rate (°C/h) of one heating period."""
        if self.heat_up_rate is None:
            self.heat_up_rate = rate
        else:
            self.heat_up_rate = _ewma(self.heat_up_rate, rate)
        self.revision += 1

    def reset(self) -> None:
        """Forget everything learned."""
        self.flat_overshoot = self.initial_overshoot
        self.beta = None
        self.beta_samples = 0
        self.holding_delta = None
        self.cycles = 0
        self.heat_up_rate = None
        self.last_overshoot = None
        self.last_peak_error = None
        self.revision += 1

    def to_dict(self) -> dict:
        """Serialise the learned values for storage."""
        return {
            "flat_overshoot": self.flat_overshoot,
            "beta": self.beta,
            "beta_samples": self.beta_samples,
            "holding_delta": self.holding_delta,
            "cycles": self.cycles,
            "heat_up_rate": self.heat_up_rate,
            "last_overshoot": self.last_overshoot,
            "last_peak_error": self.last_peak_error,
        }

    @classmethod
    def from_dict(cls, data: dict | None, initial_overshoot: float) -> OvershootModel:
        """Restore a model saved with to_dict."""
        data = data or {}
        return cls(
            initial_overshoot=initial_overshoot,
            flat_overshoot=data.get("flat_overshoot"),
            beta=data.get("beta"),
            beta_samples=data.get("beta_samples", 0),
            holding_delta=data.get("holding_delta"),
            cycles=data.get("cycles", 0),
            heat_up_rate=data.get("heat_up_rate"),
            last_overshoot=data.get("last_overshoot"),
            last_peak_error=data.get("last_peak_error"),
        )


def _finite(value: object) -> float | None:
    """Return a stored value as a finite number, or None if it isn't one."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def offset_default(slope: float) -> float:
    """Return the return offset c to start from when h can't be used."""
    return OFFSET_DEFAULT - slope * OFFSET_REFERENCE


def plausible_outside(value: float | None) -> bool:
    """Return True for an outdoor temperature that can be real.

    A faulty sensor may report, for example, -127 °C or 85 °C.
    """
    return value is not None and abs(value) <= OUTSIDE_LIMIT


@dataclass
class ZoneCalibration:
    """Slowly measured facts about a zone, kept for when a sensor is lost.

    The tickle keeps its return offset c here too. c goes with an outdoor slope: with
    an outdoor temperature, the return target has a term slope * (target - outdoor),
    and c is lower by that term at the reference, so both forms give the same target.
    """

    duty: float = INITIAL_DUTY  # share of the time the valve is open, 24 h average
    return_offset: float | None = None  # c, °C; None until the tickle starts it
    offset_slope: float | None = None  # the outdoor slope c goes with; 0 without
    last_outside: float | None = None  # the last outdoor temperature seen, °C
    last_outside_at: float | None = None  # when it was seen, s since the epoch
    revision: int = field(default=0, compare=False)  # bumped on every lasting change
    _revision_duty: float = field(init=False, repr=False, compare=False)
    _revision_offset: float | None = field(init=False, repr=False, compare=False)
    _revision_outside: float | None = field(init=False, repr=False, compare=False)
    _revision_outside_at: float | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._revision_duty = self.duty
        self._revision_offset = self.return_offset
        self._revision_outside = self.last_outside
        self._revision_outside_at = self.last_outside_at

    def add_valve_time(self, valve_open: bool, dt: float) -> None:
        """Count dt seconds of the valve open or closed into the open share."""
        sample = 1.0 if valve_open else 0.0
        # A clock step backwards gives a negative time, which counts as none
        self.duty += (sample - self.duty) * (1 - math.exp(-max(dt, 0.0) / DUTY_TAU_S))
        # The share moves a little every minute: only a lasting change is worth saving
        if abs(self.duty - self._revision_duty) >= DUTY_REVISION_STEP:
            self._revision_duty = self.duty
            self.revision += 1

    def seed_offset(
        self,
        model: OvershootModel,
        slope: float,
        target: float,
        outside: float | None,
    ) -> None:
        """Start the return offset from the thermostat's learned h, if it is trusted.

        While the room is held, the return sits about h above it, so c starts at h less
        the outdoor term (outdoor 0 °C if unknown). h counts only after a few learned
        cycles, and a seed far from the default is cut back: h may come from a single
        low point, or from another season.
        """
        default = offset_default(slope)
        offset = default
        if model.holding_delta is not None and model.cycles >= SEED_MIN_CYCLES:
            outdoor = 0.0 if outside is None else outside
            seed = model.holding_delta - slope * (target - outdoor)
            offset = min(max(seed, default - SEED_BELOW), default + SEED_ABOVE)
        self.offset_slope = slope
        self._set_offset(offset)

    def adjust_offset(self, change: float) -> None:
        """Move the return offset by change, within its bounds."""
        self._set_offset(self.return_offset + change)

    def set_offset_slope(self, slope: float) -> None:
        """Convert the return offset to go with another outdoor slope.

        The return target stays the same at the reference, 21 °C between the target and
        the outdoor temperature, so it doesn't jump when the outdoor term comes or goes.
        """
        change = (self.offset_slope - slope) * OFFSET_REFERENCE
        self.offset_slope = slope
        self._set_offset(self.return_offset + change)

    def reset_offset(self) -> None:
        """Forget the return offset; the tickle starts it afresh."""
        self.offset_slope = None
        self._set_offset(None)

    def see_outside(self, outside: float, at: float) -> None:
        """Remember the outdoor temperature seen at a time, for when it is missing."""
        self.last_outside, self.last_outside_at = outside, at
        last, last_at = self._revision_outside, self._revision_outside_at
        # Seen at every step: worth saving when it has moved, or now and then, so that
        # after a restart the value held is a recent one
        if (
            last is None
            or last_at is None
            or abs(outside - last) >= OUTSIDE_REVISION_STEP
            or abs(at - last_at) >= OUTSIDE_REVISION_S
        ):
            self._revision_outside, self._revision_outside_at = outside, at
            self.revision += 1

    def _set_offset(self, offset: float | None) -> None:
        if offset is not None:
            offset = min(max(offset, OFFSET_MIN), OFFSET_MAX)
        self.return_offset = offset
        last = self._revision_offset
        # c moves a little every minute: only a lasting change is worth saving
        if (offset is None) != (last is None) or (
            offset is not None and abs(offset - last) >= OFFSET_REVISION_STEP
        ):
            self._revision_offset = offset
            self.revision += 1

    def to_dict(self) -> dict:
        """Serialise the calibration for storage."""
        return {
            "duty": self.duty,
            "return_offset": self.return_offset,
            "offset_slope": self.offset_slope,
            "last_outside": self.last_outside,
            "last_outside_at": self.last_outside_at,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> ZoneCalibration:
        """Restore a calibration saved with to_dict; anything unusable starts afresh.

        Keys it doesn't know are left out, so a file from a newer version still loads.
        """
        data = data if isinstance(data, dict) else {}
        duty = _finite(data.get("duty"))
        offset = _finite(data.get("return_offset"))
        slope = _finite(data.get("offset_slope"))
        if offset is None or slope not in (0.0, OUTSIDE_SLOPE):
            # c is of no use without the slope it goes with, one the tickle uses
            offset = slope = None
        outside = _finite(data.get("last_outside"))
        outside_at = _finite(data.get("last_outside_at"))
        if not plausible_outside(outside) or outside_at is None:
            outside = outside_at = None
        return cls(
            duty=INITIAL_DUTY if duty is None else min(max(duty, 0.0), 1.0),
            return_offset=(
                None if offset is None else min(max(offset, OFFSET_MIN), OFFSET_MAX)
            ),
            offset_slope=slope,
            last_outside=outside,
            last_outside_at=outside_at,
        )
