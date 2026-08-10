"""Personal cash flow tracker with NISA tracking and budget advice."""

from datetime import date, timedelta

import altair as alt
import pandas as pd

import streamlit as st
from advice import generate_advice
from db import (
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    NISA_CATEGORIES,
    NISA_GROWTH_ANNUAL_LIMIT,
    NISA_GROWTH_LIFETIME_LIMIT,
    NISA_LIFETIME_LIMIT,
    NISA_TSUMITATE_ANNUAL_LIMIT,
    SETTING_GROWTH_LIFETIME_BEFORE,
    SETTING_GROWTH_YTD_BEFORE,
    SETTING_INITIAL_CASH,
    SETTING_TSUMITATE_LIFETIME_BEFORE,
    SETTING_TSUMITATE_YTD_BEFORE,
    add_transaction,
    delete_budget,
    delete_transactions,
    get_budgets,
    get_settings,
    get_transactions,
    set_budget,
    set_setting,
)

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
TYPE_LABELS = {"income": "収入", "expense": "支出", "investment": "投資(NISA)"}


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

        if st.form_submit_button("追加", type="primary"):
            type_map = {"収入": "income", "支出": "expense", "投資(NISA)": "investment"}
            add_transaction(
                date=entry_date.isoformat(),
                type_=type_map[entry_type],
                category=entry_category,
                amount=entry_amount,
                memo=entry_memo,
            )
            st.rerun()


# =============================================================================
# Data
# =============================================================================

st.markdown("# :material/payments: キャッシュフロー管理")

all_transactions = get_transactions()
budgets = get_budgets()
settings = get_settings()

tab_dashboard, tab_nisa, tab_budget, tab_settings = st.tabs(
    ["ダッシュボード", "NISA積立", "予算設定", "初期設定"]
)


# =============================================================================
# Tab: ダッシュボード
# =============================================================================

with tab_dashboard:
    if all_transactions.empty:
        st.info("左のフォームから取引を追加してください。「初期設定」タブで現在の残高も登録できます。")
    else:
        current_net = (
            all_transactions.loc[all_transactions["type"] == "income", "amount"].sum()
            - all_transactions.loc[all_transactions["type"] == "expense", "amount"].sum()
            - all_transactions.loc[all_transactions["type"] == "investment", "amount"].sum()
        )
        current_balance = settings[SETTING_INITIAL_CASH] + current_net
        st.metric("現在の残高（初期設定 + 記録した取引の合計）", f"¥{current_balance:,.0f}")

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

        st.markdown("### 健全化アドバイス")
        for tip in generate_advice(all_transactions, budgets, settings):
            st.info(tip, icon=":material/lightbulb:")

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

        event = st.dataframe(
            display_df,
            column_config={
                "id": None,
                "date": st.column_config.DateColumn("日付"),
                "type": st.column_config.TextColumn("種別"),
                "category": st.column_config.TextColumn("カテゴリ"),
                "amount": st.column_config.NumberColumn("金額", format="¥%.0f"),
                "memo": st.column_config.TextColumn("メモ"),
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
        "ここでは単純に拠出累計額で消化率を近似表示しています。"
        "「初期設定」タブで登録した既存の拠出額も合算されます。"
    )

    investment_df = all_transactions[all_transactions["type"] == "investment"]
    this_year = date.today().year
    investment_this_year = investment_df[investment_df["date"].dt.year == this_year]

    tsumitate_this_year = (
        investment_this_year.loc[investment_this_year["category"] == "つみたて投資枠", "amount"].sum()
        + settings[SETTING_TSUMITATE_YTD_BEFORE]
    )
    growth_this_year = (
        investment_this_year.loc[investment_this_year["category"] == "成長投資枠", "amount"].sum()
        + settings[SETTING_GROWTH_YTD_BEFORE]
    )
    tsumitate_lifetime = (
        investment_df.loc[investment_df["category"] == "つみたて投資枠", "amount"].sum()
        + settings[SETTING_TSUMITATE_LIFETIME_BEFORE]
    )
    growth_lifetime = (
        investment_df.loc[investment_df["category"] == "成長投資枠", "amount"].sum()
        + settings[SETTING_GROWTH_LIFETIME_BEFORE]
    )

    st.markdown(f"#### {this_year}年の年間枠")
    annual_cols = st.columns(2)
    with annual_cols[0]:
        with st.container(border=True):
            st.markdown("**つみたて投資枠**")
            ratio = min(tsumitate_this_year / NISA_TSUMITATE_ANNUAL_LIMIT, 1.0)
            st.progress(ratio, text=f"¥{tsumitate_this_year:,.0f} / ¥{NISA_TSUMITATE_ANNUAL_LIMIT:,.0f}")
    with annual_cols[1]:
        with st.container(border=True):
            st.markdown("**成長投資枠**")
            ratio = min(growth_this_year / NISA_GROWTH_ANNUAL_LIMIT, 1.0)
            st.progress(ratio, text=f"¥{growth_this_year:,.0f} / ¥{NISA_GROWTH_ANNUAL_LIMIT:,.0f}")

    st.markdown("#### 生涯投資枠")
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

    st.markdown("#### 積立履歴")
    if investment_df.empty:
        st.info("NISAの積立を記録すると、ここに履歴が表示されます。")
    else:
        st.dataframe(
            investment_df[["date", "category", "amount", "memo"]],
            column_config={
                "date": st.column_config.DateColumn("日付"),
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

        st.markdown("##### NISAつみたて投資枠")
        nisa_cols_1 = st.columns(2)
        with nisa_cols_1[0]:
            tsumitate_ytd = st.number_input(
                "今年すでに拠出した額",
                min_value=0,
                step=1000,
                value=int(settings[SETTING_TSUMITATE_YTD_BEFORE]),
            )
        with nisa_cols_1[1]:
            tsumitate_lifetime = st.number_input(
                "制度開始からの拠出累計額（生涯枠）",
                min_value=0,
                step=1000,
                value=int(settings[SETTING_TSUMITATE_LIFETIME_BEFORE]),
            )

        st.markdown("##### NISA成長投資枠")
        nisa_cols_2 = st.columns(2)
        with nisa_cols_2[0]:
            growth_ytd = st.number_input(
                "今年すでに拠出した額",
                min_value=0,
                step=1000,
                value=int(settings[SETTING_GROWTH_YTD_BEFORE]),
                key="growth_ytd_input",
            )
        with nisa_cols_2[1]:
            growth_lifetime = st.number_input(
                "制度開始からの拠出累計額（生涯枠）",
                min_value=0,
                step=1000,
                value=int(settings[SETTING_GROWTH_LIFETIME_BEFORE]),
                key="growth_lifetime_input",
            )

        if st.form_submit_button("保存", type="primary"):
            set_setting(SETTING_INITIAL_CASH, initial_cash)
            set_setting(SETTING_TSUMITATE_YTD_BEFORE, tsumitate_ytd)
            set_setting(SETTING_TSUMITATE_LIFETIME_BEFORE, tsumitate_lifetime)
            set_setting(SETTING_GROWTH_YTD_BEFORE, growth_ytd)
            set_setting(SETTING_GROWTH_LIFETIME_BEFORE, growth_lifetime)
            st.toast("初期設定を保存しました。", icon=":material/check_circle:")
            st.rerun()
