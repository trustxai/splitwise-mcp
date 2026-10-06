"""Unit tests for the shared input normalisers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from splitwise_mcp.validators import currency_code, date_iso, money


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("25.5", "25.50"),
        ("10", "10.00"),
        (" 3.33 ", "3.33"),
        (7, "7.00"),
        (Decimal("0.1"), "0.10"),
        ("1000000.99", "1000000.99"),
    ],
)
def test_money_normalises_to_two_places(value: object, expected: str) -> None:
    assert money(value) == expected


@pytest.mark.parametrize("value", [0.1, 25.5, True, False])
def test_money_rejects_floats_and_bools(value: object) -> None:
    with pytest.raises(ValueError, match="not a float/bool"):
        money(value)


@pytest.mark.parametrize("value", ["abc", "", "1,50", "NaN", "Infinity"])
def test_money_rejects_non_decimals(value: str) -> None:
    with pytest.raises(ValueError):
        money(value)


def test_money_rejects_more_than_two_places() -> None:
    with pytest.raises(ValueError, match="at most 2 decimal places"):
        money("1.005", field="cost")


def test_money_rejects_zero_and_negative_by_default() -> None:
    with pytest.raises(ValueError, match="cost must be positive"):
        money("0", field="cost")
    with pytest.raises(ValueError, match="positive"):
        money("-1.00")


def test_money_allow_zero() -> None:
    assert money("0", allow_zero=True) == "0.00"
    with pytest.raises(ValueError, match="zero or positive"):
        money("-0.01", allow_zero=True)


@pytest.mark.parametrize(("value", "expected"), [("usd", "USD"), (" pen ", "PEN"), ("BTC", "BTC")])
def test_currency_code_upper_cases(value: str, expected: str) -> None:
    assert currency_code(value) == expected


@pytest.mark.parametrize("value", ["US", "USDT", "12A", "", "u$d"])
def test_currency_code_rejects_non_three_letter(value: str) -> None:
    with pytest.raises(ValueError, match="3-letter code"):
        currency_code(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-05", "2026-10-05T00:00:00Z"),
        ("2026-10-05T13:00:00Z", "2026-10-05T13:00:00Z"),
        ("2026-10-05T13:00:00", "2026-10-05T13:00:00Z"),
        ("2026-10-05T08:00:00-05:00", "2026-10-05T13:00:00Z"),
        (" 2026-10-05T13:00:00.250+00:00 ", "2026-10-05T13:00:00Z"),
    ],
)
def test_date_iso_normalises_to_utc_zulu(value: str, expected: str) -> None:
    assert date_iso(value) == expected


@pytest.mark.parametrize("value", ["", "yesterday", "05/10/2026", "2026-13-01"])
def test_date_iso_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError, match="ISO-8601"):
        date_iso(value, field="dated_after")
