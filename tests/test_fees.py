"""Unit tests for the platform fee models.

Covers Property 8 (fee accounting): each model returns the hand-computed fee for
buying contracts at a given price, including the price extremes where the
Kalshi fee vanishes. **Validates: Requirements 2.5, 5.2**
"""

from __future__ import annotations

import math

import pytest

from scanner.fees import FeeModel, FlatFeeModel, KalshiFeeModel


# --- FlatFeeModel -----------------------------------------------------------

def test_flat_fee_zero_rate_is_always_free():
    model = FlatFeeModel(rate=0.0)
    assert model.fee_for(0.5, 100) == 0.0
    assert model.fee_for(0.99, 1000) == 0.0


@pytest.mark.parametrize(
    "rate, price, contracts, expected",
    [
        (0.02, 0.5, 100, 1.0),    # 0.02 * 0.5 * 100
        (0.01, 0.4, 50, 0.2),     # 0.01 * 0.4 * 50
        (0.05, 1.0, 10, 0.5),     # 0.05 * 1.0 * 10
        (0.05, 0.0, 10, 0.0),     # notional zero at price 0
        (0.10, 0.25, 4, 0.1),     # 0.10 * 0.25 * 4
    ],
)
def test_flat_fee_matches_hand_computed(rate, price, contracts, expected):
    model = FlatFeeModel(rate=rate)
    assert model.fee_for(price, contracts) == pytest.approx(expected)


def test_flat_fee_zero_contracts_is_zero():
    assert FlatFeeModel(rate=0.03).fee_for(0.5, 0) == 0.0


def test_flat_fee_rejects_negative_rate():
    with pytest.raises(ValueError):
        FlatFeeModel(rate=-0.01)


def test_flat_fee_rejects_negative_contracts():
    with pytest.raises(ValueError):
        FlatFeeModel(rate=0.02).fee_for(0.5, -1)


# --- KalshiFeeModel ---------------------------------------------------------

@pytest.mark.parametrize(
    "price, contracts, expected",
    [
        # 0.07 * contracts * p * (1-p), rounded UP to the next cent.
        (0.5, 1, 0.02),     # raw 0.0175 -> ceil to 0.02
        (0.5, 100, 1.75),   # raw 1.75 -> exact
        (0.1, 1, 0.01),     # raw 0.0063 -> ceil to 0.01
        (0.2, 10, 0.12),    # raw 0.112 -> ceil to 0.12
        (0.9, 1, 0.01),     # raw 0.0063 -> ceil to 0.01 (symmetric with 0.1)
        (0.75, 100, 1.32),  # raw 0.07*100*0.75*0.25 = 1.3125 -> ceil to 1.32
    ],
)
def test_kalshi_fee_matches_hand_computed(price, contracts, expected):
    model = KalshiFeeModel()
    assert model.fee_for(price, contracts) == pytest.approx(expected)


@pytest.mark.parametrize("price", [0.0, 1.0])
def test_kalshi_fee_is_zero_at_price_extremes(price):
    # p*(1-p) == 0 at both extremes, so the fee vanishes regardless of size.
    model = KalshiFeeModel()
    assert model.fee_for(price, 1000) == 0.0


def test_kalshi_fee_zero_contracts_is_zero():
    assert KalshiFeeModel().fee_for(0.5, 0) == 0.0


def test_kalshi_fee_is_maximized_at_one_half():
    model = KalshiFeeModel()
    contracts = 1000  # large enough that rounding does not mask the ordering
    at_half = model.fee_for(0.5, contracts)
    assert at_half >= model.fee_for(0.3, contracts)
    assert at_half >= model.fee_for(0.7, contracts)
    assert at_half >= model.fee_for(0.05, contracts)


def test_kalshi_fee_custom_coefficient():
    model = KalshiFeeModel(coefficient=0.035)
    # 0.035 * 100 * 0.5 * 0.5 = 0.875 -> ceil to 0.88
    assert model.fee_for(0.5, 100) == pytest.approx(0.88)


def test_kalshi_fee_rounds_up_to_next_cent():
    model = KalshiFeeModel()
    raw = 0.07 * 1 * 0.5 * 0.5  # 0.0175
    assert model.fee_for(0.5, 1) == math.ceil(raw * 100) / 100


def test_kalshi_fee_rejects_negative_coefficient():
    with pytest.raises(ValueError):
        KalshiFeeModel(coefficient=-0.01)


def test_kalshi_fee_rejects_negative_contracts():
    with pytest.raises(ValueError):
        KalshiFeeModel().fee_for(0.5, -5)


# --- Protocol conformance ---------------------------------------------------

def test_models_satisfy_fee_model_protocol():
    assert isinstance(FlatFeeModel(), FeeModel)
    assert isinstance(KalshiFeeModel(), FeeModel)
