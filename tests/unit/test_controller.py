"""Tests for the valve controller rules."""

import math

import pytest

from smartheat_core.controller import (
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
    Inputs,
)
from smartheat_core.learning import OvershootModel


class Zone:
    """Drives a controller the way Home Assistant does, with a valve that obeys at once.

    Defaults: target 21 °C, hysteresis 0.3, min open/closed 15 min, coast window 4 h.
    """

    def __init__(self, initial_overshoot: float = 0.4, **config) -> None:
        self.model = OvershootModel(initial_overshoot=initial_overshoot)
        self.ctrl = Controller(ControllerConfig(**config), self.model)
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
    assert decision.predicted_overshoot == pytest.approx(0.5 * (4.0 - 4.0 * math.log(2)))
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
