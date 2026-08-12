import pandas as pd
import pytest

from timeutil import today_jst
from db import (
    SETTING_CHILDCARE_ANNUAL_COST,
    SETTING_CHILDCARE_END_AGE,
    SETTING_HUSBAND_BIRTH_YEAR,
    SETTING_HUSBAND_PENSION_ANNUAL,
    SETTING_HUSBAND_PENSION_START_AGE,
    SETTING_HUSBAND_RETIREMENT_AGE,
    SETTING_INFLATION_RATE,
    SETTING_MORTGAGE_MONTHLY_PAYMENT,
    SETTING_MORTGAGE_PAYOFF_YEAR,
)
from forecast import build_childcare_forecast, build_expense_forecast, build_life_events


def test_build_expense_forecast_applies_compounding_inflation():
    settings = {SETTING_INFLATION_RATE: 10.0}
    df = build_expense_forecast(1_000_000, settings)
    start_year = today_jst().year
    row = df[df["year"] == start_year + 5].iloc[0]
    assert row["projected_expense"] == pytest.approx(1_000_000 * (1.1**5))


def test_build_expense_forecast_subtracts_mortgage_only_after_payoff_year():
    start_year = today_jst().year
    settings = {
        SETTING_INFLATION_RATE: 0.0,
        SETTING_MORTGAGE_MONTHLY_PAYMENT: 100_000.0,
        SETTING_MORTGAGE_PAYOFF_YEAR: start_year + 2,
    }
    df = build_expense_forecast(4_000_000, settings)
    payoff_year_row = df[df["year"] == start_year + 2].iloc[0]
    next_year_row = df[df["year"] == start_year + 3].iloc[0]
    assert payoff_year_row["projected_expense"] == 4_000_000
    assert next_year_row["projected_expense"] == 4_000_000 - 100_000 * 12


def test_build_expense_forecast_adds_pension_income_from_start_age():
    start_year = today_jst().year
    settings = {
        SETTING_HUSBAND_BIRTH_YEAR: start_year - 64,
        SETTING_HUSBAND_PENSION_START_AGE: 65,
        SETTING_HUSBAND_PENSION_ANNUAL: 1_200_000.0,
    }
    df = build_expense_forecast(3_000_000, settings)
    before_65 = df[df["year"] == start_year].iloc[0]
    after_65 = df[df["year"] == start_year + 1].iloc[0]
    assert before_65["pension_income"] == 0.0
    assert after_65["pension_income"] == 1_200_000.0


def test_build_life_events_computes_year_from_birth_year_plus_age():
    settings = {SETTING_HUSBAND_BIRTH_YEAR: 1990, SETTING_HUSBAND_RETIREMENT_AGE: 65}
    events = build_life_events(settings)
    assert {"year": 2055, "label": "夫の定年"} in events


def test_build_childcare_forecast_empty_when_no_children():
    df = build_childcare_forecast({}, pd.DataFrame(columns=["id", "name", "birth_year"]))
    assert df.empty


def test_build_childcare_forecast_only_covers_configured_age_range():
    start_year = today_jst().year
    settings = {
        SETTING_CHILDCARE_ANNUAL_COST: 1_000_000.0,
        SETTING_CHILDCARE_END_AGE: 3,
        SETTING_INFLATION_RATE: 0.0,
    }
    children = pd.DataFrame({"id": [1], "name": ["長男"], "birth_year": [start_year]})
    df = build_childcare_forecast(settings, children)
    covered_years = set(df["year"])
    assert covered_years == {start_year, start_year + 1, start_year + 2, start_year + 3}
