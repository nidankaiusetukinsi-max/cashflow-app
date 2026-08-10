"""Rule-based cash flow health advice."""

from datetime import date

import pandas as pd

from db import (
    NISA_GROWTH_ANNUAL_LIMIT,
    NISA_TSUMITATE_ANNUAL_LIMIT,
    OWNERS,
    SETTING_ANNUAL_INCOME,
    SETTING_GROWTH_YTD_BEFORE_HUSBAND,
    SETTING_GROWTH_YTD_BEFORE_WIFE,
    SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_WIFE,
)

_YTD_SETTING_KEYS = {
    ("夫", "つみたて投資枠"): SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
    ("嫁", "つみたて投資枠"): SETTING_TSUMITATE_YTD_BEFORE_WIFE,
    ("夫", "成長投資枠"): SETTING_GROWTH_YTD_BEFORE_HUSBAND,
    ("嫁", "成長投資枠"): SETTING_GROWTH_YTD_BEFORE_WIFE,
}


def generate_advice(
    all_transactions: pd.DataFrame,
    budgets: dict[str, float],
    settings: dict[str, float],
) -> list[str]:
    """Return a list of Japanese advice strings based on this month's / this year's data."""
    advice: list[str] = []
    today = date.today()

    this_month = all_transactions[
        (all_transactions["date"].dt.year == today.year) & (all_transactions["date"].dt.month == today.month)
    ]
    this_year = all_transactions[all_transactions["date"].dt.year == today.year]

    monthly_salary = settings.get(SETTING_ANNUAL_INCOME, 0.0) / 12
    income = monthly_salary + this_month.loc[this_month["type"] == "income", "amount"].sum()
    expense = this_month.loc[this_month["type"] == "expense", "amount"].sum()

    # --- 資産形成率（収入のうち支出に回らなかった割合） ---
    if income > 0:
        formation_rate = (income - expense) / income
        rate_pct = formation_rate * 100
        if formation_rate >= 0.2:
            advice.append(f"今月の資産形成率は{rate_pct:.0f}%で良好です。この調子を維持しましょう。")
        elif formation_rate >= 0.1:
            advice.append(f"今月の資産形成率は{rate_pct:.0f}%です。理想の目安は20%以上なので、あと一歩です。")
        else:
            advice.append(
                f"今月の資産形成率は{rate_pct:.0f}%と低めです。固定費や変動費を見直す余地がないか確認しましょう。"
            )
    elif expense > 0:
        advice.append("今月は収入の記録がありません。収入を記録すると資産形成率を計算できます。")

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
    elapsed_ratio = today.timetuple().tm_yday / 365
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
                + settings.get(_YTD_SETTING_KEYS[(owner, category)], 0.0)
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
