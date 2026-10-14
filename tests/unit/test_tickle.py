"""Tests for the tickle pulses."""

import pytest

from smartheat_core.controller import (
    FALLBACK_ROOM_SENSOR,
    REASON_FROST,
    REASON_MAX_RETURN,
    REASON_OFF,
    REASON_ROOM_SENSOR_WAIT,
    REASON_WINDOW,
    Decision,
    Inputs,
)
from smartheat_core.learning import OvershootModel, ZoneCalibration
from smartheat_core.tickle import (
    FALLBACK_RETURN_SENSOR,
    FALLBACK_SENSORS,
    REASON_PULSE,
    REASON_PULSE_EXTENDED,
    REASON_PULSE_FLUSH,
    REASON_PULSE_SKIPPED,
    REASON_RETURN_REACHED,
    REASON_RETURN_SENSOR_WAIT,
    REASON_TIMED_PULSE,
    REASON_WAITING,
    TickleConfig,
    TickleController,
)


class TickleZone:
    """Drives a tickle controller the way Home Assistant does; the valve obeys at once.

    Defaults: target 21 °C, 0 °C outdoors, grid points every 45 min from minute 0, a
    12 min flush, maximum return 45 °C. The return offset c starts at 4.35, so the
    return target is T = 21 + 4.35 + 0.15 * 21 - 3 * (room - 21), 28.5 at 21 °C.
    """

    def __init__(
        self,
        calibration: ZoneCalibration | None = None,
        model: OvershootModel | None = None,
        **config,
    ) -> None:
        self.model = OvershootModel(initial_overshoot=0.4) if model is None else model
        self.calibration = ZoneCalibration() if calibration is None else calibration
        config.setdefault("has_outside_sensor", True)
        self.ctrl = TickleController(
            TickleConfig(**config), self.model, self.calibration
        )
        self.now = 0.0
        self.valve: bool | None = False
        self.changed_at: float | None = -1e6  # closed long ago

    def step(self, room, ret, minutes=1.0, target=21.0, outside=0.0, **extra):
        self.now += minutes * 60
        decision = self.ctrl.update(
            Inputs(
                room=room,
                return_temp=ret,
                target=target,
                valve_open=self.valve,
                valve_changed_at=self.changed_at,
                outside=outside,
                **extra,
            ),
            self.now,
        )
        if decision.valve_open != self.valve:
            self.valve = decision.valve_open
            self.changed_at = self.now
        return decision


def start_pulse(zone: TickleZone, room=20.8, ret=25.0, **extra) -> Decision:
    """Start up at minute 1, then step to the grid point at minute 45."""
    zone.step(room, ret, **extra)
    return zone.step(room, ret, minutes=44, **extra)


def offset_moved(zone: TickleZone, room=20.5, ret=25.0, minutes=1.0, **extra) -> bool:
    """Step on; return True if the return offset c moved."""
    before = zone.ctrl.return_offset
    zone.step(room, ret, minutes=minutes, **extra)
    return zone.ctrl.return_offset != before


def test_start_up_waits_for_the_grid_point():
    zone = TickleZone()
    decision = zone.step(20.0, 25.0)  # minute 1: a cold room
    assert not decision.valve_open
    assert decision.reason == REASON_WAITING
    assert decision.next_pulse == 2700
    assert decision.fallback is None
    assert decision.predicted_overshoot is None
    assert decision.close_threshold is None
    for _ in range(43):  # minutes 2-44
        assert zone.step(20.0, 25.0).reason == REASON_WAITING
    decision = zone.step(20.0, 25.0)  # minute 45
    assert decision.valve_open
    assert decision.reason == REASON_PULSE_FLUSH
    assert decision.next_pulse == 5400


def test_pulses_follow_the_zones_interval_and_phase():
    zone = TickleZone(hold_interval_s=3600.0, grid_phase_s=1200.0)
    assert zone.step(20.0, 35.0).next_pulse == 1200  # minute 1
    opened = []
    for minute in range(2, 150):
        was_open = zone.valve
        zone.step(20.0, 35.0)  # the return is above T as soon as the flush ends
        if zone.valve and not was_open:
            opened.append(minute)
    assert opened == [20, 80, 140]


def test_flush_runs_from_the_opening_whatever_the_return_reads():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: opens
    for _ in range(11):  # minutes 46-56
        decision = zone.step(20.8, 44.0)
        assert decision.valve_open
        assert decision.reason == REASON_PULSE_FLUSH
    decision = zone.step(20.8, 44.0)  # minute 57: 12 minutes after the opening
    assert not decision.valve_open
    assert decision.reason == REASON_RETURN_REACHED


def test_closes_at_the_exact_return_target():
    zone = TickleZone()
    decision = start_pulse(zone, calibrate=False)  # c stays at 4.35
    # 21 + 4.35 + 0.15 * (21 - 0) - 3 * (20.8 - 21)
    assert decision.return_threshold == pytest.approx(29.1)
    decision = zone.step(20.8, 29.05, minutes=12, calibrate=False)  # minute 57
    assert decision.valve_open
    assert decision.reason == REASON_PULSE
    decision = zone.step(20.8, 29.1, calibrate=False)
    assert not decision.valve_open
    assert decision.reason == REASON_RETURN_REACHED
    assert decision.return_threshold == pytest.approx(29.1)
    # Closed until the next grid point, however cold the return gets
    decision = zone.step(20.8, 24.0, minutes=31, calibrate=False)  # minute 89
    assert decision.reason == REASON_RETURN_REACHED
    assert zone.step(20.8, 24.0, calibrate=False).valve_open  # minute 90


@pytest.mark.parametrize(
    ("room", "outside", "sensor", "threshold"),
    [
        (20.8, 0.0, True, 29.1),
        (21.0, 0.0, True, 28.5),
        (21.0, -10.0, True, 30.0),  # 21 + 4.35 + 0.15 * 31
        (21.5, 0.0, True, 27.0),
        (15.0, -20.0, True, 43.0),  # 49.5, but 2 °C below the maximum return at most
        (20.8, None, False, 29.3),  # without an outdoor sensor: 21 + 7.5 + 4 * 0.2
        (21.0, 5.0, False, 28.5),  # its reading is ignored without the sensor set
    ],
)
def test_return_target(room, outside, sensor, threshold):
    zone = TickleZone(has_outside_sensor=sensor)
    decision = zone.step(room, 25.0, outside=outside, calibrate=False)
    assert decision.return_threshold == pytest.approx(threshold)


def test_a_cold_floor_heats_on_past_grid_points():
    zone = TickleZone()
    start_pulse(zone, 20.0)  # minute 45: T is 31.5
    reasons = [zone.step(20.0, 26.0 + i % 2 / 10).reason for i in range(180)]  # 3 h
    assert zone.valve  # no longest pulse
    assert reasons[:11] == [REASON_PULSE_FLUSH] * 11  # minutes 46-56
    assert reasons[11:44] == [REASON_PULSE] * 33  # minutes 57-89
    assert reasons[44:] == [REASON_PULSE_EXTENDED] * 136  # from the grid point at 90


def test_warm_room_skips_the_pulse_at_a_grid_point():
    zone = TickleZone()
    zone.step(21.3, 25.0)
    decision = zone.step(21.3, 25.0, minutes=44)  # minute 45: 0.3 above the target
    assert not decision.valve_open
    assert decision.reason == REASON_PULSE_SKIPPED
    for _ in range(44):  # minutes 46-89: nothing opens before the next grid point
        assert zone.step(20.0, 25.0).reason == REASON_PULSE_SKIPPED
    assert zone.step(20.0, 25.0).valve_open  # minute 90


def test_warm_room_ends_a_pulse_at_the_next_grid_point():
    zone = TickleZone()
    start_pulse(zone, 20.5)  # minute 45
    decision = zone.step(21.29, 26.0, minutes=44)  # minute 89: below T
    assert decision.valve_open
    assert decision.reason == REASON_PULSE
    decision = zone.step(21.3, 26.0)  # minute 90: grid point
    assert not decision.valve_open
    assert decision.reason == REASON_PULSE_SKIPPED


@pytest.mark.parametrize(("room", "stays_open"), [(21.5, True), (21.6, False)])
def test_only_a_room_well_above_the_target_cuts_the_flush_short(room, stays_open):
    zone = TickleZone()
    start_pulse(zone)
    decision = zone.step(room, 25.0, minutes=5)  # minute 50: in the flush
    assert decision.valve_open is stays_open
    assert decision.reason == (
        REASON_PULSE_FLUSH if stays_open else REASON_PULSE_SKIPPED
    )


@pytest.mark.parametrize(
    ("duty", "room", "opens"),
    [
        (0.2, 21.0, False),  # 9 min of each 45: shorter than a 12 min pulse
        (0.2, 20.9, True),  # below the target, a pulse is needed anyway
        (0.3, 21.0, True),  # 13.5 min: a pulse fits
    ],
)
def test_mild_weather_skips_a_pulse_at_the_target(duty, room, opens):
    zone = TickleZone(calibration=ZoneCalibration(duty=duty))
    decision = start_pulse(zone, room)
    assert decision.valve_open is opens
    assert decision.reason == (REASON_PULSE_FLUSH if opens else REASON_PULSE_SKIPPED)


def test_target_rise_adds_a_grid_point_after_the_zones_delay():
    # Grid points at minute 15.5, 60.5 ...; the zone's delay is 930 s mod 10 min
    zone = TickleZone(grid_phase_s=930.0)
    zone.step(19.8, 25.0, target=19.0)  # minute 1: warm for the night target
    decision = zone.step(19.8, 25.0, minutes=15, target=19.0)  # minute 16
    assert decision.reason == REASON_PULSE_SKIPPED
    zone.step(19.8, 25.0, minutes=4, target=19.0)  # minute 20
    decision = zone.step(19.8, 25.0)  # minute 21: the morning target
    assert not decision.valve_open
    assert decision.next_pulse == 21 * 60 + 330  # not the grid point at 60.5
    for _ in range(5):  # minutes 22-26: within the delay of 5.5 minutes
        assert not zone.step(19.8, 25.0).valve_open
    decision = zone.step(19.8, 25.0)  # minute 27
    assert decision.valve_open
    assert decision.reason == REASON_PULSE_FLUSH
    assert decision.next_pulse == 60.5 * 60


@pytest.mark.parametrize(("rise", "opens"), [(0.5, True), (0.4, False), (-2.0, False)])
def test_only_a_target_rise_of_half_a_degree_adds_a_grid_point(rise, opens):
    zone = TickleZone()  # phase 0: no delay
    zone.step(21.4, 25.0)
    decision = zone.step(21.4, 25.0, minutes=44)  # minute 45: warm
    assert decision.reason == REASON_PULSE_SKIPPED
    zone.step(21.4, 25.0, minutes=5)  # minute 50
    assert zone.step(21.4, 25.0, target=21.0 + rise).valve_open is opens  # minute 51


def test_extra_grid_point_waits_until_the_valve_has_been_closed_10_minutes():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: opens
    decision = zone.step(20.8, 30.0, minutes=12)  # minute 57: closes at T
    assert decision.reason == REASON_RETURN_REACHED
    decision = zone.step(20.8, 25.0, minutes=2, target=21.5)  # minute 59
    assert not decision.valve_open
    assert decision.next_pulse == 67 * 60  # 10 minutes after the valve closed
    for _ in range(7):  # minutes 60-66
        assert not zone.step(20.8, 25.0, target=21.5).valve_open
    assert zone.step(20.8, 25.0, target=21.5).valve_open  # minute 67


def test_at_most_one_extra_grid_point_per_interval():
    zone = TickleZone()
    zone.step(21.4, 25.0)
    zone.step(21.4, 25.0, minutes=44)  # minute 45: skipped, warm
    assert zone.step(21.4, 25.0, target=22.0).valve_open  # minute 46: target rise
    decision = zone.step(22.7, 25.0, target=22.0)  # minute 47: far too warm, closes
    assert decision.reason == REASON_PULSE_SKIPPED
    zone.step(21.4, 25.0, minutes=12, target=21.0)  # minute 59: down again
    # Another rise, with the valve closed for 22 minutes, but in the same interval
    decision = zone.step(21.4, 25.0, minutes=10, target=22.0)  # minute 69
    assert not decision.valve_open
    assert decision.next_pulse == 5400  # the grid point
    for _ in range(20):  # minutes 70-89
        assert not zone.step(21.4, 25.0, target=22.0).valve_open
    assert zone.step(21.4, 25.0, target=22.0).valve_open  # minute 90: grid point


def test_grid_point_before_the_delay_ends_does_the_extra_one():
    zone = TickleZone(grid_phase_s=930.0)  # grid points at minute 15.5, 60.5 ...
    zone.step(20.0, 25.0, target=19.0)  # minute 1: warm for the night target
    zone.step(20.0, 25.0, minutes=57, target=19.0)  # minute 58
    assert not zone.step(20.0, 25.0).valve_open  # minute 59: rise, due at 64.5
    decision = zone.step(20.0, 25.0, minutes=2)  # minute 61: the grid point
    assert decision.valve_open
    decision = zone.step(20.0, 32.0, minutes=12)  # minute 73: closes at T
    assert decision.reason == REASON_RETURN_REACHED
    for _ in range(32):  # minutes 74-105: the rise was dealt with at minute 61
        assert not zone.step(20.0, 25.0).valve_open
    assert zone.step(20.0, 25.0).valve_open  # minute 106: the next grid point


def test_target_rise_just_before_a_safety_rule_is_dropped():
    zone = TickleZone(grid_phase_s=930.0)  # grid points at minute 15.5, 60.5 ...
    zone.step(20.0, 25.0, target=19.0)  # minute 1
    zone.step(20.0, 25.0, minutes=19, target=19.0)  # minute 20
    zone.step(20.0, 25.0)  # minute 21: rise, due at 26.5
    zone.step(20.0, 25.0, window_open=True)  # minute 22: aired
    zone.step(20.0, 25.0)  # minute 23: closed again
    for _ in range(37):  # minutes 24-60
        assert not zone.step(20.0, 25.0).valve_open
    assert zone.step(20.0, 25.0).valve_open  # minute 61: the grid point


@pytest.mark.parametrize(
    ("end_room", "end_return", "reason"),
    [
        (20.8, 29.2, REASON_RETURN_REACHED),  # at minute 70
        (21.3, 26.0, REASON_PULSE_SKIPPED),  # at the grid point at minute 90
    ],
)
def test_reports_of_the_same_state_do_not_restart_the_flush(
    end_room, end_return, reason
):
    zone = TickleZone()
    start_pulse(zone)  # minute 45: opens
    decisions = []
    for minute in range(46, 91):
        if minute % 10 == 0:
            # The valve reports "open" again with a new last_changed, e.g. going from
            # opening to open
            zone.changed_at = zone.now + 60
        ending = minute == 70 if reason == REASON_RETURN_REACHED else minute == 90
        ret = 26.0 + minute % 2 / 10  # a return sensor that responds
        decisions.append(
            zone.step(end_room if ending else 20.8, end_return if ending else ret)
        )
        if ending:
            break
    reasons = [d.reason for d in decisions]
    assert reasons[:11] == [REASON_PULSE_FLUSH] * 11  # minutes 46-56
    assert set(reasons[11:-1]) == {REASON_PULSE}  # timed from the real opening
    assert reasons[-1] == reason
    assert not zone.valve


def test_valve_back_from_unavailable_flushes_again_once_per_pulse():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: opens
    zone.step(20.8, 30.0, minutes=5)  # minute 50: in the flush, the return above T
    zone.valve = None  # unavailable
    assert zone.step(20.8, 30.0).reason == REASON_PULSE_FLUSH  # minute 51
    zone.valve, zone.changed_at = True, zone.now + 60  # back at minute 52, open
    for _ in range(6):  # minutes 52-57: the flush runs again from minute 52
        assert zone.step(20.8, 30.0).reason == REASON_PULSE_FLUSH
    zone.valve = None  # gone again
    zone.step(20.8, 30.0)  # minute 58
    zone.valve, zone.changed_at = True, zone.now + 60  # back at minute 59
    for _ in range(5):  # minutes 59-63: not timed afresh a second time
        assert zone.step(20.8, 30.0).reason == REASON_PULSE_FLUSH
    decision = zone.step(20.8, 30.0)  # minute 64: 12 minutes after minute 52
    assert decision.reason == REASON_RETURN_REACHED


def test_a_pulse_is_asked_for_until_the_valve_opens():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: asks to open
    for _ in range(10):  # minutes 46-55: the valve doesn't follow
        zone.valve, zone.changed_at = False, -1e6
        decision = zone.step(20.8, 30.0)
        assert decision.valve_open  # still asked for, so the command is sent again
        assert decision.reason == REASON_PULSE_FLUSH
    # It opened at minute 55: the flush runs from then
    for _ in range(11):  # minutes 56-66
        assert zone.step(20.8, 30.0).reason == REASON_PULSE_FLUSH
    assert zone.step(20.8, 30.0).reason == REASON_RETURN_REACHED  # minute 67


def test_a_pulse_the_valve_never_opened_for_is_decided_afresh_at_the_next_point():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: asks to open
    for _ in range(44):  # minutes 46-89: the valve doesn't follow
        zone.valve, zone.changed_at = False, -1e6
        assert zone.step(20.8, 25.0).valve_open
    zone.valve, zone.changed_at = False, -1e6
    decision = zone.step(21.3, 25.0)  # minute 90: warm by now
    assert not decision.valve_open
    assert decision.reason == REASON_PULSE_SKIPPED


def test_a_pulse_closed_by_hand_waits_for_the_next_grid_point():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: opens
    zone.step(20.8, 26.0, minutes=5)  # minute 50
    zone.valve, zone.changed_at = False, zone.now + 60  # closed by hand at minute 51
    for _ in range(39):  # minutes 51-89
        decision = zone.step(20.8, 26.0)
        assert not decision.valve_open
        assert decision.reason == REASON_WAITING
    assert zone.step(20.8, 26.0).valve_open  # minute 90


def test_a_safety_close_stands_while_the_valve_is_slow_to_close():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: opens
    zone.step(20.8, 26.0, minutes=5)  # minute 50: in the flush
    assert not zone.step(20.8, 45.5).valve_open  # minute 51: too hot
    zone.valve = True  # the valve hasn't closed yet
    decision = zone.step(20.8, 44.0)  # minute 52: the rule is over
    assert not decision.valve_open
    assert decision.reason == REASON_WAITING


def test_calibration_moves_c_a_tenth_of_a_degree_an_hour_per_degree():
    zone = TickleZone()
    zone.step(20.5, 25.0)  # starts c at 4.35; no time has passed yet
    assert zone.ctrl.return_offset == pytest.approx(4.35)
    assert zone.ctrl.calibrating
    zone.step(20.5, 25.0, minutes=5)  # 0.5 °C cold for 5 minutes
    offset = 4.35 + 0.1 * 0.5 * 5 / 60
    assert zone.ctrl.return_offset == pytest.approx(offset)
    zone.step(20.5, 25.0, minutes=60)  # a gap counts as 5 minutes at most
    offset += 0.1 * 0.5 * 5 / 60
    assert zone.ctrl.return_offset == pytest.approx(offset)
    zone.step(20.5, 25.0, minutes=-3)  # a clock step back counts as no time
    assert zone.ctrl.return_offset == pytest.approx(offset)
    zone.step(22.0, 25.0, minutes=5)  # 1 °C warm, the edge of the band
    offset -= 0.1 * 1.0 * 5 / 60
    assert zone.ctrl.return_offset == pytest.approx(offset)
    zone.step(22.1, 25.0, minutes=5)  # further off, the error says little about c
    assert not zone.ctrl.calibrating
    assert zone.ctrl.return_offset == pytest.approx(offset)


@pytest.mark.parametrize(
    ("start", "room", "end"), [(24.996, 20.0, 25.0), (-9.996, 22.0, -10.0)]
)
def test_return_offset_stays_within_its_bounds(start, room, end):
    calibration = ZoneCalibration(return_offset=start, offset_slope=0.15)
    zone = TickleZone(calibration=calibration)
    zone.step(room, 25.0)
    zone.step(room, 25.0, minutes=5)  # would move c 0.0083 beyond
    assert zone.ctrl.return_offset == end


@pytest.mark.parametrize(
    "extra",
    [
        {"calibrate": False},  # a preset that isn't held, such as away
        {"heating_enabled": False},
    ],
)
def test_calibration_waits(extra):
    zone = TickleZone()
    zone.step(20.5, 25.0)
    assert not offset_moved(zone, **extra)
    assert not zone.ctrl.calibrating
    assert offset_moved(zone)


def test_calibration_waits_while_a_window_is_open_and_an_hour_after():
    zone = TickleZone()
    zone.step(20.5, 25.0)  # minute 1
    assert not offset_moved(zone, window_open=True)  # minute 2: aired
    assert not offset_moved(zone)  # minute 3: closed
    assert not offset_moved(zone, minutes=59)  # minute 62: closed 59 minutes ago
    assert offset_moved(zone)  # minute 63


@pytest.mark.parametrize("silent_s", [6 * 3600, None])
def test_calibration_waits_for_a_room_reading_reported_in_the_last_hour(silent_s):
    # Only while the room sensor's reports are checked. With the check off, e.g. for a
    # sensor that reports only when its reading changes, an old report is no sign of
    # a fault: a steady room is just what calibrates c.
    zone = TickleZone(room_silent_s=silent_s)
    zone.step(20.5, 25.0, room_updated_at=0.0)
    assert offset_moved(zone, minutes=59, room_updated_at=0.0)  # reported 1 h ago
    moved = offset_moved(zone, minutes=2, room_updated_at=0.0)
    assert moved is (silent_s is None)
    assert zone.ctrl.calibrating is moved
    assert offset_moved(zone, room_updated_at=zone.now + 60)


def test_calibration_waits_while_the_loop_is_saturated():
    zone = TickleZone()
    start_pulse(zone, 20.0, outside=-20.0)  # minute 45: T is 21 + 4.35 + 6.15 + 3
    for i in range(31):  # minutes 46-76: supply and return close for half an hour
        zone.step(20.0, 33.4 + i % 2 / 10, supply=35.0, outside=-20.0)
    assert zone.valve
    assert zone.ctrl.loop_saturated
    assert not zone.ctrl.calibrating
    assert not offset_moved(zone, 20.0, 33.5, supply=35.0, outside=-20.0)


def test_no_hot_water_stops_calibration_until_a_pulse_warms_the_return():
    zone = TickleZone()
    start_pulse(zone, 20.5, supply=22.0)  # minute 45: opens, but the boiler is off
    zone.step(20.5, 25.0, minutes=5, supply=22.0)  # minute 50: the supply is checked
    assert zone.ctrl.no_heat_source
    assert not zone.ctrl.calibrating
    zone.step(20.5, 30.5, minutes=7, supply=22.0)  # minute 57: closes at T
    assert not offset_moved(zone, minutes=10)  # minute 67: c still waits ...
    assert not zone.ctrl.no_heat_source  # ... though the supply is only checked open
    zone.step(20.5, 25.0, minutes=23, supply=40.0)  # minute 90: a pulse, hot water
    zone.step(20.5, 25.0, supply=40.0)  # minute 91
    assert not offset_moved(zone, ret=26.5, supply=40.0)  # minute 92: too soon to tell
    assert offset_moved(zone, ret=26.5, supply=40.0, minutes=3)  # minute 95


@pytest.mark.parametrize(
    ("h", "cycles", "outside", "offset"),
    [
        (None, 0, 0.0, 4.35),  # nothing learned: the default
        (6.0, 2, 0.0, 4.35),  # h from fewer than 3 cycles isn't trusted
        (6.0, 3, 0.0, 2.85),  # 6 - 0.15 * (21 - 0)
        (6.0, 3, -10.0, 1.35),  # 6 - 0.15 * 31
        (0.3, 3, 0.0, 1.35),  # -2.85, cut back to the default - 3
        (15.0, 3, 0.0, 9.35),  # 11.85, cut back to the default + 5
        (6.0, 3, None, 6.0),  # without an outdoor temperature c is h
        (0.3, 3, None, 4.5),
        (15.0, 3, None, 12.5),
    ],
)
def test_return_offset_starts_from_the_thermostats_h(h, cycles, outside, offset):
    model = OvershootModel(initial_overshoot=0.4, holding_delta=h, cycles=cycles)
    zone = TickleZone(model=model, has_outside_sensor=outside is not None)
    zone.step(21.0, 25.0, outside=outside)
    assert zone.ctrl.return_offset == pytest.approx(offset)
    assert zone.calibration.offset_slope == (0.0 if outside is None else 0.15)


def test_outdoor_reading_is_held_for_48_hours_then_dropped_without_a_jump():
    zone = TickleZone()
    zone.step(21.0, 25.0, calibrate=False)  # minute 1, 0 °C
    decision = zone.step(21.0, 25.0, minutes=48 * 60, outside=None, calibrate=False)
    assert zone.ctrl.outside_temperature == 0.0  # the last reading, 48 h old
    assert decision.return_threshold == pytest.approx(28.5)
    decision = zone.step(21.0, 25.0, outside=None, calibrate=False)
    assert zone.ctrl.outside_temperature is None
    # The outdoor term goes: c takes it over, as it was at 21 °C below the target
    assert zone.ctrl.return_offset == pytest.approx(4.35 + 0.15 * 21)
    assert zone.calibration.offset_slope == 0.0
    assert decision.return_threshold == pytest.approx(28.5)
    decision = zone.step(21.0, 25.0, outside=-4.0, calibrate=False)  # back
    assert zone.ctrl.outside_temperature == -4.0
    assert zone.ctrl.return_offset == pytest.approx(4.35)
    assert zone.calibration.offset_slope == 0.15
    assert decision.return_threshold == pytest.approx(28.5 + 0.15 * 4)


def test_restart_uses_the_saved_outdoor_temperature():
    zone = TickleZone()
    zone.step(21.0, 25.0, outside=-8.0, calibrate=False)
    saved = zone.calibration.to_dict()
    restarted = TickleZone(calibration=ZoneCalibration.from_dict(saved))
    restarted.now = zone.now + 3600  # back an hour later; no weather yet
    decision = restarted.step(21.0, 25.0, outside=None, calibrate=False)
    assert restarted.ctrl.outside_temperature == -8.0
    assert restarted.ctrl.return_offset == pytest.approx(4.35)
    assert decision.return_threshold == pytest.approx(21 + 4.35 + 0.15 * 29)


def test_outdoor_reading_is_worth_saving_when_it_moves_or_ages():
    # A new revision makes Home Assistant save the calibration, so that a restart
    # holds a recent reading
    zone = TickleZone()
    zone.step(21.0, 25.0, calibrate=False)  # minute 1: c starts, 0 °C is seen
    revision = zone.calibration.revision
    zone.step(21.0, 25.0, outside=-0.4, calibrate=False)
    assert zone.calibration.revision == revision
    assert zone.calibration.last_outside == -0.4
    zone.step(21.0, 25.0, outside=-0.5, calibrate=False)  # minute 3: 0.5 °C moved
    assert zone.calibration.revision == revision + 1
    zone.step(21.0, 25.0, minutes=59, outside=-0.5, calibrate=False)
    assert zone.calibration.revision == revision + 1
    zone.step(21.0, 25.0, outside=-0.5, calibrate=False)  # minute 63: an hour later
    assert zone.calibration.revision == revision + 2
    assert zone.calibration.last_outside_at == zone.now


def test_outdoor_reading_saved_ahead_of_the_clock_is_not_held():
    # Saved while the clock was wrong: it would be held until the clock caught up
    calibration = ZoneCalibration(last_outside=-8.0, last_outside_at=1e10)
    zone = TickleZone(calibration=calibration)
    decision = zone.step(21.0, 25.0, outside=None, calibrate=False)
    assert zone.ctrl.outside_temperature is None
    assert decision.return_threshold == pytest.approx(21 + 7.5)


@pytest.mark.parametrize("reading", [-127.0, 85.0])
def test_faulty_outdoor_reading_counts_as_missing(reading):
    zone = TickleZone()
    zone.step(21.0, 25.0, outside=-3.0, calibrate=False)
    decision = zone.step(21.0, 25.0, outside=reading, calibrate=False)
    assert zone.ctrl.outside_temperature == -3.0  # the last real one, held
    assert zone.calibration.last_outside == -3.0
    assert decision.return_threshold == pytest.approx(21 + 4.35 + 0.15 * 24)


@pytest.mark.parametrize(
    ("room", "ret", "reason"),
    [
        (None, 26.0, REASON_ROOM_SENSOR_WAIT),
        (20.8, None, REASON_RETURN_SENSOR_WAIT),
        (None, None, REASON_ROOM_SENSOR_WAIT),
    ],
)
def test_a_sensor_gone_for_a_moment_leaves_the_valve_as_it_is(room, ret, reason):
    zone = TickleZone()
    start_pulse(zone)  # minute 45: opens
    zone.step(20.8, 26.0, minutes=20)  # minute 65: past the flush, below T
    for _ in range(2):  # minutes 66-67
        decision = zone.step(room, ret)
        assert decision.valve_open
        assert decision.reason == reason
        assert decision.fallback is None
        assert decision.return_threshold is None
    assert zone.step(20.8, 26.1).reason == REASON_PULSE  # minute 68: back


def test_restart_with_sensors_loading_late_waits_for_the_grid_point():
    zone = TickleZone()
    zone.now = 600.0  # Home Assistant starts at minute 10
    decision = zone.step(None, None)  # minute 11
    assert not decision.valve_open
    assert decision.reason == REASON_ROOM_SENSOR_WAIT
    assert decision.return_threshold is None
    assert zone.step(20.0, None).reason == REASON_RETURN_SENSOR_WAIT  # minute 12
    decision = zone.step(20.0, None)  # minute 13: missing for 2 minutes
    assert decision.fallback == FALLBACK_RETURN_SENSOR
    assert not decision.valve_open  # a fallback opens only at a grid point
    assert zone.step(20.0, 25.0).reason == REASON_WAITING  # minute 14: back
    for _ in range(30):  # minutes 15-44
        assert not zone.step(20.0, 25.0).valve_open
    assert zone.step(20.0, 25.0).valve_open  # minute 45


@pytest.mark.parametrize(
    ("room", "ret", "reason"),
    [
        (20.8, 25.0, REASON_PULSE_FLUSH),  # the room reading is back
        (20.8, None, REASON_TIMED_PULSE),  # the return is still missing: a fallback
    ],
)
def test_grid_point_passing_while_a_sensor_is_awaited_counts_after(room, ret, reason):
    zone = TickleZone()
    zone.step(20.8, 25.0)  # minute 1
    zone.step(20.8, 25.0, minutes=42)  # minute 43
    lost = (None, 25.0) if ret is not None else (20.8, None)
    for _ in range(2):  # minutes 44-45: missing as the grid point passes
        assert not zone.step(*lost).valve_open
    decision = zone.step(room, ret)  # minute 46: the wait is over
    assert decision.valve_open
    assert decision.reason == reason


def test_open_request_is_decided_afresh_at_a_grid_point_passing_in_a_wait():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: asks to open
    for _ in range(43):  # minutes 46-88: the valve doesn't follow
        zone.valve, zone.changed_at = False, -1e6
        assert zone.step(20.8, 25.0).valve_open
    for _ in range(2):  # minutes 89-90: no room reading for a moment
        zone.valve, zone.changed_at = False, -1e6
        assert zone.step(None, 25.0).reason == REASON_ROOM_SENSOR_WAIT
    zone.valve, zone.changed_at = False, -1e6
    decision = zone.step(21.3, 25.0)  # minute 91: back, and warm by now
    assert not decision.valve_open
    assert decision.reason == REASON_PULSE_SKIPPED


def test_restart_in_a_pulse_flushes_again_then_follows_the_return_target():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: opens
    zone.step(20.8, 26.0, minutes=20)  # minute 65: past the flush, below T
    # Home Assistant restarts at minute 66: a new controller from the saved data. The
    # valve keeps its state, but reports it with a new last_changed.
    restarted = TickleZone(
        calibration=ZoneCalibration.from_dict(zone.calibration.to_dict()),
        model=OvershootModel.from_dict(zone.model.to_dict(), 0.4),
    )
    restarted.now, restarted.valve = zone.now, True
    restarted.changed_at = zone.now + 60
    for _ in range(12):  # minutes 66-77
        assert restarted.step(20.8, 29.5).reason == REASON_PULSE_FLUSH
    assert restarted.step(20.8, 29.5).reason == REASON_RETURN_REACHED  # minute 78
    for _ in range(11):  # minutes 79-89: no extra pulse
        assert not restarted.step(20.8, 25.0).valve_open
    assert restarted.step(20.8, 25.0).valve_open  # minute 90


def test_clock_step_back():
    zone = TickleZone()
    start_pulse(zone)  # minute 45: opens
    zone.step(20.8, 26.0, minutes=20)  # minute 65
    offset, duty = zone.ctrl.return_offset, zone.calibration.duty
    decision = zone.step(20.5, 26.0, minutes=-30)  # the clock steps back to minute 35
    assert decision.valve_open  # the pulse carries on
    assert zone.ctrl.return_offset == offset  # no time has passed for c ...
    assert zone.calibration.duty == duty  # ... nor for the open share
    reasons = [zone.step(20.8, 26.0 + i % 2 / 10).reason for i in range(55)]  # 36-90
    # The grid point at minute 45 has been passed already: it doesn't tick again
    assert REASON_PULSE_EXTENDED not in reasons[:-1]
    assert reasons[-1] == REASON_PULSE_EXTENDED  # minute 90


def test_return_sensor_lost_gives_timed_pulses_by_the_room():
    zone = TickleZone(calibration=ZoneCalibration(duty=0.3))
    zone.step(20.6, 25.0)  # minute 1
    assert zone.step(20.6, None).reason == REASON_RETURN_SENSOR_WAIT  # minute 2
    assert zone.step(20.6, None).reason == REASON_RETURN_SENSOR_WAIT  # minute 3
    decision = zone.step(20.6, None)  # minute 4: missing for 2 minutes
    duty = zone.calibration.duty  # the integral starts here
    assert not decision.valve_open  # opens only at a grid point
    assert decision.reason == REASON_WAITING
    assert decision.fallback == FALLBACK_RETURN_SENSOR
    assert decision.fallback_share == pytest.approx(duty + 0.5 * 0.4)
    assert decision.return_threshold is None
    assert not zone.ctrl.calibrating
    decision = zone.step(20.6, None, minutes=41)  # minute 45: grid point
    # The integral moves first, by 0.5 * 0.4 * 45 min / 8 h
    share = duty + 0.5 * 0.4 * 2700 / 28800 + 0.5 * 0.4
    assert decision.valve_open
    assert decision.reason == REASON_TIMED_PULSE
    assert decision.fallback_share == pytest.approx(share)
    end = 45 + share * 45  # minute 68.3
    zone.step(20.6, None)  # minute 46
    # The share is set for the whole interval: a warmer room changes it only at the
    # next grid point
    while zone.now / 60 < 68:
        decision = zone.step(21.5, None)
        assert decision.valve_open
        assert decision.fallback_share == pytest.approx(share)
    decision = zone.step(21.5, None)  # minute 69
    assert zone.now / 60 > end
    assert not decision.valve_open
    assert decision.reason == REASON_WAITING
    assert decision.fallback == FALLBACK_RETURN_SENSOR


def test_timed_pulses_by_the_room_stop_integrating_at_their_limit():
    zone = TickleZone(calibration=ZoneCalibration(duty=0.3))
    zone.step(18.0, 25.0)  # minute 1
    zone.step(18.0, None)  # minute 2: the return is lost
    zone.step(18.0, None)
    decision = zone.step(18.0, None)  # minute 4: timed pulses
    start = zone.calibration.duty  # where the integral starts
    assert decision.fallback_share == 1.0  # 3 °C cold: the whole interval
    for _ in range(4):  # four grid points this cold
        assert zone.step(18.0, None, minutes=45).fallback_share == 1.0
    # At the target the share is the integral alone: it didn't wind up
    decision = zone.step(21.0, None, minutes=45)
    assert decision.fallback_share == pytest.approx(start)


def test_return_that_does_not_move_counts_as_missing():
    zone = TickleZone()
    start_pulse(zone, 20.0)  # minute 45: opens; T is 31.5
    for _ in range(27):  # minutes 46-72: the sensor is stuck at 25.0
        decision = zone.step(20.0, 25.0)
        assert decision.fallback is None
    # Minute 73: flush + 15 minutes after the first reading in the pulse, which came
    # with the opening seen at minute 46
    decision = zone.step(20.0, 25.0)
    assert decision.fallback == FALLBACK_RETURN_SENSOR  # at once, no waiting
    assert decision.reason == REASON_TIMED_PULSE  # within its interval's share
    decision = zone.step(20.0, 25.1)  # minute 74: it moves, so it counts again
    assert decision.fallback is None
    assert decision.reason == REASON_PULSE
    assert decision.return_threshold == pytest.approx(31.5, abs=0.1)


def test_return_that_does_not_move_stays_missing_until_it_changes():
    zone = TickleZone()
    start_pulse(zone, 20.0)  # minute 45: opens
    for _ in range(28):  # minutes 46-73: stuck, then counted as missing
        zone.step(20.0, 25.0)
    for _ in range(3):  # minutes 74-76: unavailable
        zone.step(20.0, None)
    decision = zone.step(20.0, 25.0)  # minute 77: back with the same reading
    assert decision.fallback == FALLBACK_RETURN_SENSOR
    assert zone.step(20.0, 24.9).fallback is None  # minute 78: it moved


def test_timed_pulses_by_the_room_start_afresh_each_time():
    zone = TickleZone(calibration=ZoneCalibration(duty=0.3))
    zone.step(20.0, 25.0)  # minute 1
    zone.step(20.0, None)  # minute 2: the return is lost
    zone.step(20.0, None)
    zone.step(20.0, None)  # minute 4: timed pulses
    for _ in range(4):  # four grid points 1 °C cold: the integral grows
        zone.step(20.0, None, minutes=45)
    zone.step(20.0, 25.0)  # back for a minute
    zone.step(20.0, None)  # lost again
    zone.step(20.0, None)
    decision = zone.step(20.0, None)
    # The integral starts again at the last day's open share
    assert decision.fallback_share == pytest.approx(zone.calibration.duty + 0.5)


def test_maximum_return_comes_before_a_return_fallback():
    zone = TickleZone()
    start_pulse(zone, 20.0)  # minute 45: opens
    for _ in range(28):  # minutes 46-73: stuck, then counted as missing
        decision = zone.step(20.0, 25.0)
    assert decision.fallback == FALLBACK_RETURN_SENSOR
    decision = zone.step(20.0, 45.5)  # the reading itself is checked first
    assert not decision.valve_open
    assert decision.reason == REASON_MAX_RETURN


def test_room_sensor_lost_pulses_to_the_return_target_without_the_room():
    zone = TickleZone(calibration=ZoneCalibration(duty=0.4))  # 18 of each 45 minutes
    zone.step(20.0, 25.0)  # minute 1
    assert zone.step(None, 25.0).reason == REASON_ROOM_SENSOR_WAIT  # minute 2
    zone.step(None, 25.0)  # minute 3
    decision = zone.step(None, 25.0)  # minute 4: gone for 2 minutes
    assert decision.fallback == FALLBACK_ROOM_SENSOR
    assert decision.fallback_share == pytest.approx(0.4)  # the most it opens
    assert decision.reason == REASON_WAITING
    assert decision.return_threshold == pytest.approx(21 + 4.35 + 0.15 * 21)
    assert not zone.ctrl.calibrating
    offset = zone.ctrl.return_offset
    decision = zone.step(None, 25.0, minutes=41)  # minute 45: grid point
    assert decision.valve_open
    assert decision.reason == REASON_PULSE_FLUSH
    # The return never reaches T: the valve opens 18 minutes at most
    reasons = [zone.step(None, 26.0).reason for _ in range(18)]  # minutes 46-63
    assert reasons[:11] == [REASON_PULSE_FLUSH] * 11
    assert reasons[11:17] == [REASON_PULSE] * 6  # minutes 57-62
    assert reasons[17] == REASON_WAITING  # minute 63
    assert not zone.valve
    # The next pulse ends at T; nothing skips it
    assert zone.step(None, 26.0, minutes=27).valve_open  # minute 90
    decision = zone.step(None, 28.6, minutes=12)  # minute 102
    assert decision.reason == REASON_RETURN_REACHED
    assert zone.ctrl.return_offset == offset  # c waited for the room


def test_room_sensor_lost_lets_a_pulse_run_on_when_the_share_is_whole():
    zone = TickleZone(calibration=ZoneCalibration(duty=0.9))  # 40.5 min: all 45
    zone.step(20.0, 25.0)  # minute 1
    zone.step(None, 25.0)  # minute 2: lost
    zone.step(None, 25.0, minutes=43)  # minute 45: opens
    reasons = [zone.step(None, 26.0 + i % 2 / 10).reason for i in range(50)]
    assert reasons[11:44] == [REASON_PULSE] * 33  # minutes 57-89
    assert reasons[44:] == [REASON_PULSE_EXTENDED] * 6  # from minute 90
    assert zone.valve


@pytest.mark.parametrize(("left_s", "opens"), [(150, True), (149, False)])
def test_room_sensor_lost_opens_at_a_late_grid_point_only_with_time_left(left_s, opens):
    zone = TickleZone(calibration=ZoneCalibration(duty=0.4))  # 1080 s of each 2700
    zone.step(20.0, 25.0)  # minute 1
    zone.step(None, 25.0)  # minute 2: lost
    zone.step(None, 25.0, minutes=2)  # minute 4: pulses without the room
    zone.now = 2700 + 1080 - left_s  # the grid point at minute 45, seen late
    decision = zone.step(None, 25.0, minutes=0)
    assert decision.valve_open is opens
    assert decision.fallback == FALLBACK_ROOM_SENSOR


def test_silent_room_sensor_counts_as_lost_at_once():
    zone = TickleZone(room_silent_s=6 * 3600)
    zone.step(20.0, 25.0, room_updated_at=0.0)
    decision = zone.step(20.0, 25.0, minutes=6 * 60, room_updated_at=0.0)
    assert zone.ctrl.room_silent
    assert decision.fallback == FALLBACK_ROOM_SENSOR  # lost since its last report


def test_both_sensors_lost_hold_the_last_days_share():
    zone = TickleZone(calibration=ZoneCalibration(duty=0.4))
    zone.step(20.0, 25.0)  # minute 1
    assert zone.step(None, None).reason == REASON_ROOM_SENSOR_WAIT  # minute 2
    decision = zone.step(None, None, minutes=2)  # minute 4
    assert decision.fallback == FALLBACK_SENSORS
    assert decision.fallback_share == pytest.approx(0.4)
    assert decision.reason == REASON_WAITING
    decision = zone.step(None, None, minutes=41)  # minute 45: grid point
    assert decision.valve_open
    assert decision.reason == REASON_TIMED_PULSE
    assert zone.step(None, None, minutes=17).valve_open  # minute 62
    assert not zone.step(None, None).valve_open  # minute 63: 18 minutes


def test_room_back_in_timed_pulses_waits_for_the_grid_point():
    # Both sensors lost: 20 % of each 45 minutes, so the valve opens at minute 45
    # and closes at minute 54
    zone = TickleZone(calibration=ZoneCalibration(duty=0.2))
    zone.step(20.0, 25.0)  # minute 1
    zone.step(None, None)  # minute 2: both lost
    zone.step(None, None, minutes=2)  # minute 4: timed pulses
    assert zone.step(None, None, minutes=41).valve_open  # minute 45
    assert not zone.step(None, None, minutes=9).valve_open  # minute 54
    # The room is back, 2 °C cold: by the room the whole interval is due. A valve
    # that has closed opens again only at the next grid point.
    for _ in range(35):  # minutes 55-89
        decision = zone.step(19.0, None)
        assert not decision.valve_open
        assert decision.reason == REASON_WAITING
        assert decision.fallback == FALLBACK_RETURN_SENSOR
    assert decision.fallback_share == 1.0
    decision = zone.step(19.0, None)  # minute 90
    assert decision.valve_open
    assert decision.reason == REASON_TIMED_PULSE


def test_room_going_silent_in_timed_pulses_waits_for_the_grid_point():
    zone = TickleZone(calibration=ZoneCalibration(duty=0.6), room_silent_s=1800)
    zone.step(21.6, 25.0)  # minute 1
    zone.step(21.6, None)  # minute 2: the return is lost
    zone.step(21.6, None, minutes=2)  # minute 4: timed pulses by the room
    # 0.6 °C warm: open for 0.6 - 0.5 * 0.6 = 0.3 of each interval, less as the
    # integral moves at the grid point
    assert zone.step(21.6, None, minutes=41).valve_open  # minute 45
    assert not zone.step(21.6, None, minutes=13).valve_open  # minute 58
    # The room sensor is found silent, its last report at minute 0: the last day's
    # share is longer, but the valve has closed for this interval
    for _ in range(31):  # minutes 59-89
        decision = zone.step(21.6, None, room_updated_at=0.0)
        assert not decision.valve_open
        assert decision.fallback == FALLBACK_SENSORS
    assert decision.fallback_share == pytest.approx(0.6, abs=0.03)
    assert zone.step(21.6, None, room_updated_at=0.0).valve_open  # minute 90


def test_room_sensor_lost_after_a_skipped_pulse_waits_for_the_grid_point():
    zone = TickleZone()
    zone.step(21.4, 25.0)  # minute 1
    decision = zone.step(21.4, 25.0, minutes=44)  # minute 45: warm
    assert decision.reason == REASON_PULSE_SKIPPED
    zone.step(None, 25.0)  # minutes 46-47: the room sensor is lost
    zone.step(None, 25.0)
    for _ in range(42):  # minutes 48-89: nothing skips a pulse without the room
        decision = zone.step(None, 25.0)
        assert not decision.valve_open
        assert decision.reason == REASON_WAITING
        assert decision.fallback == FALLBACK_ROOM_SENSOR
    assert zone.step(None, 25.0).valve_open  # minute 90


@pytest.mark.parametrize(
    ("held", "reason"),
    [
        # Minutes 58-63: the return is lost; after 2 minutes, timed pulses by the
        # room keep the valve open for their share
        ([(20.5, None)] * 6, REASON_TIMED_PULSE),
        ([(6.5, 40.0)], REASON_FROST),  # minute 58: freezing
    ],
)
def test_valve_held_open_after_its_pulse_closes_for_the_next_grid_point(held, reason):
    zone = TickleZone(calibration=ZoneCalibration(duty=0.5))
    start_pulse(zone, 20.5)  # minute 45: opens
    decision = zone.step(20.5, 40.0, minutes=12)  # minute 57: closes at T
    assert decision.reason == REASON_RETURN_REACHED
    zone.valve, zone.changed_at = True, 2700.0  # ... but the valve doesn't follow
    for room, ret in held:
        decision = zone.step(room, ret)
        assert decision.valve_open
    assert decision.reason == reason
    decision = zone.step(20.5, 27.0)  # the return target is far off now
    assert not decision.valve_open
    assert decision.reason == REASON_WAITING
    assert decision.return_threshold > 27.0


@pytest.mark.parametrize(
    ("extra", "ret", "reason"),
    [
        ({}, 46.0, REASON_MAX_RETURN),
        ({"window_open": True}, 26.0, REASON_WINDOW),
        ({"heating_enabled": False}, 26.0, REASON_OFF),
    ],
)
def test_safety_rules_override_the_flush(extra, ret, reason):
    zone = TickleZone()
    start_pulse(zone)  # minute 45
    decision = zone.step(20.8, ret, minutes=2, **extra)  # minute 47: in the flush
    assert not decision.valve_open
    assert decision.reason == reason
    assert decision.return_threshold is None
    assert decision.next_pulse == 5400
    decision = zone.step(20.8, 26.0)  # minute 48: the rule is over
    assert not decision.valve_open
    assert decision.reason == REASON_WAITING


def test_grid_points_during_a_safety_rule_are_dropped():
    zone = TickleZone()
    zone.step(20.0, 25.0)
    zone.step(20.0, 25.0, minutes=40, window_open=True)  # minute 41
    decision = zone.step(20.0, 25.0, minutes=4, window_open=True)  # minute 45
    assert decision.reason == REASON_WINDOW
    for _ in range(44):  # minutes 46-89
        assert zone.step(20.0, 25.0).reason == REASON_WAITING
    assert zone.step(20.0, 25.0).valve_open  # minute 90


def test_frost_guard_opens_at_once_and_its_opening_carries_on_as_a_pulse():
    zone = TickleZone()
    zone.step(21.5, 25.0)
    decision = zone.step(6.5, 25.0, minutes=9, heating_enabled=False)  # minute 10
    assert decision.valve_open
    assert decision.reason == REASON_FROST
    decision = zone.step(7.4, 25.0, heating_enabled=False)  # minute 11: warm enough
    assert not decision.valve_open
    assert decision.reason == REASON_OFF
    decision = zone.step(6.5, 25.0, minutes=4)  # minute 15: on, but freezing
    assert decision.reason == REASON_FROST
    # Out of frost, the opening counts as a pulse that started at minute 15
    reasons = [zone.step(8.0, 30.0).reason for _ in range(12)]  # minutes 16-27
    assert reasons[:11] == [REASON_PULSE_FLUSH] * 11
    assert reasons[11] == REASON_PULSE  # T is capped at 43


def test_pulses_teach_the_overshoot_model_nothing():
    zone = TickleZone()
    start_pulse(zone, 20.5, 24.0)  # minute 45: opens
    # A low point and a rise that would teach the thermostat h, then a close and a
    # peak that would teach it the overshoot
    for room, ret in [(20.4, 25.0), (20.4, 26.0), (20.5, 27.0), (20.7, 28.0)]:
        zone.step(room, ret, minutes=3)
    zone.step(20.8, 30.0, minutes=3)  # minute 60: closes at T
    for room in (21.0, 21.2, 21.1, 21.0):
        zone.step(room, 26.0, minutes=20)
    assert zone.model.revision == 0
    assert not zone.ctrl.coasting


def test_aim_offset_is_not_applied():
    aimed, plain = TickleZone(aim_offset=0.15), TickleZone()
    for minute in range(240):
        room = 20.6 + 0.4 * (minute % 90) / 90
        ret = 24.0 + 6.0 * ((minute % 45) / 45)
        assert aimed.step(room, ret) == plain.step(room, ret)


def test_reset_learning_starts_c_afresh_from_h_and_keeps_the_rest():
    model = OvershootModel(
        initial_overshoot=0.4, holding_delta=6.0, cycles=5, beta=1.5, beta_samples=4
    )
    calibration = ZoneCalibration(duty=0.33, return_offset=8.0, offset_slope=0.15)
    zone = TickleZone(model=model, calibration=calibration)
    zone.step(21.0, 25.0, outside=-4.0, calibrate=False)
    learned, revision = model.to_dict(), calibration.revision
    zone.ctrl.reset_learning()
    assert zone.ctrl.return_offset == pytest.approx(6.0 - 0.15 * 25)
    assert model.to_dict() == learned
    assert calibration.duty == 0.33
    assert calibration.revision > revision
    decision = zone.step(21.0, 25.0, outside=-4.0, calibrate=False)
    assert decision.return_threshold == pytest.approx(21 + 6.0)  # T = target + h
