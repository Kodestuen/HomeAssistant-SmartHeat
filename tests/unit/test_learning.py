"""Tests for the overshoot model."""

import math

import pytest

from smartheat_core.learning import MAX_OVERSHOOT, OvershootModel, excess_heat

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
