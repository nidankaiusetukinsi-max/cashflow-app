from datetime import date

import pandas as pd

from ui_helpers import (
    account_select_index,
    build_account_labels,
    category_edit_options,
    csv_safe_value,
    filter_by_time_range,
    month_select_index,
    parse_month_label,
    progress_ratio,
    resolved_default,
    safe_index,
    time_range_start,
)


def _transactions(dates: list[str]) -> pd.DataFrame:
    df = pd.DataFrame({"date": dates})
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_time_range_start_one_month_uses_calendar_month():
    today = pd.Timestamp("2026-03-31")
    assert time_range_start(pd.DataFrame(), "1ヶ月", today) == pd.Timestamp("2026-02-28")


def test_time_range_start_this_year_is_january_first():
    today = pd.Timestamp("2026-08-13")
    assert time_range_start(pd.DataFrame(), "今年", today) == pd.Timestamp("2026-01-01")


def test_time_range_start_all_returns_earliest_transaction_date():
    df = _transactions(["2024-05-01", "2023-01-10", "2025-01-01"])
    today = pd.Timestamp("2026-08-13")
    assert time_range_start(df, "すべて", today) == pd.Timestamp("2023-01-10")


def test_time_range_start_all_returns_none_when_empty():
    today = pd.Timestamp("2026-08-13")
    assert time_range_start(pd.DataFrame(), "すべて", today) is None


def test_filter_by_time_range_excludes_transactions_before_the_window():
    df = _transactions(["2020-01-01", "2026-08-01"])
    filtered = filter_by_time_range(df, "1ヶ月")
    assert len(filtered) == 1
    assert filtered.iloc[0]["date"] == pd.Timestamp("2026-08-01")


def test_filter_by_time_range_all_returns_everything_unchanged():
    df = _transactions(["2020-01-01", "2026-08-01"])
    filtered = filter_by_time_range(df, "すべて")
    assert len(filtered) == 2


def test_resolved_default_returns_saved_value_when_present():
    assert resolved_default("k", legacy=999, present_keys={"k"}, settings_={"k": 0.0}) == 0


def test_resolved_default_falls_back_to_legacy_when_never_saved():
    assert resolved_default("k", legacy=999, present_keys=set(), settings_={"k": 0.0}) == 999


def test_safe_index_returns_position_when_present():
    assert safe_index(["a", "b", "c"], "b") == 1


def test_safe_index_returns_default_when_missing():
    assert safe_index(["a", "b"], "deleted", default=0) == 0


def test_build_account_labels_includes_cash_option_plus_accounts():
    accounts = pd.DataFrame(
        [{"id": 1, "owner": "夫", "name": "みずほ銀行", "kind": "bank"}]
    )
    labels = build_account_labels(accounts, {"bank": "銀行口座", "card": "クレジットカード"})
    assert labels["現金"] is None
    assert labels["夫: みずほ銀行（銀行口座）"] == 1


def test_account_select_index_finds_matching_account():
    labels = {"現金": None, "夫: A銀行": 1, "夫: B銀行": 2}
    assert account_select_index(labels, 2) == 2


def test_account_select_index_defaults_to_zero_when_not_found():
    labels = {"現金": None, "夫: A銀行": 1}
    assert account_select_index(labels, 999) == 0


def test_month_select_index_none_maps_to_zero():
    assert month_select_index(None) == 0


def test_month_select_index_returns_month_number():
    assert month_select_index(6) == 6


def test_parse_month_label_none_option_returns_none():
    assert parse_month_label("なし") is None


def test_parse_month_label_parses_month_number():
    assert parse_month_label("12月") == 12


def test_category_edit_options_keeps_original_order_when_category_still_exists():
    options, index = category_edit_options(["食費", "住居", "娯楽"], "住居")
    assert options == ["食費", "住居", "娯楽"]
    assert index == 1


def test_category_edit_options_preserves_deleted_category_instead_of_silently_switching():
    # Regression test: a plain safe_index() fallback to 0 would have silently pre-selected
    # "住居" (the first remaining category) for a transaction that was actually "旧カテゴリ",
    # so an unsuspecting user clicking 保存 would rewrite the transaction's category. The
    # deleted category must stay selected as its own option instead.
    options, index = category_edit_options(["食費", "住居"], "旧カテゴリ")
    assert options[index] == "旧カテゴリ"
    assert "旧カテゴリ" in options


def test_progress_ratio_clamps_upper_bound():
    assert progress_ratio(1_500_000, 1_200_000) == 1.0


def test_progress_ratio_clamps_negative_totals_to_zero_instead_of_raising():
    # Regression test: a NISA sale/withdrawal (see the negative-amount handling for
    # 投資(NISA) transactions in streamlit_app.py) can push a cumulative total negative.
    # st.progress() raises StreamlitAPIException for any value outside [0, 1], which would
    # crash the whole rerun - progress_ratio must clamp instead of just capping the top end.
    assert progress_ratio(-500_000, 1_200_000) == 0.0


def test_progress_ratio_normal_value_passes_through():
    assert progress_ratio(600_000, 1_200_000) == 0.5


def test_csv_safe_value_prefixes_formula_looking_strings():
    assert csv_safe_value("=cmd|'/c calc'!A1") == "'=cmd|'/c calc'!A1"
    assert csv_safe_value("+1234") == "'+1234"
    assert csv_safe_value("@SUM(A1)") == "'@SUM(A1)"


def test_csv_safe_value_leaves_normal_text_and_non_strings_untouched():
    assert csv_safe_value("スーパーで買い物") == "スーパーで買い物"
    assert csv_safe_value(None) is None
    assert csv_safe_value(1000) == 1000
