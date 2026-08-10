"""Personal cash flow tracker with NISA tracking and budget advice."""

from datetime import date, timedelta

import altair as alt
import pandas as pd

import streamlit as st
from advice import generate_advice
from db import (
    ACCOUNT_KINDS,
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    NISA_CATEGORIES,
    NISA_GROWTH_ANNUAL_LIMIT,
    NISA_GROWTH_LIFETIME_LIMIT,
    NISA_LIFETIME_LIMIT,
    NISA_TSUMITATE_ANNUAL_LIMIT,
    OWNERS,
    SETTING_ANNUAL_EXPENSE_TARGET,
    SETTING_ANNUAL_INCOME,
    SETTING_CHILDCARE_ANNUAL_COST,
    SETTING_CHILDCARE_END_AGE,
    SETTING_GROWTH_LIFETIME_BEFORE,
    SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND,
    SETTING_GROWTH_LIFETIME_BEFORE_WIFE,
    SETTING_GROWTH_YTD_BEFORE,
    SETTING_GROWTH_YTD_BEFORE_HUSBAND,
    SETTING_GROWTH_YTD_BEFORE_WIFE,
    SETTING_HUSBAND_BIRTH_YEAR,
    SETTING_HUSBAND_PENSION_ANNUAL,
    SETTING_HUSBAND_PENSION_START_AGE,
    SETTING_HUSBAND_RETIREMENT_AGE,
    SETTING_INFLATION_RATE,
    SETTING_INITIAL_CASH,
    SETTING_MORTGAGE_MONTHLY_PAYMENT,
    SETTING_MORTGAGE_PAYOFF_YEAR,
    SETTING_TSUMITATE_LIFETIME_BEFORE,
    SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND,
    SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE,
    SETTING_TSUMITATE_YTD_BEFORE,
    SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_WIFE,
    SETTING_WIFE_BIRTH_YEAR,
    SETTING_WIFE_PENSION_ANNUAL,
    SETTING_WIFE_PENSION_START_AGE,
    SETTING_WIFE_RETIREMENT_AGE,
    add_account,
    add_child,
    add_recurring_expense,
    add_recurring_investment,
    add_transaction,
    add_transfer,
    apply_recurring_expenses,
    apply_recurring_investments,
    delete_account,
    delete_budget,
    delete_child,
    delete_recurring_expense,
    delete_recurring_investment,
    delete_transactions,
    get_accounts,
    get_budgets,
    get_children,
    get_recurring_expenses,
    get_recurring_investments,
    get_settings,
    get_transactions,
    set_budget,
    set_setting,
)
from forecast import build_childcare_forecast, build_expense_forecast, build_life_events

st.set_page_config(
    page_title="キャッシュフロー管理",
    page_icon=":material/payments:",
    layout="wide",
)


def check_password() -> bool:
    """Show a password gate; return True once the correct password is entered."""

    def password_entered() -> None:
        if st.session_state["password_input"] == st.secrets["APP_PASSWORD"]:
            st.session_state["password_correct"] = True
            del st.session_state["password_input"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct"):
        return True

    st.text_input("パスワード", type="password", on_change=password_entered, key="password_input")
    if st.session_state.get("password_correct") is False:
        st.error("パスワードが違います。")
    return False


if not check_password():
    st.stop()


TIME_RANGES = ["1ヶ月", "6ヶ月", "1年", "今年", "すべて"]
TYPE_LABELS = {"income": "収入", "expense": "支出", "investment": "投資(NISA)", "transfer": "振替"}


def filter_by_time_range(df: pd.DataFrame, time_range: str) -> pd.DataFrame:
    if time_range == "すべて" or df.empty:
        return df

    max_date = df["date"].max()
    if time_range == "1ヶ月":
        min_date = max_date - timedelta(days=30)
    elif time_range == "6ヶ月":
        min_date = max_date - timedelta(days=180)
    elif time_range == "1年":
        min_date = max_date - timedelta(days=365)
    elif time_range == "今年":
        min_date = pd.Timestamp(date(max_date.year, 1, 1))
    else:
        return df

    filtered: pd.DataFrame = df[df["date"] >= min_date]
    return filtered


# =============================================================================
# Data
# =============================================================================

apply_recurring_expenses()
apply_recurring_investments()

all_transactions = get_transactions()
budgets = get_budgets()
settings = get_settings()
accounts_df = get_accounts()
recurring_df = get_recurring_expenses()

account_name_map: dict[int, str] = {row.id: f"{row.owner}: {row.name}" for row in accounts_df.itertuples()}

all_transactions["signed_amount"] = all_transactions["amount"].where(
    all_transactions["type"] == "income", -all_transactions["amount"]
)
net_by_account = all_transactions.dropna(subset=["account_id"]).groupby("account_id")["signed_amount"].sum()
transfer_in = (
    all_transactions[all_transactions["type"] == "transfer"].groupby("to_account_id")["amount"].sum()
)
net_by_account = net_by_account.add(transfer_in, fill_value=0)
account_balances: dict[int, float] = {
    row.id: row.initial_balance + net_by_account.get(row.id, 0.0) for row in accounts_df.itertuples()
}

untagged_net = all_transactions.loc[all_transactions["account_id"].isna(), "signed_amount"].sum()
current_balance = settings[SETTING_INITIAL_CASH] + untagged_net + sum(account_balances.values())

nisa_lifetime_total = (
    all_transactions.loc[all_transactions["type"] == "investment", "amount"].sum()
    + settings[SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND]
    + settings[SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE]
    + settings[SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND]
    + settings[SETTING_GROWTH_LIFETIME_BEFORE_WIFE]
)
total_assets = current_balance + nisa_lifetime_total


# =============================================================================
# Sidebar: new transaction form
# =============================================================================

with st.sidebar:
    st.markdown("### 取引を追加")
    with st.form("add_transaction", clear_on_submit=True):
        entry_date = st.date_input("日付", value=date.today())
        entry_type = st.segmented_control(
            "種別",
            options=["収入", "支出", "投資(NISA)"],
            default="支出",
            key="entry_type",
        )
        if entry_type == "収入":
            categories = INCOME_CATEGORIES
        elif entry_type == "投資(NISA)":
            categories = NISA_CATEGORIES
        else:
            categories = EXPENSE_CATEGORIES
        entry_category = st.selectbox("カテゴリ", options=categories)
        entry_amount = st.number_input("金額", min_value=0, step=100)
        entry_memo = st.text_input("メモ", label_visibility="collapsed", placeholder="メモ（任意）")

        entry_owner = None
        if entry_type == "投資(NISA)":
            entry_owner = st.selectbox("所有者", options=OWNERS, key="entry_owner")

        account_labels = {"現金": None}
        for row in accounts_df.itertuples():
            account_labels[f"{row.owner}: {row.name}（{ACCOUNT_KINDS[row.kind]}）"] = row.id
        entry_account_label = st.selectbox("口座/カード", options=list(account_labels.keys()))

        if st.form_submit_button("追加", type="primary"):
            type_map = {"収入": "income", "支出": "expense", "投資(NISA)": "investment"}
            add_transaction(
                date=entry_date.isoformat(),
                type_=type_map[entry_type],
                category=entry_category,
                amount=entry_amount,
                memo=entry_memo,
                account_id=account_labels[entry_account_label],
                owner=entry_owner,
            )
            st.rerun()


st.markdown("### :material/payments: キャッシュフロー管理")

tab_dashboard, tab_nisa, tab_budget, tab_accounts, tab_recurring, tab_lifeplan, tab_settings = st.tabs(
    ["ダッシュボード", "NISA積立", "予算設定", "口座・カード", "固定費", "ライフプラン", "初期設定"]
)


# =============================================================================
# Tab: ダッシュボード
# =============================================================================

with tab_dashboard:
    if all_transactions.empty:
        st.info("左のフォームから取引を追加してください。「初期設定」タブで現在の残高も登録できます。")
    else:
        st.metric("現在の残高（初期設定 + 全口座 + 未設定分の取引合計）", f"¥{current_balance:,.0f}")

        if not accounts_df.empty:
            st.markdown("**口座・カード別残高**")
            balance_cols = st.columns(min(len(accounts_df), 4) or 1)
            for i, row in enumerate(accounts_df.itertuples()):
                with balance_cols[i % len(balance_cols)]:
                    st.metric(f"{row.owner}: {row.name}", f"¥{account_balances[row.id]:,.0f}")

        time_range = st.segmented_control(
            "期間", options=TIME_RANGES, default="すべて", key="dashboard_time_range"
        )
        filtered = filter_by_time_range(all_transactions, time_range or "すべて")

        total_income = filtered.loc[filtered["type"] == "income", "amount"].sum()
        total_expense = filtered.loc[filtered["type"] == "expense", "amount"].sum()
        total_investment = filtered.loc[filtered["type"] == "investment", "amount"].sum()

        kpi_cols = st.columns(3)
        with kpi_cols[0]:
            st.metric("収入合計（期間内）", f"¥{total_income:,.0f}")
        with kpi_cols[1]:
            st.metric("支出合計（期間内）", f"¥{total_expense:,.0f}")
        with kpi_cols[2]:
            st.metric("NISA拠出額（期間内）", f"¥{total_investment:,.0f}")

        if total_assets > 0:
            st.markdown("### 全資産に対する支出割合")
            asset_ratio_pct = total_expense / total_assets * 100
            st.metric(
                "期間内の支出が総資産に占める割合",
                f"{asset_ratio_pct:.1f}%",
                help=(
                    f"総資産 ¥{total_assets:,.0f}（現金・預金残高 + NISA拠出累計額）に対する、"
                    f"期間内の支出合計 ¥{total_expense:,.0f} の割合です。"
                ),
            )
            by_category_pct = (
                filtered[filtered["type"] == "expense"].groupby("category", as_index=False)["amount"].sum()
            )
            if not by_category_pct.empty:
                by_category_pct["割合"] = by_category_pct["amount"] / total_assets * 100
                by_category_pct = by_category_pct.sort_values("割合", ascending=False)
                pct_chart = (
                    alt.Chart(by_category_pct)
                    .mark_bar()
                    .encode(
                        x=alt.X("割合:Q", title="総資産に対する割合(%)"),
                        y=alt.Y("category:N", title=None, sort="-x"),
                        tooltip=[
                            alt.Tooltip("category:N", title="カテゴリ"),
                            alt.Tooltip("amount:Q", title="金額", format=",.0f"),
                            alt.Tooltip("割合:Q", title="総資産比(%)", format=".1f"),
                        ],
                    )
                    .properties(height=250)
                )
                with st.container(border=True):
                    st.markdown("**カテゴリ別支出の総資産に対する割合**")
                    st.altair_chart(pct_chart)

        st.markdown("### 健全化アドバイス")
        for tip in generate_advice(all_transactions, budgets, settings):
            st.info(tip, icon=":material/lightbulb:")

        expense_target = settings[SETTING_ANNUAL_EXPENSE_TARGET]
        if expense_target > 0:
            st.markdown("### 年間支出目標との比較")
            this_year_num = date.today().year
            elapsed_ratio = date.today().timetuple().tm_yday / 365
            this_year_all = all_transactions[all_transactions["date"].dt.year == this_year_num]

            expense_ytd = this_year_all.loc[this_year_all["type"] == "expense", "amount"].sum()
            expense_pace = expense_target * elapsed_ratio
            st.metric(
                "今年の支出実績",
                f"¥{expense_ytd:,.0f}",
                delta=f"¥{expense_ytd - expense_pace:,.0f}（対目標ペース）",
                delta_color="inverse",
                help=f"年間目標 ¥{expense_target:,.0f} に対し、経過{elapsed_ratio * 100:.0f}%時点の目標ペースは ¥{expense_pace:,.0f}",
            )

            year_start = pd.Timestamp(date(this_year_num, 1, 1))
            year_end = pd.Timestamp(date(this_year_num, 12, 31))
            type_df = this_year_all[this_year_all["type"] == "expense"]
            actual = type_df.groupby("date", as_index=False)["amount"].sum().sort_values("date")
            actual["累計"] = actual["amount"].cumsum()
            actual_series = pd.DataFrame({"日付": actual["date"], "系列": "実績", "金額": actual["累計"]})
            target_series = pd.DataFrame(
                {"日付": [year_start, year_end], "系列": "目標ペース", "金額": [0, expense_target]}
            )
            combined = pd.concat([actual_series, target_series], ignore_index=True)
            expense_target_chart = (
                alt.Chart(combined)
                .mark_line()
                .encode(
                    x=alt.X("日付:T", title=None),
                    y=alt.Y("金額:Q", title=None),
                    color=alt.Color("系列:N", title=None, legend=alt.Legend(orient="bottom")),
                    strokeDash=alt.condition(
                        alt.datum.系列 == "目標ペース", alt.value([5, 5]), alt.value([0])
                    ),
                    tooltip=[
                        alt.Tooltip("日付:T", title="日付", format="%Y-%m-%d"),
                        alt.Tooltip("系列:N", title="系列"),
                        alt.Tooltip("金額:Q", title="金額", format=",.0f"),
                    ],
                )
                .properties(height=300)
            )
            with st.container(border=True):
                st.markdown("**支出: 実績 vs 目標ペース**")
                st.altair_chart(expense_target_chart)

        chart_cols = st.columns(2)

        with chart_cols[0]:
            with st.container(border=True):
                st.markdown("**期間内の収支推移（累計）**")
                cash_flow = filtered[filtered["type"] != "investment"].copy()
                cash_flow["signed"] = cash_flow["amount"].where(
                    cash_flow["type"] == "income", -cash_flow["amount"]
                )
                daily = cash_flow.groupby("date", as_index=False)["signed"].sum().sort_values("date")
                daily["累計収支"] = daily["signed"].cumsum()
                chart = (
                    alt.Chart(daily)
                    .mark_line(point=True)
                    .encode(
                        x=alt.X("date:T", title=None),
                        y=alt.Y("累計収支:Q", title=None),
                        tooltip=[
                            alt.Tooltip("date:T", title="日付", format="%Y-%m-%d"),
                            alt.Tooltip("累計収支:Q", title="累計収支", format=",.0f"),
                        ],
                    )
                    .properties(height=300)
                )
                st.altair_chart(chart)

        with chart_cols[1]:
            with st.container(border=True):
                st.markdown("**カテゴリ別支出**")
                by_category = (
                    filtered[filtered["type"] == "expense"]
                    .groupby("category", as_index=False)["amount"]
                    .sum()
                    .sort_values("amount", ascending=False)
                )
                if by_category.empty:
                    st.info("この期間の支出データはありません。")
                else:
                    chart = (
                        alt.Chart(by_category)
                        .mark_bar()
                        .encode(
                            x=alt.X("amount:Q", title=None),
                            y=alt.Y("category:N", title=None, sort="-x"),
                            tooltip=[
                                alt.Tooltip("category:N", title="カテゴリ"),
                                alt.Tooltip("amount:Q", title="金額", format=",.0f"),
                            ],
                        )
                        .properties(height=300)
                    )
                    st.altair_chart(chart)

        st.markdown("### 取引履歴")

        display_df = all_transactions.copy()
        display_df["type"] = display_df["type"].map(TYPE_LABELS)
        display_df["account"] = display_df["account_id"].map(account_name_map).fillna("現金")
        is_transfer = all_transactions["type"] == "transfer"
        display_df.loc[is_transfer, "account"] = (
            all_transactions.loc[is_transfer, "account_id"].map(account_name_map)
            + " → "
            + all_transactions.loc[is_transfer, "to_account_id"].map(account_name_map)
        )

        event = st.dataframe(
            display_df,
            column_config={
                "id": None,
                "account_id": None,
                "to_account_id": None,
                "signed_amount": None,
                "date": st.column_config.DateColumn("日付"),
                "type": st.column_config.TextColumn("種別"),
                "category": st.column_config.TextColumn("カテゴリ"),
                "amount": st.column_config.NumberColumn("金額", format="¥%.0f"),
                "memo": st.column_config.TextColumn("メモ"),
                "account": st.column_config.TextColumn("口座/カード"),
            },
            hide_index=True,
            on_select="rerun",
            selection_mode="multi-row",
            key="transactions_table",
        )

        selected_rows = event.selection.rows
        if selected_rows:
            selected_ids = display_df.iloc[selected_rows]["id"].tolist()
            if st.button(f":material/delete: 選択した{len(selected_ids)}件を削除", type="tertiary"):
                delete_transactions(selected_ids)
                st.rerun()


# =============================================================================
# Tab: NISA積立
# =============================================================================

with tab_nisa:
    st.caption(
        "新NISA制度は簿価残高方式（売却すると翌年に枠が復活）ですが、"
        "ここでは単純に拠出累計額で消化率を近似表示しています。非課税枠は夫婦一人ずつに割り当てられるため、"
        "所有者ごとに分けて管理します。「初期設定」タブで登録した既存の拠出額も合算されます。"
    )

    investment_df = all_transactions[all_transactions["type"] == "investment"]
    this_year = date.today().year
    investment_this_year = investment_df[investment_df["date"].dt.year == this_year]

    owner_ytd_keys = {
        ("夫", "つみたて投資枠"): SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
        ("嫁", "つみたて投資枠"): SETTING_TSUMITATE_YTD_BEFORE_WIFE,
        ("夫", "成長投資枠"): SETTING_GROWTH_YTD_BEFORE_HUSBAND,
        ("嫁", "成長投資枠"): SETTING_GROWTH_YTD_BEFORE_WIFE,
    }
    owner_lifetime_keys = {
        ("夫", "つみたて投資枠"): SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND,
        ("嫁", "つみたて投資枠"): SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE,
        ("夫", "成長投資枠"): SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND,
        ("嫁", "成長投資枠"): SETTING_GROWTH_LIFETIME_BEFORE_WIFE,
    }

    for owner in OWNERS:
        st.markdown(f"### {owner}のNISA")
        owner_investment_df = investment_df[investment_df["owner"] == owner]
        owner_investment_this_year = investment_this_year[investment_this_year["owner"] == owner]

        tsumitate_this_year = (
            owner_investment_this_year.loc[
                owner_investment_this_year["category"] == "つみたて投資枠", "amount"
            ].sum()
            + settings[owner_ytd_keys[(owner, "つみたて投資枠")]]
        )
        growth_this_year = (
            owner_investment_this_year.loc[owner_investment_this_year["category"] == "成長投資枠", "amount"].sum()
            + settings[owner_ytd_keys[(owner, "成長投資枠")]]
        )
        tsumitate_lifetime = (
            owner_investment_df.loc[owner_investment_df["category"] == "つみたて投資枠", "amount"].sum()
            + settings[owner_lifetime_keys[(owner, "つみたて投資枠")]]
        )
        growth_lifetime = (
            owner_investment_df.loc[owner_investment_df["category"] == "成長投資枠", "amount"].sum()
            + settings[owner_lifetime_keys[(owner, "成長投資枠")]]
        )

        annual_cols = st.columns(2)
        with annual_cols[0]:
            with st.container(border=True):
                st.markdown("**つみたて投資枠（年間）**")
                ratio = min(tsumitate_this_year / NISA_TSUMITATE_ANNUAL_LIMIT, 1.0)
                st.progress(ratio, text=f"¥{tsumitate_this_year:,.0f} / ¥{NISA_TSUMITATE_ANNUAL_LIMIT:,.0f}")
        with annual_cols[1]:
            with st.container(border=True):
                st.markdown("**成長投資枠（年間）**")
                ratio = min(growth_this_year / NISA_GROWTH_ANNUAL_LIMIT, 1.0)
                st.progress(ratio, text=f"¥{growth_this_year:,.0f} / ¥{NISA_GROWTH_ANNUAL_LIMIT:,.0f}")

        lifetime_cols = st.columns(2)
        with lifetime_cols[0]:
            with st.container(border=True):
                st.markdown("**成長投資枠（生涯上限あり）**")
                ratio = min(growth_lifetime / NISA_GROWTH_LIFETIME_LIMIT, 1.0)
                st.progress(ratio, text=f"¥{growth_lifetime:,.0f} / ¥{NISA_GROWTH_LIFETIME_LIMIT:,.0f}")
        with lifetime_cols[1]:
            with st.container(border=True):
                st.markdown("**生涯投資枠合計**")
                total_lifetime = tsumitate_lifetime + growth_lifetime
                ratio = min(total_lifetime / NISA_LIFETIME_LIMIT, 1.0)
                st.progress(ratio, text=f"¥{total_lifetime:,.0f} / ¥{NISA_LIFETIME_LIMIT:,.0f}")

    if investment_df["owner"].isna().any():
        st.caption(
            "所有者が未設定の積立データがあります（この機能を追加する前に記録した取引など）。"
            "上記の進捗には反映されないため、必要であれば取引履歴から記録し直してください。"
        )

    st.markdown("---")
    st.markdown("#### 定期積立の設定")
    st.caption(
        "毎月自動的にNISA拠出を記録したい場合はここから登録できます。"
        "指定日を過ぎてからアプリを開くと、その月の分が自動的に積立として記録されます。"
    )
    with st.form("add_recurring_investment", clear_on_submit=True):
        ri_cols = st.columns([1, 1, 1, 2, 1])
        with ri_cols[0]:
            ri_owner = st.selectbox("所有者", options=OWNERS, key="ri_owner")
        with ri_cols[1]:
            ri_category = st.selectbox("枠", options=NISA_CATEGORIES, key="ri_category")
        with ri_cols[2]:
            ri_amount = st.number_input("金額", min_value=0, step=1000, key="ri_amount")
        with ri_cols[3]:
            ri_account_labels = {"現金": None}
            for row in accounts_df.itertuples():
                ri_account_labels[f"{row.owner}: {row.name}（{ACCOUNT_KINDS[row.kind]}）"] = row.id
            ri_account_label = st.selectbox(
                "引き落とし口座/カード", options=list(ri_account_labels.keys()), key="ri_account"
            )
        with ri_cols[4]:
            ri_day = st.number_input("積立日", min_value=1, max_value=28, value=27, step=1, key="ri_day")

        if st.form_submit_button("登録", type="primary"):
            add_recurring_investment(
                ri_owner, ri_category, ri_amount, ri_account_labels[ri_account_label], int(ri_day)
            )
            st.rerun()

    recurring_investments_df = get_recurring_investments()
    if recurring_investments_df.empty:
        st.info("まだ定期積立が登録されていません。")
    else:
        for row in recurring_investments_df.itertuples():
            row_cols = st.columns([1, 1, 1, 2, 1])
            with row_cols[0]:
                st.write(row.owner)
            with row_cols[1]:
                st.write(row.category)
            with row_cols[2]:
                st.write(f"¥{row.amount:,.0f}")
            with row_cols[3]:
                account_label = account_name_map.get(row.account_id, "現金")
                st.write(f"{account_label}（毎月{row.day_of_month}日）")
            with row_cols[4]:
                if st.button(":material/delete:", key=f"del_recurring_investment_{row.id}", type="tertiary"):
                    delete_recurring_investment(row.id)
                    st.rerun()

    st.markdown("#### 積立履歴")
    if investment_df.empty:
        st.info("NISAの積立を記録すると、ここに履歴が表示されます。")
    else:
        display_investment_df = investment_df.copy()
        display_investment_df["owner"] = display_investment_df["owner"].fillna("未設定")
        st.dataframe(
            display_investment_df[["date", "owner", "category", "amount", "memo"]],
            column_config={
                "date": st.column_config.DateColumn("日付"),
                "owner": st.column_config.TextColumn("所有者"),
                "category": st.column_config.TextColumn("枠"),
                "amount": st.column_config.NumberColumn("金額", format="¥%.0f"),
                "memo": st.column_config.TextColumn("メモ"),
            },
            hide_index=True,
        )


# =============================================================================
# Tab: 予算設定
# =============================================================================

with tab_budget:
    st.markdown("#### カテゴリ別の月予算を設定")
    with st.form("set_budget", clear_on_submit=True):
        budget_cols = st.columns([2, 2, 1])
        with budget_cols[0]:
            budget_category = st.selectbox("カテゴリ", options=EXPENSE_CATEGORIES)
        with budget_cols[1]:
            budget_amount = st.number_input("月予算", min_value=0, step=1000)
        with budget_cols[2]:
            st.markdown("&nbsp;")
            if st.form_submit_button("設定", type="primary"):
                set_budget(budget_category, budget_amount)
                st.rerun()

    st.markdown("#### 設定済みの予算")
    if not budgets:
        st.info("まだ予算が設定されていません。")
    else:
        for category, limit in budgets.items():
            row_cols = st.columns([2, 2, 1])
            with row_cols[0]:
                st.write(category)
            with row_cols[1]:
                st.write(f"¥{limit:,.0f} / 月")
            with row_cols[2]:
                if st.button(":material/delete:", key=f"del_budget_{category}", type="tertiary"):
                    delete_budget(category)
                    st.rerun()


# =============================================================================
# Tab: 初期設定
# =============================================================================

with tab_settings:
    st.caption(
        "アプリを使い始める前からの残高・NISA拠出額をここで登録すると、"
        "ダッシュボードの残高やNISA消化率に反映されます。後から何度でも修正できます。"
    )

    with st.form("initial_settings"):
        st.markdown("##### 現在の現金・預金残高")
        initial_cash = st.number_input(
            "現在の現金・預金残高",
            min_value=0,
            step=1000,
            value=int(settings[SETTING_INITIAL_CASH]),
            label_visibility="collapsed",
        )

        st.markdown("##### NISA拠出額（夫婦それぞれ）")
        st.caption("非課税枠は一人ずつに割り当てられるため、夫・嫁それぞれの拠出額を分けて入力してください。")

        nisa_owner_keys = {
            "夫": (
                SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
                SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND,
                SETTING_GROWTH_YTD_BEFORE_HUSBAND,
                SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND,
            ),
            "嫁": (
                SETTING_TSUMITATE_YTD_BEFORE_WIFE,
                SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE,
                SETTING_GROWTH_YTD_BEFORE_WIFE,
                SETTING_GROWTH_LIFETIME_BEFORE_WIFE,
            ),
        }
        # 所有者別に分ける前の合算値が残っていれば、夫の欄に初期値として引き継ぐ（データ消失防止）。
        legacy_nisa_defaults = {
            "夫": (
                int(settings[SETTING_TSUMITATE_YTD_BEFORE]),
                int(settings[SETTING_TSUMITATE_LIFETIME_BEFORE]),
                int(settings[SETTING_GROWTH_YTD_BEFORE]),
                int(settings[SETTING_GROWTH_LIFETIME_BEFORE]),
            ),
            "嫁": (0, 0, 0, 0),
        }

        nisa_owner_inputs: dict[str, tuple[int, int, int, int]] = {}
        for nisa_owner in OWNERS:
            ytd_t_key, life_t_key, ytd_g_key, life_g_key = nisa_owner_keys[nisa_owner]
            legacy_ytd_t, legacy_life_t, legacy_ytd_g, legacy_life_g = legacy_nisa_defaults[nisa_owner]
            st.markdown(f"**{nisa_owner}**")
            o_cols = st.columns(4)
            with o_cols[0]:
                o_ytd_t = st.number_input(
                    "つみたて: 今年の拠出額",
                    min_value=0,
                    step=1000,
                    value=int(settings[ytd_t_key]) or legacy_ytd_t,
                    key=f"nisa_{nisa_owner}_ytd_t",
                )
            with o_cols[1]:
                o_life_t = st.number_input(
                    "つみたて: 生涯拠出累計額",
                    min_value=0,
                    step=1000,
                    value=int(settings[life_t_key]) or legacy_life_t,
                    key=f"nisa_{nisa_owner}_life_t",
                )
            with o_cols[2]:
                o_ytd_g = st.number_input(
                    "成長: 今年の拠出額",
                    min_value=0,
                    step=1000,
                    value=int(settings[ytd_g_key]) or legacy_ytd_g,
                    key=f"nisa_{nisa_owner}_ytd_g",
                )
            with o_cols[3]:
                o_life_g = st.number_input(
                    "成長: 生涯拠出累計額",
                    min_value=0,
                    step=1000,
                    value=int(settings[life_g_key]) or legacy_life_g,
                    key=f"nisa_{nisa_owner}_life_g",
                )
            nisa_owner_inputs[nisa_owner] = (o_ytd_t, o_life_t, o_ytd_g, o_life_g)

        st.markdown("##### 年収・支出設定")
        target_cols_input = st.columns(2)
        with target_cols_input[0]:
            annual_income = st.number_input(
                "手取り年収",
                min_value=0,
                step=10000,
                value=int(settings[SETTING_ANNUAL_INCOME]),
                help="毎月の給与を記録しなくても、この設定値（÷12）が月々の収入実績として健全化アドバイスの計算に自動的に使われます。",
            )
        with target_cols_input[1]:
            annual_expense_target = st.number_input(
                "年間目標支出",
                min_value=0,
                step=10000,
                value=int(settings[SETTING_ANNUAL_EXPENSE_TARGET]),
                help="ダッシュボードで実績とのズレをグラフ表示するための目標値です。",
            )

        if st.form_submit_button("保存", type="primary"):
            set_setting(SETTING_INITIAL_CASH, initial_cash)
            for nisa_owner, (o_ytd_t, o_life_t, o_ytd_g, o_life_g) in nisa_owner_inputs.items():
                ytd_t_key, life_t_key, ytd_g_key, life_g_key = nisa_owner_keys[nisa_owner]
                set_setting(ytd_t_key, o_ytd_t)
                set_setting(life_t_key, o_life_t)
                set_setting(ytd_g_key, o_ytd_g)
                set_setting(life_g_key, o_life_g)
            set_setting(SETTING_ANNUAL_INCOME, annual_income)
            set_setting(SETTING_ANNUAL_EXPENSE_TARGET, annual_expense_target)
            st.toast("初期設定を保存しました。", icon=":material/check_circle:")
            st.rerun()


# =============================================================================
# Tab: 口座・カード
# =============================================================================

with tab_accounts:
    st.caption("夫婦それぞれの銀行口座・クレジットカードを登録すると、取引ごとにどれを使ったか記録でき、口座/カード別の残高も確認できます。")

    with st.form("add_account", clear_on_submit=True):
        acc_cols = st.columns([1, 1, 2, 1])
        with acc_cols[0]:
            acc_owner = st.selectbox("所有者", options=OWNERS)
        with acc_cols[1]:
            acc_kind_label = st.selectbox("種類", options=list(ACCOUNT_KINDS.values()))
        with acc_cols[2]:
            acc_name = st.text_input("名前", placeholder="例: みずほ銀行、楽天カード")
        with acc_cols[3]:
            acc_initial = st.number_input("初期残高", step=1000)

        if st.form_submit_button("追加", type="primary"):
            kind_key = {label: key for key, label in ACCOUNT_KINDS.items()}[acc_kind_label]
            if acc_name.strip():
                add_account(acc_owner, acc_name.strip(), kind_key, acc_initial)
                st.rerun()
            else:
                st.error("名前を入力してください。")

    st.markdown("#### 登録済みの口座・カード")
    if accounts_df.empty:
        st.info("まだ口座・カードが登録されていません。")
    else:
        for owner in OWNERS:
            owner_accounts = accounts_df[accounts_df["owner"] == owner]
            if owner_accounts.empty:
                continue
            st.markdown(f"**{owner}**")
            for row in owner_accounts.itertuples():
                row_cols = st.columns([2, 1, 1, 1])
                with row_cols[0]:
                    st.write(f"{row.name}（{ACCOUNT_KINDS[row.kind]}）")
                with row_cols[1]:
                    st.write(f"¥{account_balances[row.id]:,.0f}")
                with row_cols[2]:
                    st.write("")
                with row_cols[3]:
                    if st.button(":material/delete:", key=f"del_account_{row.id}", type="tertiary"):
                        delete_account(row.id)
                        st.rerun()

    if len(accounts_df) >= 2:
        st.markdown("#### 口座間の振替")
        st.caption("現金を口座間で移動した場合はここから記録します。振替元の残高が減り、振替先の残高が増えます。")
        with st.form("add_transfer", clear_on_submit=True):
            transfer_cols = st.columns([1, 2, 2, 2, 2])
            account_choices = [f"{row.owner}: {row.name}" for row in accounts_df.itertuples()]
            with transfer_cols[0]:
                transfer_date = st.date_input("日付", value=date.today(), key="transfer_date")
            with transfer_cols[1]:
                transfer_from_label = st.selectbox("振替元", options=account_choices, key="transfer_from")
            with transfer_cols[2]:
                transfer_to_label = st.selectbox("振替先", options=account_choices, key="transfer_to")
            with transfer_cols[3]:
                transfer_amount = st.number_input("金額", min_value=0, step=1000, key="transfer_amount")
            with transfer_cols[4]:
                transfer_memo = st.text_input(
                    "メモ", placeholder="メモ（任意）", key="transfer_memo"
                )

            if st.form_submit_button("振替を記録", type="primary"):
                account_id_by_label = {f"{row.owner}: {row.name}": row.id for row in accounts_df.itertuples()}
                from_id = account_id_by_label[transfer_from_label]
                to_id = account_id_by_label[transfer_to_label]
                if from_id == to_id:
                    st.error("振替元と振替先は異なる口座を選んでください。")
                else:
                    add_transfer(
                        date=transfer_date.isoformat(),
                        from_account_id=from_id,
                        to_account_id=to_id,
                        amount=transfer_amount,
                        memo=transfer_memo,
                    )
                    st.rerun()
    else:
        st.info("口座を2つ以上登録すると、口座間の振替を記録できます。")


# =============================================================================
# Tab: 固定費
# =============================================================================

with tab_recurring:
    st.caption(
        "家賃やサブスクなど毎月決まった日に発生する固定費を登録すると、"
        "その日を過ぎてからアプリを開いたタイミングで自動的に支出として記録され、指定した口座/カードの残高にも反映されます。"
    )

    with st.form("add_recurring", clear_on_submit=True):
        rec_cols = st.columns([2, 1, 1, 2, 1])
        with rec_cols[0]:
            rec_name = st.text_input("名称", placeholder="例: 家賃、サブスク")
        with rec_cols[1]:
            rec_category = st.selectbox("カテゴリ", options=EXPENSE_CATEGORIES, key="rec_category")
        with rec_cols[2]:
            rec_amount = st.number_input("金額", min_value=0, step=100, key="rec_amount")
        with rec_cols[3]:
            rec_account_labels = {"現金": None}
            for row in accounts_df.itertuples():
                rec_account_labels[f"{row.owner}: {row.name}（{ACCOUNT_KINDS[row.kind]}）"] = row.id
            rec_account_label = st.selectbox(
                "引き落とし口座/カード", options=list(rec_account_labels.keys()), key="rec_account"
            )
        with rec_cols[4]:
            rec_day = st.number_input("引き落とし日", min_value=1, max_value=28, value=27, step=1, key="rec_day")

        if st.form_submit_button("登録", type="primary"):
            if rec_name.strip():
                add_recurring_expense(
                    rec_name.strip(),
                    rec_category,
                    rec_amount,
                    rec_account_labels[rec_account_label],
                    int(rec_day),
                )
                st.rerun()
            else:
                st.error("名称を入力してください。")

    st.markdown("#### 登録済みの固定費")
    if recurring_df.empty:
        st.info("まだ固定費が登録されていません。")
    else:
        for row in recurring_df.itertuples():
            row_cols = st.columns([2, 1, 1, 2, 1])
            with row_cols[0]:
                st.write(row.name)
            with row_cols[1]:
                st.write(row.category)
            with row_cols[2]:
                st.write(f"¥{row.amount:,.0f}")
            with row_cols[3]:
                account_label = account_name_map.get(row.account_id, "現金")
                st.write(f"{account_label}（毎月{row.day_of_month}日）")
            with row_cols[4]:
                if st.button(":material/delete:", key=f"del_recurring_{row.id}", type="tertiary"):
                    delete_recurring_expense(row.id)
                    st.rerun()


# =============================================================================
# Tab: ライフプラン
# =============================================================================

with tab_lifeplan:
    st.caption(
        "物価上昇率・住宅ローン・定年・年金・育児費用を設定すると、将来の支出や年金収入の見通しを概算でグラフ表示します。"
        "あくまで簡易なシミュレーションです。"
    )

    with st.form("lifeplan_settings"):
        st.markdown("##### 基本情報")
        base_cols = st.columns(3)
        with base_cols[0]:
            husband_birth_year_setting = settings[SETTING_HUSBAND_BIRTH_YEAR]
            husband_age_default = (
                int(date.today().year - husband_birth_year_setting) if husband_birth_year_setting > 0 else 35
            )
            husband_age = st.number_input(
                "夫の現在の年齢", min_value=0, max_value=120, step=1, value=husband_age_default
            )
        with base_cols[1]:
            wife_birth_year_setting = settings[SETTING_WIFE_BIRTH_YEAR]
            wife_age_default = (
                int(date.today().year - wife_birth_year_setting) if wife_birth_year_setting > 0 else 35
            )
            wife_age = st.number_input(
                "嫁の現在の年齢", min_value=0, max_value=120, step=1, value=wife_age_default
            )
        with base_cols[2]:
            inflation_rate = st.number_input(
                "物価上昇率（年率 %）",
                min_value=0.0,
                max_value=20.0,
                step=0.1,
                value=float(settings[SETTING_INFLATION_RATE]),
            )

        st.markdown("##### 住宅ローン")
        mortgage_cols = st.columns(2)
        with mortgage_cols[0]:
            mortgage_monthly = st.number_input(
                "月々の返済額",
                min_value=0,
                step=1000,
                value=int(settings[SETTING_MORTGAGE_MONTHLY_PAYMENT]),
            )
        with mortgage_cols[1]:
            mortgage_payoff_year = st.number_input(
                "完済年（西暦）",
                min_value=0,
                max_value=2200,
                step=1,
                value=int(settings[SETTING_MORTGAGE_PAYOFF_YEAR]),
                help="0のままにすると住宅ローンなしとして扱われます。",
            )

        st.markdown("##### 定年")
        retire_cols = st.columns(2)
        with retire_cols[0]:
            husband_retirement_age = st.number_input(
                "夫の定年年齢",
                min_value=0,
                max_value=100,
                step=1,
                value=int(settings[SETTING_HUSBAND_RETIREMENT_AGE]) or 65,
            )
        with retire_cols[1]:
            wife_retirement_age = st.number_input(
                "嫁の定年年齢",
                min_value=0,
                max_value=100,
                step=1,
                value=int(settings[SETTING_WIFE_RETIREMENT_AGE]) or 65,
            )

        st.markdown("##### 年金")
        pension_cols = st.columns(4)
        with pension_cols[0]:
            husband_pension_start_age = st.number_input(
                "夫の受給開始年齢",
                min_value=0,
                max_value=100,
                step=1,
                value=int(settings[SETTING_HUSBAND_PENSION_START_AGE]) or 65,
            )
        with pension_cols[1]:
            husband_pension_annual = st.number_input(
                "夫の年間受給見込み額",
                min_value=0,
                step=10000,
                value=int(settings[SETTING_HUSBAND_PENSION_ANNUAL]),
            )
        with pension_cols[2]:
            wife_pension_start_age = st.number_input(
                "嫁の受給開始年齢",
                min_value=0,
                max_value=100,
                step=1,
                value=int(settings[SETTING_WIFE_PENSION_START_AGE]) or 65,
            )
        with pension_cols[3]:
            wife_pension_annual = st.number_input(
                "嫁の年間受給見込み額",
                min_value=0,
                step=10000,
                value=int(settings[SETTING_WIFE_PENSION_ANNUAL]),
            )

        st.markdown("##### 育児費用の目安")
        st.caption(
            "子育て費用（教育費込み）の全国平均・中央値は各種調査で年齢により年間およそ50万〜200万円程度とされています。"
            "実態に合わせて調整してください。"
        )
        childcare_cols = st.columns(2)
        with childcare_cols[0]:
            childcare_annual_cost = st.number_input(
                "子ども1人あたりの年間費用目安",
                min_value=0,
                step=10000,
                value=int(settings[SETTING_CHILDCARE_ANNUAL_COST]) or 1_000_000,
            )
        with childcare_cols[1]:
            childcare_end_age = st.number_input(
                "費用がかかる年齢の上限（目安: 大学卒業=22歳）",
                min_value=0,
                max_value=30,
                step=1,
                value=int(settings[SETTING_CHILDCARE_END_AGE]) or 22,
            )

        if st.form_submit_button("ライフプラン設定を保存", type="primary"):
            set_setting(SETTING_HUSBAND_BIRTH_YEAR, date.today().year - husband_age)
            set_setting(SETTING_WIFE_BIRTH_YEAR, date.today().year - wife_age)
            set_setting(SETTING_INFLATION_RATE, inflation_rate)
            set_setting(SETTING_MORTGAGE_MONTHLY_PAYMENT, mortgage_monthly)
            set_setting(SETTING_MORTGAGE_PAYOFF_YEAR, mortgage_payoff_year)
            set_setting(SETTING_HUSBAND_RETIREMENT_AGE, husband_retirement_age)
            set_setting(SETTING_WIFE_RETIREMENT_AGE, wife_retirement_age)
            set_setting(SETTING_HUSBAND_PENSION_START_AGE, husband_pension_start_age)
            set_setting(SETTING_HUSBAND_PENSION_ANNUAL, husband_pension_annual)
            set_setting(SETTING_WIFE_PENSION_START_AGE, wife_pension_start_age)
            set_setting(SETTING_WIFE_PENSION_ANNUAL, wife_pension_annual)
            set_setting(SETTING_CHILDCARE_ANNUAL_COST, childcare_annual_cost)
            set_setting(SETTING_CHILDCARE_END_AGE, childcare_end_age)
            st.toast("ライフプラン設定を保存しました。", icon=":material/check_circle:")
            st.rerun()

    st.markdown("##### 子どもの登録")
    with st.form("add_child", clear_on_submit=True):
        child_cols = st.columns([2, 1, 1])
        with child_cols[0]:
            child_name = st.text_input("名前（任意）", placeholder="例: 長男")
        with child_cols[1]:
            child_age = st.number_input("現在の年齢", min_value=0, max_value=30, step=1, key="child_age")
        with child_cols[2]:
            st.markdown("&nbsp;")
            if st.form_submit_button("追加", type="primary"):
                add_child(child_name.strip() or None, date.today().year - child_age)
                st.rerun()

    children_df = get_children()
    if not children_df.empty:
        for row in children_df.itertuples():
            row_cols = st.columns([2, 1, 1])
            with row_cols[0]:
                st.write(row.name if pd.notna(row.name) else "(名前未設定)")
            with row_cols[1]:
                st.write(f"{date.today().year - row.birth_year}歳")
            with row_cols[2]:
                if st.button(":material/delete:", key=f"del_child_{row.id}", type="tertiary"):
                    delete_child(row.id)
                    st.rerun()

    st.markdown("---")
    st.markdown("#### 将来の支出・年金収入シミュレーション")

    if settings[SETTING_ANNUAL_EXPENSE_TARGET] > 0:
        base_annual_expense = settings[SETTING_ANNUAL_EXPENSE_TARGET]
    else:
        recent_cutoff = pd.Timestamp(date.today()) - pd.Timedelta(days=365)
        base_annual_expense = all_transactions.loc[
            (all_transactions["type"] == "expense") & (all_transactions["date"] >= recent_cutoff), "amount"
        ].sum()

    if base_annual_expense <= 0:
        st.info("支出データ、または「初期設定」タブの年間目標支出を登録すると、将来の支出シミュレーションが表示されます。")
    else:
        forecast_df = build_expense_forecast(base_annual_expense, settings)
        events = build_life_events(settings)

        st.caption(
            f"現在の年間支出（直近1年の実績、未登録の場合は年間目標支出）¥{base_annual_expense:,.0f} を基準に、"
            "物価上昇率で将来の金額に換算しています。住宅ローンは完済年より後、月々の返済額×12を差し引いています。"
        )

        long_df = forecast_df.melt(
            id_vars=["year"],
            value_vars=["projected_expense", "pension_income"],
            var_name="系列",
            value_name="金額",
        )
        long_df["系列"] = long_df["系列"].map({"projected_expense": "予測支出", "pension_income": "年金収入"})

        base_chart = (
            alt.Chart(long_df)
            .mark_line()
            .encode(
                x=alt.X("year:Q", title="年", axis=alt.Axis(format="d")),
                y=alt.Y("金額:Q", title=None),
                color=alt.Color("系列:N", title=None, legend=alt.Legend(orient="bottom")),
                tooltip=[
                    alt.Tooltip("year:Q", title="年", format="d"),
                    alt.Tooltip("系列:N", title="系列"),
                    alt.Tooltip("金額:Q", title="金額", format=",.0f"),
                ],
            )
        )

        layers = [base_chart]
        if events:
            events_df = pd.DataFrame(events)
            rule_chart = (
                alt.Chart(events_df)
                .mark_rule(strokeDash=[4, 4], color="gray")
                .encode(
                    x=alt.X("year:Q", axis=alt.Axis(format="d")),
                    tooltip=[
                        alt.Tooltip("label:N", title="イベント"),
                        alt.Tooltip("year:Q", title="年", format="d"),
                    ],
                )
            )
            layers.append(rule_chart)

        combined_chart = alt.layer(*layers).properties(height=350)
        with st.container(border=True):
            st.altair_chart(combined_chart)

        if events:
            st.caption(" / ".join(f"{e['label']}: {e['year']}年" for e in events))

    st.markdown("#### 育児費用の予測（全国平均・中央値目安ベース）")
    if children_df.empty:
        st.info("子どもを登録すると、育児費用の予測グラフが表示されます。")
    else:
        childcare_forecast_df = build_childcare_forecast(settings, children_df)
        if childcare_forecast_df.empty:
            st.info("設定された年齢上限の範囲では、対象になる育児費用がありません。")
        else:
            childcare_chart = (
                alt.Chart(childcare_forecast_df)
                .mark_bar()
                .encode(
                    x=alt.X("year:O", title="年"),
                    y=alt.Y("childcare_cost:Q", title=None),
                    tooltip=[
                        alt.Tooltip("year:O", title="年"),
                        alt.Tooltip("childcare_cost:Q", title="育児費用目安", format=",.0f"),
                    ],
                )
                .properties(height=300)
            )
            with st.container(border=True):
                st.altair_chart(childcare_chart)
