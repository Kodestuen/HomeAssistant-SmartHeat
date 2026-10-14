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
is lost.

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


@dataclass
class ZoneCalibration:
    """Slowly measured facts about a zone, kept for when a sensor is lost."""

    duty: float = INITIAL_DUTY  # share of the time the valve is open, 24 h average
    revision: int = field(default=0, compare=False)  # bumped on every lasting change
    _revision_duty: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._revision_duty = self.duty

    def add_valve_time(self, valve_open: bool, dt: float) -> None:
        """Count dt seconds of the valve open or closed into the open share."""
        sample = 1.0 if valve_open else 0.0
        # A clock step backwards gives a negative time, which counts as none
        self.duty += (sample - self.duty) * (1 - math.exp(-max(dt, 0.0) / DUTY_TAU_S))
        # The share moves a little every minute: only a lasting change is worth saving
        if abs(self.duty - self._revision_duty) >= DUTY_REVISION_STEP:
            self._revision_duty = self.duty
            self.revision += 1

    def to_dict(self) -> dict:
        """Serialise the calibration for storage."""
        return {"duty": self.duty}

    @classmethod
    def from_dict(cls, data: dict | None) -> ZoneCalibration:
        """Restore a calibration saved with to_dict; anything unusable starts afresh."""
        data = data if isinstance(data, dict) else {}
        try:
            duty = float(data.get("duty", INITIAL_DUTY))
        except (TypeError, ValueError):
            duty = INITIAL_DUTY
        if not math.isfinite(duty):
            duty = INITIAL_DUTY
        return cls(duty=min(max(duty, 0.0), 1.0))
