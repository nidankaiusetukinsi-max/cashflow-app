"""Rule-based cash flow health advice."""

from datetime import date

import pandas as pd

from aggregates import NetWorthSummary
from timeutil import today_jst
from db import (
    NISA_GROWTH_ANNUAL_LIMIT,
    NISA_TSUMITATE_ANNUAL_LIMIT,
    OWNERS,
    SETTING_ANNUAL_INCOME_HUSBAND,
    SETTING_ANNUAL_INCOME_WIFE,
    SETTING_GROWTH_YTD_BEFORE_HUSBAND,
    SETTING_GROWTH_YTD_BEFORE_WIFE,
    SETTING_GROWTH_YTD_BEFORE_YEAR_HUSBAND,
    SETTING_GROWTH_YTD_BEFORE_YEAR_WIFE,
    SETTING_HUSBAND_BIRTH_YEAR,
    SETTING_MORTGAGE_MONTHLY_PAYMENT,
    SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_WIFE,
    SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_YEAR_WIFE,
    SETTING_WIFE_BIRTH_YEAR,
)

# 生活防衛資金(緊急予備資金)の目安。一般的なFPの目安である「生活費の3〜6ヶ月分」を採用。
EMERGENCY_FUND_MIN_MONTHS = 3
EMERGENCY_FUND_COMFORTABLE_MONTHS = 6

# 住宅ローンの返済負担率の警戒ライン。金融機関の審査基準は額面年収ベースで30〜35%程度が
# 一般的だが、ここではより保守的に手取り月収ベースで25%を基準とする。
MORTGAGE_BURDEN_WARNING_RATIO = 0.25

# 定年前とみなす年齢、子育て世代とみなす末子年齢の上限。
PRE_RETIREMENT_AGE = 55
CHILD_REARING_MAX_AGE = 18

_YTD_SETTING_KEYS = {
    ("夫", "つみたて投資枠"): SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
    ("嫁", "つみたて投資枠"): SETTING_TSUMITATE_YTD_BEFORE_WIFE,
    ("夫", "成長投資枠"): SETTING_GROWTH_YTD_BEFORE_HUSBAND,
    ("嫁", "成長投資枠"): SETTING_GROWTH_YTD_BEFORE_WIFE,
}
_YTD_YEAR_SETTING_KEYS = {
    ("夫", "つみたて投資枠"): SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND,
    ("嫁", "つみたて投資枠"): SETTING_TSUMITATE_YTD_BEFORE_YEAR_WIFE,
    ("夫", "成長投資枠"): SETTING_GROWTH_YTD_BEFORE_YEAR_HUSBAND,
    ("嫁", "成長投資枠"): SETTING_GROWTH_YTD_BEFORE_YEAR_WIFE,
}


def year_elapsed_ratio(today: date) -> float:
    """Return how far through `today`'s calendar year has elapsed (leap-year aware)."""
    days_in_year = (date(today.year + 1, 1, 1) - date(today.year, 1, 1)).days
    return today.timetuple().tm_yday / days_in_year


def effective_nisa_ytd_before(settings: dict[str, float], owner: str, category: str, today: date) -> float:
    """The "contributed before using the app" NISA baseline, for the year it was recorded.

    This value is meant to represent contributions made earlier in ONE specific calendar
    year, before the user started tracking NISA in the app. It's tagged with the year it
    was saved for; once the calendar rolls past that year it must stop being added to
    "this year's" progress, or every future year would permanently start already at that
    old baseline.
    """
    recorded_year = settings.get(_YTD_YEAR_SETTING_KEYS[(owner, category)], 0.0)
    if int(recorded_year) != today.year:
        return 0.0
    return settings.get(_YTD_SETTING_KEYS[(owner, category)], 0.0)


def _life_stage_formation_thresholds(
    settings: dict[str, float], children: pd.DataFrame | None, today: date
) -> tuple[float, float, str]:
    """The (healthy_threshold, low_threshold, life_stage_note) for the 資産形成率 check.

    A single flat "20% is healthy" bar doesn't reflect how FPs actually advise households:
    those nearing retirement are typically told to save MORE (rebuilding the cushion they'll
    soon draw down), while those actively raising children are typically told LESS is fine
    (education costs legitimately eat into the savings rate during that window - flagging
    that dip every month isn't useful advice). Pre-retirement takes priority over
    child-rearing when both apply, since proximity to retirement is the harder constraint.
    """
    husband_birth_year = settings.get(SETTING_HUSBAND_BIRTH_YEAR, 0.0)
    wife_birth_year = settings.get(SETTING_WIFE_BIRTH_YEAR, 0.0)
    ages = [
        today.year - int(birth_year)
        for birth_year in (husband_birth_year, wife_birth_year)
        if birth_year > 0
    ]
    if any(age >= PRE_RETIREMENT_AGE for age in ages):
        return (0.25, 0.15, "定年に向けて基準を引き上げています")

    if children is not None and not children.empty:
        has_young_child = any(
            0 <= today.year - int(birth_year) < CHILD_REARING_MAX_AGE
            for birth_year in children["birth_year"]
        )
        if has_young_child:
            return (0.12, 0.05, "教育費がかかる時期のため基準を引き下げています")

    return (0.2, 0.1, "")


def _emergency_fund_advice(
    accounts_df: pd.DataFrame, net_worth: NetWorthSummary, all_transactions: pd.DataFrame, today: date
) -> str | None:
    """Warn if liquid cash can't cover EMERGENCY_FUND_MIN_MONTHS of typical spending, or
    reassure once it comfortably covers EMERGENCY_FUND_COMFORTABLE_MONTHS. Silent in between.

    This is the item FPs check first, before any investment pace: whether there's a cash
    cushion for job loss / a sudden expense, before money is pushed toward NISA. A card
    account's balance is a liability (unpaid charges), not a cushion, so only bank-kind
    accounts plus shared_cash count as liquid here.
    """
    if all_transactions.empty:
        return None
    today_ts = pd.Timestamp(today)
    span_months = max((today_ts - all_transactions["date"].min()).days, 0) / 30
    if span_months < 1:
        return None  # too little history for a meaningful average

    window_months = min(EMERGENCY_FUND_MIN_MONTHS, span_months)
    cutoff = today_ts - pd.Timedelta(days=window_months * 30)
    recent_expense = all_transactions.loc[
        (all_transactions["type"] == "expense") & (all_transactions["date"] >= cutoff), "amount"
    ].sum()
    avg_monthly_expense = recent_expense / window_months
    if avg_monthly_expense <= 0:
        return None

    bank_account_ids = accounts_df.loc[accounts_df["kind"] == "bank", "id"]
    liquid_assets = net_worth.shared_cash + sum(
        net_worth.account_balances.get(account_id, 0.0) for account_id in bank_account_ids
    )
    months_covered = liquid_assets / avg_monthly_expense

    if months_covered < EMERGENCY_FUND_MIN_MONTHS:
        return (
            f"生活防衛資金(現金クッション)が支出の{months_covered:.1f}ヶ月分しかありません"
            f"（目安は{EMERGENCY_FUND_MIN_MONTHS}〜{EMERGENCY_FUND_COMFORTABLE_MONTHS}ヶ月分）。"
            "NISA拠出のペースを上げるより先に、まず現金の確保を優先しましょう。"
        )
    if months_covered >= EMERGENCY_FUND_COMFORTABLE_MONTHS:
        return f"生活防衛資金は支出の{months_covered:.1f}ヶ月分確保できており、十分な水準です。"
    return None


def _mortgage_burden_advice(settings: dict[str, float]) -> str | None:
    """Warn if the mortgage payment looks unsustainably large relative to take-home pay.

    Lenders' own "返済負担率" guidelines are usually calculated against gross (額面) annual
    income, commonly allowing up to 30-35%. This check is deliberately stricter: it's
    calculated against take-home (手取り) income, which FPs generally treat as a more
    conservative and realistic base for what a household can actually sustain.
    """
    mortgage_monthly = settings.get(SETTING_MORTGAGE_MONTHLY_PAYMENT, 0.0)
    monthly_takehome = (
        settings.get(SETTING_ANNUAL_INCOME_HUSBAND, 0.0) + settings.get(SETTING_ANNUAL_INCOME_WIFE, 0.0)
    ) / 12
    if mortgage_monthly <= 0 or monthly_takehome <= 0:
        return None

    burden_ratio = mortgage_monthly / monthly_takehome
    if burden_ratio > MORTGAGE_BURDEN_WARNING_RATIO:
        return (
            f"住宅ローンの返済額(月¥{mortgage_monthly:,.0f})が手取り月収の{burden_ratio * 100:.0f}%を"
            f"占めています（目安は{MORTGAGE_BURDEN_WARNING_RATIO * 100:.0f}%以内。手取りベースの目安のため、"
            "金融機関の審査基準より保守的です）。"
        )
    return None


def generate_advice(
    all_transactions: pd.DataFrame,
    budgets: dict[str, float],
    settings: dict[str, float],
    accounts_df: pd.DataFrame | None = None,
    net_worth: NetWorthSummary | None = None,
    children: pd.DataFrame | None = None,
) -> list[str]:
    """Return a list of Japanese advice strings based on this month's / this year's data.

    accounts_df/net_worth/children are optional: when omitted, the checks that need them
    (emergency fund, life-stage-adjusted savings-rate thresholds) are skipped rather than
    erroring, so callers that only have transactions/budgets/settings keep working.
    """
    advice: list[str] = []
    today = today_jst()

    this_month = all_transactions[
        (all_transactions["date"].dt.year == today.year) & (all_transactions["date"].dt.month == today.month)
    ]
    this_year = all_transactions[all_transactions["date"].dt.year == today.year]

    monthly_salary = (
        settings.get(SETTING_ANNUAL_INCOME_HUSBAND, 0.0) + settings.get(SETTING_ANNUAL_INCOME_WIFE, 0.0)
    ) / 12
    income = monthly_salary + this_month.loc[this_month["type"] == "income", "amount"].sum()
    expense = this_month.loc[this_month["type"] == "expense", "amount"].sum()

    # --- 資産形成率（収入のうち支出に回らなかった割合） ---
    if income > 0:
        healthy_threshold, low_threshold, life_stage_note = _life_stage_formation_thresholds(
            settings, children, today
        )
        note_suffix = f"（{life_stage_note}）" if life_stage_note else ""
        formation_rate = (income - expense) / income
        rate_pct = formation_rate * 100
        if formation_rate >= healthy_threshold:
            advice.append(f"今月の資産形成率は{rate_pct:.0f}%で良好です。この調子を維持しましょう。{note_suffix}")
        elif formation_rate >= low_threshold:
            advice.append(
                f"今月の資産形成率は{rate_pct:.0f}%です。理想の目安は{healthy_threshold * 100:.0f}%以上なので、"
                f"あと一歩です。{note_suffix}"
            )
        else:
            advice.append(
                f"今月の資産形成率は{rate_pct:.0f}%と低めです。固定費や変動費を見直す余地がないか確認しましょう。"
                f"{note_suffix}"
            )
    elif expense > 0:
        advice.append("今月は収入の記録がありません。収入を記録すると資産形成率を計算できます。")

    # --- 生活防衛資金(緊急予備資金)チェック ---
    if accounts_df is not None and net_worth is not None:
        emergency_fund_tip = _emergency_fund_advice(accounts_df, net_worth, all_transactions, today)
        if emergency_fund_tip:
            advice.append(emergency_fund_tip)

    # --- 住宅ローンの返済負担率チェック ---
    mortgage_tip = _mortgage_burden_advice(settings)
    if mortgage_tip:
        advice.append(mortgage_tip)

    # --- カテゴリ別予算超過チェック ---
    if budgets:
        month_expense_by_category = this_month[this_month["type"] == "expense"].groupby("category")["amount"].sum()
        for category, limit in budgets.items():
            actual = month_expense_by_category.get(category, 0)
            if actual > limit:
                over = actual - limit
                advice.append(
                    f"「{category}」の今月の支出は予算を¥{over:,.0f}超過しています"
                    f"（実績¥{actual:,.0f} / 予算¥{limit:,.0f}）。"
                )

    # --- NISA消化ペース（夫婦それぞれの非課税枠ごとに判定） ---
    elapsed_ratio = year_elapsed_ratio(today)
    investment_this_year = this_year[this_year["type"] == "investment"]

    for owner in OWNERS:
        owner_investment_this_year = investment_this_year[investment_this_year["owner"] == owner]
        for category, annual_limit in (
            ("つみたて投資枠", NISA_TSUMITATE_ANNUAL_LIMIT),
            ("成長投資枠", NISA_GROWTH_ANNUAL_LIMIT),
        ):
            contributed = (
                owner_investment_this_year.loc[
                    owner_investment_this_year["category"] == category, "amount"
                ].sum()
                + effective_nisa_ytd_before(settings, owner, category, today)
            )
            if contributed == 0:
                continue
            pace_ratio = contributed / annual_limit
            if pace_ratio < elapsed_ratio - 0.1:
                remaining_months = max(12 - today.month + 1, 1)
                needed_monthly = (annual_limit - contributed) / remaining_months
                advice.append(
                    f"{owner}の{category}の消化ペースがやや遅れています"
                    f"（年間上限¥{annual_limit:,.0f}のうち¥{contributed:,.0f}拠出済み）。"
                    f"年内に使い切るには月あたり¥{needed_monthly:,.0f}程度のペースが必要です。"
                )
            elif pace_ratio >= 0.99:
                advice.append(f"{owner}の{category}は年間上限まで拠出済みです。")

    if not advice:
        advice.append("取引を記録すると、ここに家計の健全化アドバイスが表示されます。")

    return advice
