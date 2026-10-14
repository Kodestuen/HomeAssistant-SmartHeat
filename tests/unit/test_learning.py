"""Tests for the overshoot model and the zone's calibration."""

import json
import math

import pytest

from smartheat_core.learning import (
    MAX_OVERSHOOT,
    OvershootModel,
    ZoneCalibration,
    excess_heat,
)

H = 4.0
X = 8.0  # return - room at close
SHAPE = (X - H) - H * math.log(X / H)


def trained_model(beta: float = 0.5) -> OvershootModel:
    model = OvershootModel(initial_overshoot=0.4)
    model.add_holding_delta(H)
    for _ in range(3):
        model.add_cycle(beta * SHAPE, room_close=20.0, return_close=20.0 + X)
    return model


def test_excess_heat_shape():
    assert excess_heat(3.0, H) == 0.0
    assert excess_heat(H, H) == 0.0
    assert excess_heat(X, H) == pytest.approx(SHAPE)
    assert excess_heat(5.0, 0.0) == 0.0


def test_flat_overshoot_until_trained():
    model = OvershootModel(initial_overshoot=0.4)
    assert model.predict(20.0, 30.0) == pytest.approx(0.4)

    model.add_holding_delta(H)
    model.add_cycle(0.5 * SHAPE, room_close=20.0, return_close=28.0)
    model.add_cycle(0.5 * SHAPE, room_close=20.0, return_close=28.0)
    assert not model.trained
    assert model.predict(20.0, 28.0) == pytest.approx(model.flat_overshoot)

    model.add_cycle(0.5 * SHAPE, room_close=20.0, return_close=28.0)
    assert model.trained
    assert model.beta == pytest.approx(0.5)
    assert model.predict(20.0, 28.0) == pytest.approx(0.5 * SHAPE)
    assert model.predict(20.0, 23.0) == 0.0  # below what holding needs: no overshoot
    assert model.predict(20.0, None) == pytest.approx(model.flat_overshoot)


def test_first_beta_samples_are_averaged_then_ewma():
    model = OvershootModel(initial_overshoot=0.4)
    model.add_holding_delta(H)
    for beta in (0.2, 0.4, 0.6):
        model.add_cycle(beta * SHAPE, room_close=20.0, return_close=28.0)
    assert model.beta == pytest.approx(0.4)

    model.add_cycle(0.0, room_close=20.0, return_close=28.0)
    assert model.beta == pytest.approx(0.4 * 0.8)


def test_beta_needs_holding_delta_and_excess_heat():
    model = OvershootModel(initial_overshoot=0.4)
    model.add_cycle(0.5, room_close=20.0, return_close=28.0)  # h unknown
    assert model.beta is None

    model.add_holding_delta(H)
    model.add_cycle(0.5, room_close=20.0, return_close=24.2)  # barely above h
    assert model.beta is None
    assert model.cycles == 2


def test_flat_overshoot_is_ewma_from_initial_guess():
    model = OvershootModel(initial_overshoot=0.4)
    model.add_cycle(0.9, room_close=20.0, return_close=None)
    assert model.flat_overshoot == pytest.approx(0.4 + 0.2 * 0.5)
    assert model.cycles == 1


def test_holding_delta_is_averaged_and_ignores_artefacts():
    model = OvershootModel(initial_overshoot=0.4)
    model.add_holding_delta(0.1)
    assert model.holding_delta is None
    model.add_holding_delta(4.0)
    model.add_holding_delta(5.0)
    assert model.holding_delta == pytest.approx(4.2)


def test_predictions_and_samples_are_clamped():
    model = trained_model(beta=0.5)
    assert model.predict(20.0, 60.0) == MAX_OVERSHOOT

    model.add_cycle(5.0, room_close=20.0, return_close=28.0)
    assert model.last_overshoot == MAX_OVERSHOOT
    model.add_cycle(-0.3, room_close=20.0, return_close=28.0)
    assert model.last_overshoot == 0.0


def test_heat_up_rate_is_averaged():
    model = OvershootModel(initial_overshoot=0.4)
    model.add_heat_up_rate(1.0)
    model.add_heat_up_rate(2.0)
    assert model.heat_up_rate == pytest.approx(1.2)


def test_round_trip_and_reset():
    model = trained_model()
    model.add_cycle(0.3, room_close=20.0, return_close=27.0, peak_error=0.1)
    model.add_heat_up_rate(0.8)

    restored = OvershootModel.from_dict(model.to_dict(), 0.4)
    assert restored == model

    revision = restored.revision
    restored.reset()
    assert restored == OvershootModel(initial_overshoot=0.4)
    assert restored.revision > revision


def test_from_empty_storage():
    model = OvershootModel.from_dict(None, 0.2)
    assert model.flat_overshoot == 0.2
    assert model.cycles == 0
    assert not model.trained


def test_model_ignores_the_calibration_saved_next_to_it():
    model = trained_model()
    calibration = ZoneCalibration(0.55, 4.1, 0.15, -3.0, 1.7e9).to_dict()
    stored = {**model.to_dict(), "calibration": calibration}
    assert OvershootModel.from_dict(stored, 0.4) == model


@pytest.mark.parametrize("stored", [None, {}, {"duty": None}, {"duty": "high"}])
def test_calibration_defaults(stored):
    calibration = ZoneCalibration.from_dict(stored)
    assert calibration == ZoneCalibration()
    assert calibration.duty == 0.4
    assert calibration.return_offset is None
    assert calibration.offset_slope is None
    assert calibration.last_outside is None
    assert calibration.last_outside_at is None


@pytest.mark.parametrize(
    ("duty", "restored"), [(1.7, 1.0), (-0.2, 0.0), (float("nan"), 0.4), (0.25, 0.25)]
)
def test_calibration_duty_is_kept_within_0_and_1(duty, restored):
    assert ZoneCalibration.from_dict({"duty": duty}).duty == restored


def test_calibration_round_trip():
    calibration = ZoneCalibration(
        duty=0.37,
        return_offset=4.1,
        offset_slope=0.15,
        last_outside=-3.5,
        last_outside_at=1.7e9,
    )
    assert calibration.to_dict() == {
        "duty": 0.37,
        "return_offset": 4.1,
        "offset_slope": 0.15,
        "last_outside": -3.5,
        "last_outside_at": 1.7e9,
    }
    # Saved as JSON by Home Assistant
    stored = json.loads(json.dumps(calibration.to_dict()))
    assert ZoneCalibration.from_dict(stored) == calibration


def test_calibration_saved_by_version_0_3_1_loads():
    calibration = ZoneCalibration.from_dict({"duty": 0.37})
    assert calibration == ZoneCalibration(duty=0.37)
    assert calibration.return_offset is None  # the tickle starts it when needed


def test_calibration_keeps_to_its_own_keys():
    # Version 0.3.1 reads only "duty", which stays a plain share of 0-1
    saved = ZoneCalibration(duty=0.37, return_offset=4.1, offset_slope=0.15).to_dict()
    assert saved["duty"] == 0.37
    # Keys of a newer version are left out
    assert ZoneCalibration.from_dict({**saved, "new": 1}) == ZoneCalibration.from_dict(
        saved
    )


@pytest.mark.parametrize(
    ("stored", "offset", "slope"),
    [
        ({"return_offset": "warm", "offset_slope": 0.15}, None, None),
        ({"return_offset": float("nan"), "offset_slope": 0.15}, None, None),
        ({"return_offset": 4.1}, None, None),  # c is of no use without its slope
        ({"return_offset": 4.1, "offset_slope": None}, None, None),
        ({"return_offset": 4.1, "offset_slope": 2.0}, None, None),
        ({"return_offset": 4.1, "offset_slope": -0.1}, None, None),
        # Only the slopes the tickle uses: 0.15 with an outdoor term, 0 without
        ({"return_offset": 4.1, "offset_slope": 0.5}, None, None),
        ({"return_offset": 4.1, "offset_slope": 1.0}, None, None),
        ({"offset_slope": 0.15}, None, None),
        ({"return_offset": 40.0, "offset_slope": 0.15}, 25.0, 0.15),  # kept in bounds
        ({"return_offset": -12.0, "offset_slope": 0.0}, -10.0, 0.0),
        ({"return_offset": "4.1", "offset_slope": "0"}, 4.1, 0.0),
    ],
)
def test_calibration_return_offset_that_cant_be_used_starts_afresh(
    stored, offset, slope
):
    calibration = ZoneCalibration.from_dict({"duty": 0.3, **stored})
    assert calibration.duty == 0.3
    assert calibration.return_offset == offset
    assert calibration.offset_slope == slope


@pytest.mark.parametrize(
    "stored",
    [
        {"last_outside": -3.0},  # no time: it can't be held
        {"last_outside_at": 1.7e9},
        {"last_outside": "cold", "last_outside_at": 1.7e9},
        {"last_outside": -3.0, "last_outside_at": float("inf")},
        # Readings no outdoor sensor gives, e.g. a faulty one's
        {"last_outside": 1e300, "last_outside_at": 1.7e9},
        {"last_outside": -127.0, "last_outside_at": 1.7e9},
        {"last_outside": 85.0, "last_outside_at": 1.7e9},
    ],
)
def test_calibration_outdoor_reading_that_cant_be_used_is_dropped(stored):
    calibration = ZoneCalibration.from_dict(stored)
    assert calibration.last_outside is None
    assert calibration.last_outside_at is None


def test_calibration_revision_counts_the_outdoor_reading_moving_or_ageing():
    calibration = ZoneCalibration.from_dict({"last_outside": 2.0, "last_outside_at": 0})
    calibration.see_outside(2.4, 600.0)
    assert (calibration.last_outside, calibration.last_outside_at) == (2.4, 600.0)
    assert calibration.revision == 0  # as loaded, give or take 0.4 °C
    calibration.see_outside(1.5, 1200.0)  # 0.5 °C below the last change
    assert calibration.revision == 1
    calibration.see_outside(1.4, 4799.0)
    assert calibration.revision == 1
    calibration.see_outside(1.4, 4800.0)  # an hour after the last change
    assert calibration.revision == 2
    calibration.see_outside(1.4, 1000.0)  # the clock stepped back an hour
    assert calibration.revision == 3


def test_calibration_revision_counts_the_first_outdoor_reading():
    calibration = ZoneCalibration()
    calibration.see_outside(-2.0, 1.7e9)
    assert calibration.revision == 1
    assert calibration.to_dict()["last_outside"] == -2.0


def test_calibration_revision_counts_the_return_offset_moving_by_0_05():
    calibration = ZoneCalibration(return_offset=4.0, offset_slope=0.15)
    calibration.adjust_offset(0.03)
    calibration.adjust_offset(0.019)
    assert calibration.revision == 0  # 0.049
    calibration.adjust_offset(0.002)  # 0.051
    assert calibration.revision == 1
    calibration.adjust_offset(-0.04)
    assert calibration.revision == 1
    calibration.adjust_offset(-0.011)  # 0.051 below the last change
    assert calibration.revision == 2
    assert calibration.return_offset == pytest.approx(4.0)


def test_calibration_return_offset_stays_within_its_bounds():
    calibration = ZoneCalibration(return_offset=24.9, offset_slope=0.15)
    calibration.adjust_offset(1.0)
    assert calibration.return_offset == 25.0
    calibration.adjust_offset(-40.0)
    assert calibration.return_offset == -10.0


@pytest.mark.parametrize(
    ("h", "cycles", "slope", "target", "outside", "offset"),
    [
        (None, 0, 0.15, 21.0, 0.0, 4.35),  # nothing learned: the default
        (None, 0, 0.0, 21.0, None, 7.5),  # ... without an outdoor term
        (6.0, 2, 0.15, 21.0, 0.0, 4.35),  # fewer than 3 cycles: h isn't trusted
        (6.0, 3, 0.15, 21.0, 0.0, 2.85),  # h - 0.15 * (21 - 0)
        (6.0, 3, 0.15, 19.0, 5.0, 3.9),  # h - 0.15 * (19 - 5)
        (6.0, 3, 0.15, 21.0, None, 2.85),  # outdoor unknown: 0 °C
        (6.0, 3, 0.0, 21.0, None, 6.0),
        (0.3, 3, 0.15, 21.0, 0.0, 1.35),  # -2.85: at most 3 below the default
        (15.0, 3, 0.15, 21.0, 0.0, 9.35),  # 11.85: at most 5 above
        (0.3, 3, 0.0, 21.0, None, 4.5),
        (15.0, 3, 0.0, 21.0, None, 12.5),
    ],
)
def test_calibration_return_offset_is_seeded_from_h(
    h, cycles, slope, target, outside, offset
):
    model = OvershootModel(initial_overshoot=0.4, holding_delta=h, cycles=cycles)
    calibration = ZoneCalibration()
    calibration.seed_offset(model, slope, target, outside)
    assert calibration.return_offset == pytest.approx(offset)
    assert calibration.offset_slope == slope
    assert calibration.revision == 1


def test_calibration_return_offset_converts_between_outdoor_slopes():
    calibration = ZoneCalibration(return_offset=4.35, offset_slope=0.15)
    calibration.set_offset_slope(0.0)  # the outdoor term is dropped
    assert calibration.return_offset == pytest.approx(7.5)  # + 0.15 * 21
    assert calibration.offset_slope == 0.0
    assert calibration.revision == 1
    calibration.set_offset_slope(0.15)  # and back
    assert calibration.return_offset == pytest.approx(4.35)
    assert calibration.revision == 2


def test_calibration_reset_offset_keeps_the_duty():
    calibration = ZoneCalibration(duty=0.3, return_offset=4.1, offset_slope=0.15)
    calibration.reset_offset()
    assert calibration.return_offset is None
    assert calibration.offset_slope is None
    assert calibration.duty == 0.3
    assert calibration.revision == 1
    calibration.reset_offset()  # nothing left to forget
    assert calibration.revision == 1


def test_duty_is_a_24h_average_of_the_open_share():
    calibration = ZoneCalibration()
    calibration.add_valve_time(True, 300)
    step = 1 - math.exp(-300 / 86400)
    assert calibration.duty == pytest.approx(0.4 + 0.6 * step)
    duty = calibration.duty
    calibration.add_valve_time(False, 300)
    assert calibration.duty == pytest.approx(duty - duty * step)
    calibration.add_valve_time(True, 0)
    assert calibration.duty == pytest.approx(duty - duty * step)
    calibration.add_valve_time(True, -1e6)  # a clock step back: no time passed
    assert calibration.duty == pytest.approx(duty - duty * step)


def test_calibration_revision_follows_lasting_changes_only():
    calibration = ZoneCalibration()
    for _ in range(4):  # 0.4 -> 0.4083
        calibration.add_valve_time(True, 300)
    assert calibration.revision == 0
    calibration.add_valve_time(True, 300)  # 0.4103: moved 0.01
    assert calibration.revision == 1

    calibration.add_valve_time(True, 300)  # 0.4124
    for _ in range(8):  # back down to 0.4011, 0.0092 below the last change
        calibration.add_valve_time(False, 300)
    assert calibration.revision == 1
    calibration.add_valve_time(False, 300)  # 0.3997
    assert calibration.revision == 2
