"""Tests for the valve controller rules."""

from dataclasses import replace
import math

import pytest

from smartheat_core.controller import (
    FALLBACK_ROOM_SENSOR,
    REASON_COASTING,
    REASON_FROST,
    REASON_HEATING,
    REASON_IDLE,
    REASON_MAX_RETURN,
    REASON_MIN_CLOSED,
    REASON_MIN_OPEN,
    REASON_OFF,
    REASON_PREDICTIVE_CLOSE,
    REASON_ROOM_SENSOR_LOST,
    REASON_ROOM_SENSOR_WAIT,
    REASON_WINDOW,
    Controller,
    ControllerConfig,
    Grid,
    Inputs,
    hold_on_time,
    zone_phase,
)
from smartheat_core.learning import OvershootModel, ZoneCalibration


class Zone:
    """Drives a controller the way Home Assistant does, with a valve that obeys at once.

    Defaults: target 21 °C, hysteresis 0.3, min open/closed 15 min, coast window 4 h.
    """

    def __init__(
        self,
        initial_overshoot: float = 0.4,
        calibration: ZoneCalibration | None = None,
        **config,
    ) -> None:
        self.model = OvershootModel(initial_overshoot=initial_overshoot)
        self.ctrl = Controller(ControllerConfig(**config), self.model, calibration)
        self.now = 0.0
        self.valve = False
        self.changed_at = -1e6  # closed long ago

    def step(self, room, ret=None, minutes=1.0, target=21.0, **extra):
        self.now += minutes * 60
        decision = self.ctrl.update(
            Inputs(
                room=room,
                return_temp=ret,
                target=target,
                valve_open=self.valve,
                valve_changed_at=self.changed_at,
                **extra,
            ),
            self.now,
        )
        if decision.valve_open != self.valve:
            self.valve = decision.valve_open
            self.changed_at = self.now
        return decision

    def open_then_close(self, ret=28.0):
        """Open at 20.0 °C, then close predictively 20 minutes later at 20.2 °C."""
        self.step(20.0)
        decision = self.step(20.2, ret=ret, minutes=20)
        assert decision.reason == REASON_PREDICTIVE_CLOSE


def test_opens_below_target_minus_hysteresis():
    zone = Zone()
    assert zone.step(20.8).reason == REASON_IDLE
    decision = zone.step(20.6)
    assert decision.valve_open
    assert decision.reason == REASON_HEATING


def test_waits_min_closed_time_before_reopening():
    zone = Zone()
    zone.changed_at = 0.0  # just closed
    decision = zone.step(20.0, minutes=5)
    assert not decision.valve_open
    assert decision.reason == REASON_MIN_CLOSED
    assert zone.step(20.0, minutes=10).valve_open


def test_closes_early_by_predicted_overshoot():
    zone = Zone()  # flat overshoot 0.4: close at 20.6
    zone.step(20.0)
    decision = zone.step(20.5, ret=30.0, minutes=20)
    assert decision.valve_open
    assert decision.close_threshold == pytest.approx(20.6)
    decision = zone.step(20.65, ret=30.0)
    assert not decision.valve_open
    assert decision.reason == REASON_PREDICTIVE_CLOSE


def test_min_open_time_keeps_valve_open():
    zone = Zone()
    zone.step(20.0)
    decision = zone.step(20.9, ret=30.0, minutes=5)
    assert decision.valve_open
    assert decision.reason == REASON_MIN_OPEN


def test_coasting_blocks_reopen_until_room_peaks_then_learns():
    zone = Zone(initial_overshoot=0.8)  # closes at 20.2, below the 20.7 open threshold
    zone.open_then_close()

    decision = zone.step(20.3, ret=26.0)
    assert zone.ctrl.coasting
    assert not decision.valve_open
    assert decision.reason == REASON_COASTING

    zone.step(20.6, minutes=30)
    zone.step(20.9, minutes=30)
    assert zone.ctrl.coasting

    decision = zone.step(20.8, minutes=30)  # fell 0.1 below the peak
    assert not zone.ctrl.coasting
    assert decision.reason == REASON_IDLE
    assert zone.model.cycles == 1
    assert zone.model.last_overshoot == pytest.approx(0.7)
    assert zone.model.last_peak_error == pytest.approx(-0.1)
    assert zone.model.flat_overshoot == pytest.approx(0.8 + 0.2 * (0.7 - 0.8))

    assert zone.step(20.6).valve_open


def test_holding_delta_learned_at_room_low_point():
    zone = Zone()
    zone.step(20.6, ret=22.0)  # opens
    zone.step(20.5, ret=23.0)  # still cooling: low point starts, x = 2.5
    zone.step(20.5, ret=25.0)
    zone.step(20.5, ret=26.0)  # low point ends, x = 5.5
    assert zone.model.holding_delta is None
    zone.step(20.6, ret=27.0)  # rising again
    assert zone.model.holding_delta == pytest.approx(4.0)

    zone.step(20.4, ret=28.0)  # one sample per heating period
    zone.step(20.6, ret=28.0)
    assert zone.model.holding_delta == pytest.approx(4.0)


def test_holding_delta_not_learned_without_hot_water():
    zone = Zone()
    zone.step(20.6, ret=22.0, supply=21.0)
    zone.step(20.5, ret=21.0, supply=21.0, minutes=6)
    assert zone.ctrl.no_heat_source
    zone.step(20.6, ret=21.0, supply=21.0)
    assert zone.model.holding_delta is None


def test_early_dip_does_not_end_coast_before_min_coast_time():
    zone = Zone(initial_overshoot=0.8)
    zone.open_then_close()
    zone.step(20.3)
    zone.step(20.2, minutes=5)  # sensor noise right after closing
    assert zone.ctrl.coasting
    assert zone.model.cycles == 0


def test_coast_window_ends_coasting():
    zone = Zone(initial_overshoot=0.8, coast_window_s=3600)
    zone.open_then_close()
    zone.step(20.3)
    decision = zone.step(20.4, minutes=59)  # still rising when the window ends
    assert not zone.ctrl.coasting
    assert zone.model.cycles == 1
    assert zone.model.last_overshoot == pytest.approx(0.2)
    assert decision.valve_open


def test_target_change_drops_the_cycle():
    zone = Zone(initial_overshoot=0.8)
    zone.open_then_close()
    zone.step(20.3)
    assert zone.ctrl.coasting

    decision = zone.step(20.3, target=22.0)
    assert not zone.ctrl.coasting
    assert decision.reason == REASON_MIN_CLOSED
    assert zone.step(20.3, target=22.0, minutes=15).valve_open
    assert zone.model.cycles == 0


def test_window_during_coast_drops_the_cycle():
    zone = Zone(initial_overshoot=0.8)
    zone.open_then_close()
    zone.step(20.3)
    decision = zone.step(20.4, window_open=True)
    assert decision.reason == REASON_WINDOW
    assert not zone.ctrl.coasting
    assert zone.model.cycles == 0


def test_reopening_during_coast_drops_the_cycle():
    zone = Zone(initial_overshoot=0.8)
    zone.open_then_close()
    zone.step(20.3)
    zone.valve, zone.changed_at = True, zone.now  # opened by hand
    zone.step(20.4)
    assert not zone.ctrl.coasting
    assert zone.model.cycles == 0


def test_max_return_closes_immediately_without_coasting():
    zone = Zone()
    zone.step(20.0)
    decision = zone.step(20.1, ret=46.0)
    assert not decision.valve_open
    assert decision.reason == REASON_MAX_RETURN

    decision = zone.step(20.1, ret=40.0)
    assert not zone.ctrl.coasting
    assert decision.reason == REASON_MIN_CLOSED


def test_window_and_off_close_the_valve():
    zone = Zone()
    zone.step(20.0)
    decision = zone.step(20.0, window_open=True)
    assert not decision.valve_open
    assert decision.reason == REASON_WINDOW

    zone = Zone()
    zone.step(20.0)
    decision = zone.step(20.0, heating_enabled=False)
    assert not decision.valve_open
    assert decision.reason == REASON_OFF


def test_manual_close_does_not_start_coasting():
    zone = Zone()
    zone.step(20.0)
    zone.step(20.0)  # the valve is seen open
    zone.valve, zone.changed_at = False, zone.now  # closed by hand
    decision = zone.step(20.0)
    assert not zone.ctrl.coasting
    assert decision.reason == REASON_MIN_CLOSED


def test_room_sensor_loss_holds_then_closes():
    zone = Zone()
    zone.step(20.0)
    decision = zone.step(None, ret=30.0, minutes=10)
    assert decision.valve_open
    assert decision.reason == REASON_ROOM_SENSOR_WAIT

    decision = zone.step(None, ret=30.0, minutes=30)
    assert not decision.valve_open
    assert decision.reason == REASON_ROOM_SENSOR_LOST


def test_frost_guard_uses_return_when_room_sensor_is_lost():
    zone = Zone()
    zone.step(None, ret=15.0)
    zone.step(None, ret=15.0, minutes=30)  # lost for 30 minutes: closed
    decision = zone.step(None, ret=6.0, minutes=60)
    assert decision.valve_open
    assert decision.reason == REASON_FROST

    decision = zone.step(None, ret=20.0, minutes=10)  # warm return, but min open time
    assert decision.valve_open
    decision = zone.step(None, ret=20.0, minutes=5)
    assert not decision.valve_open
    assert decision.reason == REASON_ROOM_SENSOR_LOST


def test_lost_room_sensor_without_return_sensor_closes_after_timeout():
    zone = Zone()
    zone.step(20.0)
    assert zone.step(None, minutes=10).reason == REASON_ROOM_SENSOR_WAIT
    assert zone.step(None, minutes=30).reason == REASON_ROOM_SENSOR_LOST
    # nothing can stand in for the room, so no frost pulses either
    decision = zone.step(None, minutes=600)
    assert not decision.valve_open
    assert decision.reason == REASON_ROOM_SENSOR_LOST


def test_frost_guard_heats_even_when_off():
    zone = Zone()
    decision = zone.step(6.5, heating_enabled=False)
    assert decision.valve_open
    assert decision.reason == REASON_FROST
    assert zone.step(7.1, heating_enabled=False).valve_open  # below frost + hysteresis
    decision = zone.step(7.4, heating_enabled=False, minutes=15)
    assert not decision.valve_open
    assert decision.reason == REASON_OFF


def test_learned_model_used_when_return_known_else_flat():
    zone = Zone()
    zone.model.beta, zone.model.beta_samples, zone.model.holding_delta = 0.5, 3, 4.0
    zone.step(20.0)
    decision = zone.step(20.0, ret=28.0, minutes=20)
    assert decision.predicted_overshoot == pytest.approx(
        0.5 * (4.0 - 4.0 * math.log(2))
    )
    assert zone.step(20.0, ret=None).predicted_overshoot == pytest.approx(0.4)


def test_no_hot_water_is_flagged_and_not_learned():
    zone = Zone(initial_overshoot=0.9)  # close at 20.1
    zone.step(20.0, supply=22.0)
    zone.step(20.0, ret=21.0, supply=22.0, minutes=10)
    assert zone.ctrl.no_heat_source

    decision = zone.step(20.2, ret=21.0, supply=22.0, minutes=10)
    assert decision.reason == REASON_PREDICTIVE_CLOSE
    zone.step(20.3)
    assert zone.ctrl.coasting
    assert not zone.ctrl.no_heat_source  # only judged while the valve is open

    zone.step(20.4, minutes=60)
    zone.step(20.3, minutes=10)
    assert not zone.ctrl.coasting
    assert zone.model.cycles == 0
    assert zone.model.heat_up_rate is None


def test_saturated_loop_is_flagged():
    zone = Zone()
    zone.step(20.0)
    zone.step(20.0, ret=34.0, supply=35.0, minutes=10)
    assert not zone.ctrl.loop_saturated
    zone.step(20.0, ret=34.0, supply=35.0, minutes=30)
    assert zone.ctrl.loop_saturated
    assert zone.ctrl.supply_return_delta == pytest.approx(1.0)
    zone.step(20.0, ret=30.0, supply=35.0)
    assert not zone.ctrl.loop_saturated


def test_supply_sensor_without_return_sensor():
    zone = Zone()
    zone.step(20.0, supply=50.0)
    zone.step(20.0, supply=50.0, minutes=60)
    assert zone.ctrl.supply_return_delta is None
    assert not zone.ctrl.loop_saturated
    assert not zone.ctrl.no_heat_source
    zone.step(20.0, supply=21.0, minutes=10)
    assert zone.ctrl.no_heat_source


def test_zone_without_return_sensor_learns_only_the_average():
    zone = Zone(initial_overshoot=0.8)
    zone.open_then_close(ret=None)
    zone.step(20.6, minutes=30)
    zone.step(20.9, minutes=30)
    zone.step(20.8, minutes=30)  # peaked
    assert zone.model.cycles == 1
    assert zone.model.flat_overshoot == pytest.approx(0.8 + 0.2 * (0.7 - 0.8))
    assert zone.model.beta is None
    assert zone.model.holding_delta is None


def test_heat_up_rate_learned_from_heating_period():
    zone = Zone()
    zone.step(20.0)  # opens at minute 1
    zone.step(20.1, ret=30.0, minutes=59)
    decision = zone.step(20.6, ret=30.0, minutes=60)  # closes at minute 120
    assert decision.reason == REASON_PREDICTIVE_CLOSE
    zone.step(20.6, ret=29.0)
    assert zone.model.heat_up_rate == pytest.approx(0.6 / (119 / 60))


def test_duty_cycle_over_last_day():
    zone = Zone(min_open_s=0, min_closed_s=0)
    zone.step(20.0)  # opens at 60 s
    zone.step(21.5, minutes=59)  # closes at 3600 s
    zone.step(21.5)
    assert zone.ctrl.duty_cycle(zone.now) == pytest.approx(100 * 3540 / 86400)
    assert zone.ctrl.duty_cycle(zone.now + 86400) == 0.0


def test_reset_learning_forgets_and_stops_coasting():
    zone = Zone(initial_overshoot=0.8)
    zone.open_then_close()
    zone.step(20.3)
    zone.model.add_cycle(0.5, room_close=20.0, return_close=25.0)
    zone.ctrl.reset_learning()
    assert not zone.ctrl.coasting
    assert zone.model.cycles == 0
    assert zone.model.flat_overshoot == 0.8


@pytest.mark.parametrize("offset", [0.0, 0.15, -0.2])
def test_aim_offset_moves_the_open_and_close_points(offset):
    zone = Zone(aim_offset=offset)  # flat overshoot 0.4
    aim = 21.0 + offset
    assert zone.step(aim - 0.25).reason == REASON_IDLE  # within the hysteresis
    decision = zone.step(aim - 0.35)
    assert decision.reason == REASON_HEATING
    assert decision.close_threshold == pytest.approx(aim - 0.4)
    assert zone.step(aim - 0.45, ret=30.0, minutes=20).valve_open
    assert zone.step(aim - 0.35, ret=30.0).reason == REASON_PREDICTIVE_CLOSE


def test_aim_offset_moves_the_coast_target():
    zone = Zone(initial_overshoot=0.8, aim_offset=0.15)  # closes at 20.35
    zone.step(20.0)
    assert zone.step(20.4, ret=28.0, minutes=20).reason == REASON_PREDICTIVE_CLOSE
    zone.step(20.5, ret=26.0)
    assert zone.ctrl.coasting
    assert zone.ctrl.coast.target == pytest.approx(21.15)

    zone.step(20.8, minutes=30)
    zone.step(21.1, minutes=30)
    decision = zone.step(21.0, minutes=30)  # fell 0.1 below the peak
    assert not zone.ctrl.coasting
    assert decision.reason == REASON_IDLE  # above 21.15 - 0.3
    assert zone.model.last_overshoot == pytest.approx(0.7)
    assert zone.model.last_peak_error == pytest.approx(-0.05)  # the peak vs 21.15


@pytest.mark.parametrize("offset", [0.0, 0.15, -0.2])
def test_aim_offset_acts_like_a_target_that_much_higher(offset):
    """An offset of 0 changes nothing: the same decisions, and the same learning."""
    aimed, raised = Zone(aim_offset=offset), Zone()
    floor = room = 20.0
    for minute in range(24 * 60):  # a crude floor, heated while the valve is open
        target = 21.0 if 6 * 60 <= minute < 22 * 60 else 19.0
        if aimed.valve:
            floor += 0.02 * (35.0 - floor)
        floor -= 0.01 * (floor - room)
        room += 0.004 * (floor - room) - 0.001 * (room - 5.0)
        decision = aimed.step(round(room, 1), ret=round(floor, 1), target=target)
        assert decision == raised.step(
            round(room, 1), ret=round(floor, 1), target=target + offset
        )
    assert aimed.model.cycles >= 5
    assert aimed.model == raised.model


def test_same_state_report_does_not_restart_min_open_time():
    zone = Zone()
    zone.step(20.0)  # opens at minute 1
    zone.step(20.2, ret=30.0)  # seen open
    zone.changed_at = zone.now + 8 * 60  # minute 10: back from unavailable, still on
    assert zone.step(20.9, ret=30.0, minutes=8).reason == REASON_MIN_OPEN
    decision = zone.step(20.9, ret=30.0, minutes=6)  # 15 min after it really opened
    assert decision.reason == REASON_PREDICTIVE_CLOSE


def test_valve_change_time_is_filled_in_once_known():
    ctrl = Controller(ControllerConfig(), OvershootModel(initial_overshoot=0.4))
    inputs = Inputs(room=21.0, return_temp=None, target=21.0, valve_open=False)
    ctrl.update(inputs, 60.0)
    assert ctrl.valve_changed_at is None
    ctrl.update(replace(inputs, valve_changed_at=30.0), 120.0)
    assert ctrl.valve_changed_at == 30.0
    ctrl.update(replace(inputs, valve_changed_at=100.0), 180.0)
    assert ctrl.valve_changed_at == 30.0


def test_open_share_is_averaged_in_normal_control_only():
    calibration = ZoneCalibration()
    zone = Zone(calibration=calibration)
    zone.step(21.0)  # the first update: no time has passed yet
    assert calibration.duty == 0.4

    zone.step(21.0, minutes=60)  # closed; a gap counts as 5 minutes at most
    closed = 0.4 * math.exp(-300 / 86400)
    assert calibration.duty == pytest.approx(closed)
    zone.step(21.0, window_open=True)
    zone.step(21.0, heating_enabled=False)
    zone.step(None)
    assert calibration.duty == pytest.approx(closed)

    zone.step(20.0)  # closed for another minute, then opens
    zone.step(20.0)  # open for a minute
    closed *= math.exp(-60 / 86400)
    assert calibration.duty == pytest.approx(
        closed + (1 - closed) * (1 - math.exp(-60 / 86400))
    )


def test_open_share_counts_only_a_known_valve_state():
    calibration = ZoneCalibration()
    ctrl = Controller(ControllerConfig(), OvershootModel(0.4), calibration)
    inputs = Inputs(room=20.0, return_temp=None, target=21.0, valve_open=True)
    ctrl.update(inputs, 60.0)
    ctrl.update(replace(inputs, valve_open=None), 120.0)  # unavailable
    assert calibration.duty == 0.4
    ctrl.update(inputs, 180.0)
    assert calibration.duty == pytest.approx(0.4 + 0.6 * (1 - math.exp(-60 / 86400)))


def test_open_share_ignores_a_clock_step_back():
    calibration = ZoneCalibration()
    zone = Zone(calibration=calibration)
    zone.step(21.0)
    zone.step(21.0, minutes=-30)
    assert calibration.duty == 0.4


@pytest.mark.parametrize(("silent_s", "left_out"), [(6 * 3600, True), (None, False)])
def test_open_share_leaves_out_a_reading_not_reported_for_an_hour(silent_s, left_out):
    calibration = ZoneCalibration()
    zone = Zone(calibration=calibration, room_silent_s=silent_s)
    zone.step(21.0, room_updated_at=0.0)
    zone.step(21.0, minutes=59, room_updated_at=0.0)  # reported an hour ago
    duty = 0.4 * math.exp(-300 / 86400)
    assert calibration.duty == pytest.approx(duty)
    # The sensor may have stopped with this value. Only checked with a silent time.
    zone.step(21.0, minutes=2, room_updated_at=0.0)
    if not left_out:
        duty *= math.exp(-120 / 86400)
    assert calibration.duty == pytest.approx(duty)


def hold_zone(**config) -> Zone:
    """An underfloor zone: holds a 40 % share on a 45 min grid from minute 0."""
    return Zone(lost_room_hold=True, calibration=ZoneCalibration(duty=0.4), **config)


def test_underfloor_hold_opens_for_the_days_share_at_each_grid_point():
    zone = hold_zone()
    zone.step(21.0, minutes=62)
    for _ in range(30):  # minute 63-92: lost, waiting for the sensor
        assert zone.step(None).reason == REASON_ROOM_SENSOR_WAIT
    decision = zone.step(None)
    # Lost for 30 min: the hold starts 3 min after a grid point, too late to open
    assert not decision.valve_open
    assert decision.reason == REASON_ROOM_SENSOR_LOST
    assert decision.fallback == FALLBACK_ROOM_SENSOR
    assert decision.fallback_share == pytest.approx(0.4)

    opened, closed = [], []
    for _ in range(108):
        was_open = zone.valve
        decision = zone.step(None)
        assert decision.reason == REASON_ROOM_SENSOR_LOST
        if zone.valve != was_open:
            (opened if zone.valve else closed).append(zone.now / 60)
    # 40 % of 45 min: open 18 min from each grid point
    assert opened == [135, 180]
    assert closed == [153, 198]


def test_hold_lets_an_open_valve_finish_the_intervals_share():
    zone = Zone(lost_room_hold=True)  # without a calibration the share is 40 %
    assert zone.step(20.0, minutes=72).valve_open  # minute 72: heating
    zone.step(None)  # minute 73: lost
    decision = zone.step(None, minutes=30)  # minute 103: 13 min after a grid point
    assert decision.valve_open
    assert decision.reason == REASON_ROOM_SENSOR_LOST
    assert decision.fallback_share == pytest.approx(0.4)
    assert zone.step(None, minutes=4).valve_open
    assert not zone.step(None).valve_open  # minute 108: 90 + 18
    assert not zone.step(None, minutes=26).valve_open
    assert zone.step(None).valve_open  # minute 135: the next grid point


def test_hold_share_is_frozen_until_the_room_is_back():
    calibration = ZoneCalibration(duty=0.25)
    zone = Zone(lost_room_hold=True, calibration=calibration)
    zone.step(21.0)
    zone.step(None)
    assert zone.step(None, minutes=30).fallback_share == 0.25
    calibration.duty = 0.8
    assert zone.step(None).fallback_share == 0.25

    decision = zone.step(21.0)  # back: normal control
    assert decision.reason == REASON_IDLE
    assert decision.fallback is None
    zone.step(None)
    assert zone.step(None, minutes=30).fallback_share == pytest.approx(0.8, abs=0.01)


def test_radiator_zone_still_closes_without_room_sensor():
    zone = Zone(min_open_s=300, min_closed_s=300, coast_window_s=3600, max_return=75.0)
    zone.step(20.0)  # opens
    zone.step(None, ret=40.0)
    decision = zone.step(None, ret=40.0, minutes=30)
    assert not decision.valve_open
    assert decision.reason == REASON_ROOM_SENSOR_LOST
    assert decision.fallback is None
    for _ in range(90):  # no grid point opens it
        assert not zone.step(None, ret=40.0).valve_open


def test_hold_gives_way_to_max_return_and_frost_guard():
    zone = hold_zone()
    zone.step(21.0, ret=25.0)
    zone.step(None, ret=25.0)  # minute 2: lost
    assert zone.step(None, ret=25.0, minutes=43).valve_open  # minute 45: grid point
    decision = zone.step(None, ret=46.0)
    assert not decision.valve_open
    assert decision.reason == REASON_MAX_RETURN
    assert decision.fallback is None
    # it opens again at the next grid point, not before
    assert not zone.step(None, ret=30.0).valve_open
    decision = zone.step(None, ret=6.0, minutes=10)  # a closed loop this cold: frost
    assert decision.valve_open
    assert decision.reason == REASON_FROST


def test_silent_room_sensor_counts_as_lost_until_it_reports():
    zone = hold_zone(room_silent_s=6 * 3600)
    zone.step(21.0, minutes=10, room_updated_at=600.0)  # minute 10: a report
    decision = zone.step(21.0, minutes=359, room_updated_at=600.0)  # 5 h 59 min later
    assert not zone.ctrl.room_silent
    assert decision.reason == REASON_IDLE
    # 6 h: the value may be stale. Lost since its last report, the sensor is long
    # past the room sensor timeout, so the hold starts at once.
    decision = zone.step(21.0, room_updated_at=600.0)
    assert zone.ctrl.room_silent
    assert decision.reason == REASON_ROOM_SENSOR_LOST
    assert decision.fallback == FALLBACK_ROOM_SENSOR
    assert not decision.valve_open  # minute 370: it opens at the next grid point

    decision = zone.step(21.0, room_updated_at=zone.now + 60)  # a report, same value
    assert not zone.ctrl.room_silent
    assert decision.reason == REASON_IDLE
    assert decision.fallback is None


def test_silent_room_sensor_still_waits_out_a_longer_timeout():
    zone = hold_zone(room_silent_s=3600, sensor_timeout_s=4 * 3600)
    zone.step(21.0, room_updated_at=0.0)
    decision = zone.step(21.0, minutes=59, room_updated_at=0.0)  # silent for an hour
    assert zone.ctrl.room_silent
    assert decision.reason == REASON_ROOM_SENSOR_WAIT
    decision = zone.step(21.0, minutes=179, room_updated_at=0.0)  # 3 h 59 min
    assert decision.reason == REASON_ROOM_SENSOR_WAIT
    decision = zone.step(21.0, room_updated_at=0.0)  # 4 h after the last report
    assert decision.reason == REASON_ROOM_SENSOR_LOST


@pytest.mark.parametrize(
    ("silent_s", "updated_at"),
    [
        (None, 0.0),
        (0, 0.0),
        (3600, None),  # a sensor whose reports aren't watched, such as a helper
    ],
)
def test_room_silence_check_can_be_off(silent_s, updated_at):
    zone = Zone(room_silent_s=silent_s)
    decision = zone.step(21.0, minutes=24 * 60, room_updated_at=updated_at)
    assert not zone.ctrl.room_silent
    assert decision.reason == REASON_IDLE


def test_openings_of_the_hold_teach_nothing():
    zone = hold_zone()
    zone.step(21.0)
    zone.step(None, ret=22.0)  # minute 2: lost
    assert zone.step(None, ret=22.0, minutes=43).valve_open  # minute 45: grid point
    # The room is back at once and has a low point while the hold's opening runs
    zone.step(20.0, ret=23.0)
    zone.step(19.9, ret=24.0)
    zone.step(19.9, ret=25.0)
    zone.step(20.1, ret=26.0)
    assert zone.model.holding_delta is None
    decision = zone.step(20.7, ret=30.0, minutes=20)
    assert decision.reason == REASON_PREDICTIVE_CLOSE
    zone.step(20.8, ret=29.0)
    assert zone.model.revision == 0  # nothing learned, not even the heat-up rate
    # The coast still keeps the valve shut until the room peaks, but won't learn
    assert zone.ctrl.coasting
    assert not zone.ctrl.coast.valid
    assert zone.step(20.6, ret=27.0, minutes=15).reason == REASON_COASTING

    # The peak has passed: nothing learned. The next opening starts with the room
    # known, and teaches as usual.
    assert zone.step(20.5, ret=25.0, minutes=15).valve_open
    assert not zone.ctrl.coasting
    assert zone.model.revision == 0
    zone.step(20.4, ret=26.0)
    zone.step(20.5, ret=27.0)
    assert zone.model.holding_delta == pytest.approx(5.6)


def test_hold_opening_reported_late_teaches_nothing():
    zone = hold_zone()
    zone.step(21.0)
    zone.step(None, ret=22.0)  # minute 2: lost
    assert zone.step(None, ret=22.0, minutes=43).valve_open  # minute 45: grid point
    zone.valve, zone.changed_at = False, -1e6  # a slow valve: not open yet
    # The room is back before the valve reports the opening, and asks for no heat
    assert zone.step(20.8, ret=22.0).reason == REASON_IDLE
    zone.valve, zone.changed_at = True, zone.now + 60  # minute 47: open at last
    zone.step(20.7, ret=24.0)
    zone.step(20.7, ret=25.0)
    zone.step(20.8, ret=26.0)  # a low point, which would teach h
    decision = zone.step(20.9, ret=28.0, minutes=13)  # minute 62
    assert decision.reason == REASON_PREDICTIVE_CLOSE
    zone.step(20.9, ret=27.0)
    assert zone.ctrl.coasting
    assert zone.model.revision == 0


def test_valve_seen_open_at_start_without_room_reading_teaches():
    # At a start, the room sensor may load after the valve. The valve was not opened
    # without a room reading, so this opening teaches as usual.
    zone = Zone(initial_overshoot=0.8)  # closes at 20.2
    zone.valve, zone.changed_at = True, -600.0
    assert zone.step(None, ret=25.0).reason == REASON_ROOM_SENSOR_WAIT
    zone.step(19.9, ret=26.0)
    zone.step(19.8, ret=27.0)
    zone.step(19.9, ret=28.0)
    assert zone.model.holding_delta == pytest.approx(7.2)
    assert zone.step(20.3, ret=30.0, minutes=20).reason == REASON_PREDICTIVE_CLOSE
    zone.step(20.4)
    assert zone.ctrl.coasting
    assert zone.ctrl.coast.valid


@pytest.mark.parametrize("valve_at_grid_point", [False, None])  # closed, unavailable
def test_hold_keeps_asking_for_an_opening_the_valve_missed(valve_at_grid_point):
    zone = hold_zone()
    zone.step(21.0)
    zone.step(None)  # minute 2: lost
    assert not zone.step(None, minutes=42).valve_open  # minute 44
    zone.valve = valve_at_grid_point
    assert zone.step(None).valve_open  # minute 45: grid point
    zone.valve, zone.changed_at = False, -1e6  # the valve did not open
    # Still asked for, so the command is sent again
    decision = zone.step(None)
    assert decision.valve_open
    assert decision.reason == REASON_ROOM_SENSOR_LOST
    assert zone.step(None, minutes=16).valve_open
    assert not zone.step(None).valve_open  # minute 63: 45 + 18, as usual


def test_hold_opens_no_shorter_than_half_the_shortest_opening():
    zone = hold_zone()  # opens 18 min from each grid point
    zone.step(21.0)
    zone.step(None)  # minute 2: lost
    zone.step(None, minutes=42)
    # After a gap in the updates the grid point at minute 45 is seen at minute 61,
    # with 2 of its 18 minutes left: not worth opening for
    assert not zone.step(None, minutes=17).valve_open
    # The one at minute 90 is seen at minute 100, with 8 minutes left
    assert zone.step(None, minutes=39).valve_open
    assert not zone.step(None, minutes=8).valve_open


@pytest.mark.parametrize(
    ("duty", "share", "opens"),
    [(0.04, 0.0, False), (0.07, 300 / 2700, True), (0.9, 1.0, True)],
)
def test_hold_shows_the_share_the_valve_really_opens(duty, share, opens):
    zone = Zone(lost_room_hold=True, calibration=ZoneCalibration(duty=duty))
    zone.step(21.0)
    zone.step(None)  # minute 2: lost
    decision = zone.step(None, minutes=43)  # minute 45: grid point
    assert decision.fallback_share == pytest.approx(share)
    assert decision.valve_open is opens


def test_grid_ticks_once_at_the_first_step_after_each_grid_point():
    grid = Grid(2700.0, phase_s=600.0)  # grid points at minute 10, 55, 100 ...
    assert grid.last(3299.0) == 600.0
    assert grid.last(3300.0) == 3300.0
    assert grid.next(3300.0) == 6000.0

    assert not grid.tick(3400.0)  # the first step can't tell what is new
    assert not grid.tick(5999.0)
    assert grid.tick(6030.0)
    assert not grid.tick(6090.0)
    assert not grid.tick(5990.0)  # the clock stepped back: no tick,
    assert not grid.tick(6050.0)  # and the grid point does not tick again
    assert grid.tick(8700.0)
    assert not grid.tick(100.0)  # back by more than an interval: counting restarts
    assert grid.tick(600.0)

    grid.restart(8800.0)
    assert not grid.tick(8810.0)  # 8700 was passed before the restart
    assert grid.tick(11400.0)


@pytest.mark.parametrize(
    ("share", "minutes"),
    [
        (-0.1, 0),
        (0.0, 0),
        (0.05, 0),  # 2.25 min
        (0.0555, 0),  # just under 2.5 min
        (0.0556, 5),  # just over 2.5 min
        (0.06, 5),  # 2.7 min
        (0.4, 18),
        (0.88, 39.6),
        (0.8888, 39.996),
        (0.8889, 45),  # just over 40 min would leave less than 5 min closed
        (0.9, 45),
        (1.2, 45),
    ],
)
def test_hold_on_time_is_rounded_to_whole_valve_moves(share, minutes):
    assert hold_on_time(share, 2700.0) == pytest.approx(minutes * 60)


@pytest.mark.parametrize(("share", "seconds"), [(0.0625, 300.0), (0.875, 2100.0)])
def test_hold_on_time_at_its_exact_limits(share, seconds):
    # 150 s is half the shortest opening, and 2100 s leaves the shortest closing
    assert hold_on_time(share, 2400.0) == seconds


def test_zone_phase_is_stable_and_spreads_zones():
    assert zone_phase("01JA2B3C4D5E6F7G8H9JKMNPQR", 2700.0) == 779.0  # every start
    phases = [zone_phase(f"entry {i}", 2700.0) for i in range(100)]
    assert all(0 <= phase < 2700 for phase in phases)
    assert len(set(phases)) > 90
