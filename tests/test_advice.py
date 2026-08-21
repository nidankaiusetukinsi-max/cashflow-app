from datetime import date

import pandas as pd

from aggregates import NetWorthSummary
from advice import (
    _emergency_fund_advice,
    _life_stage_formation_thresholds,
    _mortgage_burden_advice,
    effective_nisa_ytd_before,
    generate_advice,
    year_elapsed_ratio,
)
from timeutil import today_jst
from db import (
    NISA_TSUMITATE_ANNUAL_LIMIT,
    SETTING_ANNUAL_INCOME_HUSBAND,
    SETTING_ANNUAL_INCOME_WIFE,
    SETTING_HUSBAND_BIRTH_YEAR,
    SETTING_MORTGAGE_MONTHLY_PAYMENT,
    SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND,
    SETTING_WIFE_BIRTH_YEAR,
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


def _accounts(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["id", "owner", "name", "kind", "initial_balance"])


def _children(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["id", "name", "birth_year"])


def _net_worth(shared_cash: float = 0.0, account_balances: dict[int, float] | None = None) -> NetWorthSummary:
    return NetWorthSummary(
        account_balances=account_balances or {},
        untagged_net=0.0,
        shared_cash=shared_cash,
        current_balance=0.0,
        nisa_lifetime_total=0.0,
        total_assets=0.0,
    )


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


# --- _life_stage_formation_thresholds ---


def test_life_stage_formation_thresholds_standard_when_no_birth_year_or_children():
    assert _life_stage_formation_thresholds({}, None, date(2026, 6, 1)) == (0.2, 0.1, "")


def test_life_stage_formation_thresholds_lowered_for_child_rearing():
    settings = {SETTING_HUSBAND_BIRTH_YEAR: 1990.0, SETTING_WIFE_BIRTH_YEAR: 1990.0}
    children = _children([{"id": 1, "name": "子", "birth_year": 2020}])
    healthy, low, note = _life_stage_formation_thresholds(settings, children, date(2026, 6, 1))
    assert (healthy, low) == (0.12, 0.05)
    assert "教育費" in note


def test_life_stage_formation_thresholds_raised_when_pre_retirement():
    settings = {SETTING_HUSBAND_BIRTH_YEAR: 1970.0, SETTING_WIFE_BIRTH_YEAR: 1970.0}  # age 56 in 2026
    healthy, low, note = _life_stage_formation_thresholds(settings, None, date(2026, 6, 1))
    assert (healthy, low) == (0.25, 0.15)
    assert "定年" in note


def test_life_stage_formation_thresholds_pre_retirement_takes_priority_over_child_rearing():
    settings = {SETTING_HUSBAND_BIRTH_YEAR: 1970.0, SETTING_WIFE_BIRTH_YEAR: 1990.0}
    children = _children([{"id": 1, "name": "子", "birth_year": 2020}])
    healthy, low, note = _life_stage_formation_thresholds(settings, children, date(2026, 6, 1))
    assert (healthy, low) == (0.25, 0.15)
    assert "定年" in note


# --- _emergency_fund_advice ---


def test_emergency_fund_advice_warns_when_below_minimum():
    today = date(2026, 6, 27)
    transactions = _transactions(
        [
            {"date": date(2026, 4, 1), "type": "expense", "category": "食費", "amount": 300_000, "owner": None},
            {"date": date(2026, 5, 1), "type": "expense", "category": "食費", "amount": 300_000, "owner": None},
            {"date": date(2026, 6, 1), "type": "expense", "category": "食費", "amount": 300_000, "owner": None},
        ]
    )
    accounts = _accounts([{"id": 1, "owner": "夫", "name": "銀行", "kind": "bank", "initial_balance": 0}])
    net_worth = _net_worth(shared_cash=0.0, account_balances={1: 500_000.0})
    tip = _emergency_fund_advice(accounts, net_worth, transactions, today)
    assert tip is not None
    assert "生活防衛資金" in tip and "優先" in tip


def test_emergency_fund_advice_reassures_when_comfortable():
    today = date(2026, 6, 27)
    transactions = _transactions(
        [
            {"date": date(2026, 4, 1), "type": "expense", "category": "食費", "amount": 100_000, "owner": None},
            {"date": date(2026, 5, 1), "type": "expense", "category": "食費", "amount": 100_000, "owner": None},
            {"date": date(2026, 6, 1), "type": "expense", "category": "食費", "amount": 100_000, "owner": None},
        ]
    )
    accounts = _accounts([{"id": 1, "owner": "夫", "name": "銀行", "kind": "bank", "initial_balance": 0}])
    net_worth = _net_worth(shared_cash=0.0, account_balances={1: 3_000_000.0})
    tip = _emergency_fund_advice(accounts, net_worth, transactions, today)
    assert tip is not None
    assert "十分な水準" in tip


def test_emergency_fund_advice_silent_when_in_normal_range():
    today = date(2026, 6, 27)
    transactions = _transactions(
        [
            {"date": date(2026, 4, 1), "type": "expense", "category": "食費", "amount": 300_000, "owner": None},
            {"date": date(2026, 5, 1), "type": "expense", "category": "食費", "amount": 300_000, "owner": None},
            {"date": date(2026, 6, 1), "type": "expense", "category": "食費", "amount": 300_000, "owner": None},
        ]
    )
    accounts = _accounts([{"id": 1, "owner": "夫", "name": "銀行", "kind": "bank", "initial_balance": 0}])
    # ~4.5 months of expense covered - within the 3-6 month "normal" band, so no message either way.
    net_worth = _net_worth(shared_cash=0.0, account_balances={1: 1_400_000.0})
    assert _emergency_fund_advice(accounts, net_worth, transactions, today) is None


def test_emergency_fund_advice_ignores_card_balances():
    today = date(2026, 6, 27)
    transactions = _transactions(
        [
            {"date": date(2026, 4, 1), "type": "expense", "category": "食費", "amount": 300_000, "owner": None},
            {"date": date(2026, 5, 1), "type": "expense", "category": "食費", "amount": 300_000, "owner": None},
            {"date": date(2026, 6, 1), "type": "expense", "category": "食費", "amount": 300_000, "owner": None},
        ]
    )
    # A large positive "balance" sitting on a card must not count as a liquid cushion.
    accounts = _accounts([{"id": 1, "owner": "夫", "name": "カード", "kind": "card", "initial_balance": 0}])
    net_worth = _net_worth(shared_cash=0.0, account_balances={1: 5_000_000.0})
    tip = _emergency_fund_advice(accounts, net_worth, transactions, today)
    assert tip is not None
    assert "優先" in tip


def test_emergency_fund_advice_skips_when_not_enough_history():
    today = date(2026, 6, 27)
    transactions = _transactions(
        [{"date": date(2026, 6, 20), "type": "expense", "category": "食費", "amount": 10_000, "owner": None}]
    )
    accounts = _accounts([{"id": 1, "owner": "夫", "name": "銀行", "kind": "bank", "initial_balance": 0}])
    net_worth = _net_worth(shared_cash=0.0, account_balances={1: 100.0})
    assert _emergency_fund_advice(accounts, net_worth, transactions, today) is None


# --- _mortgage_burden_advice ---


def test_mortgage_burden_advice_warns_when_ratio_exceeds_threshold():
    settings = {
        SETTING_MORTGAGE_MONTHLY_PAYMENT: 150_000.0,
        SETTING_ANNUAL_INCOME_HUSBAND: 4_800_000.0,  # monthly take-home = 400,000
        SETTING_ANNUAL_INCOME_WIFE: 0.0,
    }
    # 150,000 / 400,000 = 37.5% > 25%
    tip = _mortgage_burden_advice(settings)
    assert tip is not None
    assert "手取り月収" in tip and "25%以内" in tip


def test_mortgage_burden_advice_silent_when_within_threshold():
    settings = {
        SETTING_MORTGAGE_MONTHLY_PAYMENT: 80_000.0,
        SETTING_ANNUAL_INCOME_HUSBAND: 4_800_000.0,
        SETTING_ANNUAL_INCOME_WIFE: 0.0,
    }
    # 80,000 / 400,000 = 20% <= 25%
    assert _mortgage_burden_advice(settings) is None


def test_mortgage_burden_advice_skips_when_income_not_configured():
    settings = {SETTING_MORTGAGE_MONTHLY_PAYMENT: 150_000.0}
    assert _mortgage_burden_advice(settings) is None


# --- generate_advice wiring for the new optional context ---


def test_generate_advice_includes_mortgage_check_when_settings_provided():
    today = today_jst()
    transactions = _transactions(
        [{"date": today, "type": "expense", "category": "食費", "amount": 10_000, "owner": None}]
    )
    settings = {
        SETTING_ANNUAL_INCOME_HUSBAND: 6_000_000.0,  # monthly take-home = 500,000
        SETTING_ANNUAL_INCOME_WIFE: 0.0,
        SETTING_MORTGAGE_MONTHLY_PAYMENT: 200_000.0,  # 200,000 / 500,000 = 40% > 25%
    }
    advice = generate_advice(transactions, {}, settings, _accounts([]), _net_worth(), _children([]))
    assert any("手取り月収" in tip for tip in advice)


def test_generate_advice_formation_rate_message_includes_life_stage_note_for_child_rearing():
    today = today_jst()
    # monthly income 500,000; expense 440,000 -> formation rate 12%, exactly the lowered
    # "healthy" bar for child-rearing households (vs. the standard 20%).
    transactions = _transactions(
        [{"date": today, "type": "expense", "category": "食費", "amount": 440_000, "owner": None}]
    )
    settings = {
        SETTING_ANNUAL_INCOME_HUSBAND: 6_000_000.0,
        SETTING_ANNUAL_INCOME_WIFE: 0.0,
        SETTING_HUSBAND_BIRTH_YEAR: today.year - 35,
        SETTING_WIFE_BIRTH_YEAR: today.year - 35,
    }
    children = _children([{"id": 1, "name": "子", "birth_year": today.year - 5}])
    advice = generate_advice(transactions, {}, settings, _accounts([]), _net_worth(), children)
    assert any("良好です" in tip and "教育費" in tip for tip in advice)
