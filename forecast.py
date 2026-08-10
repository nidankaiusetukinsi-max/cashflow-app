"""Simplified long-term life-plan forecasting (inflation, mortgage, retirement, pension, childcare).

These are rough, illustrative projections for household planning, not financial advice.
"""

from datetime import date

import pandas as pd

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
    SETTING_WIFE_BIRTH_YEAR,
    SETTING_WIFE_PENSION_ANNUAL,
    SETTING_WIFE_PENSION_START_AGE,
    SETTING_WIFE_RETIREMENT_AGE,
)

HORIZON_YEARS = 40


def build_expense_forecast(base_annual_expense: float, settings: dict[str, float]) -> pd.DataFrame:
    """Project annual expense forward, adjusted for inflation and mortgage payoff, plus pension income."""
    start_year = date.today().year
    inflation_rate = settings.get(SETTING_INFLATION_RATE, 0.0) / 100
    mortgage_annual = settings.get(SETTING_MORTGAGE_MONTHLY_PAYMENT, 0.0) * 12
    mortgage_payoff_year = settings.get(SETTING_MORTGAGE_PAYOFF_YEAR, 0.0)
    husband_birth_year = settings.get(SETTING_HUSBAND_BIRTH_YEAR, 0.0)
    wife_birth_year = settings.get(SETTING_WIFE_BIRTH_YEAR, 0.0)
    husband_pension_start = settings.get(SETTING_HUSBAND_PENSION_START_AGE, 0.0)
    husband_pension_annual = settings.get(SETTING_HUSBAND_PENSION_ANNUAL, 0.0)
    wife_pension_start = settings.get(SETTING_WIFE_PENSION_START_AGE, 0.0)
    wife_pension_annual = settings.get(SETTING_WIFE_PENSION_ANNUAL, 0.0)

    rows = []
    for i in range(HORIZON_YEARS + 1):
        year = start_year + i
        inflated_expense = base_annual_expense * ((1 + inflation_rate) ** i)
        if mortgage_payoff_year > 0 and mortgage_annual > 0 and year > mortgage_payoff_year:
            inflated_expense = max(inflated_expense - mortgage_annual, 0.0)

        pension_income = 0.0
        if husband_birth_year > 0 and husband_pension_start > 0:
            if year - husband_birth_year >= husband_pension_start:
                pension_income += husband_pension_annual
        if wife_birth_year > 0 and wife_pension_start > 0:
            if year - wife_birth_year >= wife_pension_start:
                pension_income += wife_pension_annual

        rows.append({"year": year, "projected_expense": inflated_expense, "pension_income": pension_income})

    return pd.DataFrame(rows)


def build_life_events(settings: dict[str, float]) -> list[dict]:
    """Return marker events (year, label) for annotating the forecast chart."""
    husband_birth_year = settings.get(SETTING_HUSBAND_BIRTH_YEAR, 0.0)
    wife_birth_year = settings.get(SETTING_WIFE_BIRTH_YEAR, 0.0)
    mortgage_payoff_year = settings.get(SETTING_MORTGAGE_PAYOFF_YEAR, 0.0)
    husband_retirement_age = settings.get(SETTING_HUSBAND_RETIREMENT_AGE, 0.0)
    wife_retirement_age = settings.get(SETTING_WIFE_RETIREMENT_AGE, 0.0)
    husband_pension_start = settings.get(SETTING_HUSBAND_PENSION_START_AGE, 0.0)
    wife_pension_start = settings.get(SETTING_WIFE_PENSION_START_AGE, 0.0)

    events: list[dict] = []
    if mortgage_payoff_year > 0:
        events.append({"year": int(mortgage_payoff_year), "label": "住宅ローン完済"})
    if husband_birth_year > 0 and husband_retirement_age > 0:
        events.append({"year": int(husband_birth_year + husband_retirement_age), "label": "夫の定年"})
    if wife_birth_year > 0 and wife_retirement_age > 0:
        events.append({"year": int(wife_birth_year + wife_retirement_age), "label": "嫁の定年"})
    if husband_birth_year > 0 and husband_pension_start > 0:
        events.append({"year": int(husband_birth_year + husband_pension_start), "label": "夫の年金開始"})
    if wife_birth_year > 0 and wife_pension_start > 0:
        events.append({"year": int(wife_birth_year + wife_pension_start), "label": "嫁の年金開始"})
    return events


def build_childcare_forecast(settings: dict[str, float], children: pd.DataFrame) -> pd.DataFrame:
    """Project the national-average/median childcare cost benchmark forward, per registered child."""
    if children.empty:
        return pd.DataFrame(columns=["year", "childcare_cost"])

    start_year = date.today().year
    inflation_rate = settings.get(SETTING_INFLATION_RATE, 0.0) / 100
    annual_cost = settings.get(SETTING_CHILDCARE_ANNUAL_COST, 0.0)
    end_age = settings.get(SETTING_CHILDCARE_END_AGE, 0.0) or 22

    rows = []
    for i in range(HORIZON_YEARS + 1):
        year = start_year + i
        cost = 0.0
        for child in children.itertuples():
            age = year - child.birth_year
            if 0 <= age <= end_age:
                cost += annual_cost * ((1 + inflation_rate) ** i)
        rows.append({"year": year, "childcare_cost": cost})

    df = pd.DataFrame(rows)
    return df[df["childcare_cost"] > 0]
