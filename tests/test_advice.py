from datetime import date

import pandas as pd

from advice import effective_nisa_ytd_before, generate_advice, year_elapsed_ratio
from timeutil import today_jst
from db import (
    NISA_TSUMITATE_ANNUAL_LIMIT,
    SETTING_ANNUAL_INCOME_HUSBAND,
    SETTING_ANNUAL_INCOME_WIFE,
    SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND,
)


def _transactions(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(
        rows,
        columns=["date", "type", "category", "amount", "owner"],
    )
    df["date"] = pd.to_datetime(df["date"])
    return df


def _empty_transactions() -> pd.DataFrame:
    return _transactions([])


def test_year_elapsed_ratio_new_year_day():
    assert year_elapsed_ratio(date(2025, 1, 1)) == 1 / 365


def test_year_elapsed_ratio_last_day_of_non_leap_year():
    assert year_elapsed_ratio(date(2025, 12, 31)) == 1.0


def test_year_elapsed_ratio_leap_year_uses_366_days():
    # day-of-year for March 1st in a leap year is 61 (31 Jan + 29 Feb + 1)
    assert year_elapsed_ratio(date(2024, 3, 1)) == 61 / 366


def test_effective_nisa_ytd_before_returns_value_when_year_matches():
    today = date(2026, 6, 1)
    settings = {
        SETTING_TSUMITATE_YTD_BEFORE_HUSBAND: 100_000.0,
        SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND: 2026,
    }
    assert effective_nisa_ytd_before(settings, "夫", "つみたて投資枠", today) == 100_000.0


def test_effective_nisa_ytd_before_ignored_once_year_rolls_over():
    # The recorded baseline is tagged to 2026; once the calendar rolls into 2027 it must
    # stop counting toward "this year's" progress, or every future year would start
    # permanently inflated by that old baseline.
    today = date(2027, 1, 1)
    settings = {
        SETTING_TSUMITATE_YTD_BEFORE_HUSBAND: 100_000.0,
        SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND: 2026,
    }
    assert effective_nisa_ytd_before(settings, "夫", "つみたて投資枠", today) == 0.0


def test_effective_nisa_ytd_before_defaults_to_zero_when_never_recorded():
    assert effective_nisa_ytd_before({}, "夫", "つみたて投資枠", date(2026, 6, 1)) == 0.0


def test_generate_advice_no_data_returns_fallback_message():
    assert generate_advice(_empty_transactions(), {}, {}) == [
        "取引を記録すると、ここに家計の健全化アドバイスが表示されます。"
    ]


def test_generate_advice_flags_healthy_formation_rate():
    today = today_jst()
    transactions = _transactions(
        [{"date": today, "type": "expense", "category": "食費", "amount": 10_000, "owner": None}]
    )
    settings = {SETTING_ANNUAL_INCOME_HUSBAND: 6_000_000.0, SETTING_ANNUAL_INCOME_WIFE: 0.0}
    advice = generate_advice(transactions, {}, settings)
    assert any("良好です" in tip for tip in advice)


def test_generate_advice_flags_low_formation_rate():
    today = today_jst()
    # monthly income = 6,000,000 / 12 = 500,000; expense 480,000 -> formation rate 4% (<10%)
    transactions = _transactions(
        [{"date": today, "type": "expense", "category": "食費", "amount": 480_000, "owner": None}]
    )
    settings = {SETTING_ANNUAL_INCOME_HUSBAND: 6_000_000.0, SETTING_ANNUAL_INCOME_WIFE: 0.0}
    advice = generate_advice(transactions, {}, settings)
    assert any("低め" in tip for tip in advice)


def test_generate_advice_flags_budget_overrun():
    today = today_jst()
    transactions = _transactions(
        [{"date": today, "type": "expense", "category": "食費", "amount": 50_000, "owner": None}]
    )
    advice = generate_advice(transactions, {"食費": 30_000.0}, {})
    assert any("予算を" in tip and "超過" in tip for tip in advice)


def test_generate_advice_flags_nisa_fully_contributed():
    today = today_jst()
    transactions = _transactions(
        [
            {
                "date": today,
                "type": "investment",
                "category": "つみたて投資枠",
                "amount": NISA_TSUMITATE_ANNUAL_LIMIT,
                "owner": "夫",
            }
        ]
    )
    advice = generate_advice(transactions, {}, {})
    assert any("年間上限まで拠出済み" in tip for tip in advice)
