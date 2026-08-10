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
    SETTING_GROWTH_LIFETIME_BEFORE,
    SETTING_GROWTH_YTD_BEFORE,
    SETTING_INITIAL_CASH,
    SETTING_TSUMITATE_LIFETIME_BEFORE,
    SETTING_TSUMITATE_YTD_BEFORE,
    add_account,
    add_recurring_expense,
    add_transaction,
    add_transfer,
    apply_recurring_expenses,
    delete_account,
    delete_budget,
    delete_recurring_expense,
    delete_transactions,
    get_accounts,
    get_budgets,
    get_recurring_expenses,
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
    + settings[SETTING_TSUMITATE_LIFETIME_BEFORE]
    + settings[SETTING_GROWTH_LIFETIME_BEFORE]
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

        account_labels = {"未設定": None}
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
            )
            st.rerun()


st.markdown("### :material/payments: キャッシュフロー管理")

tab_dashboard, tab_nisa, tab_budget, tab_accounts, tab_recurring, tab_settings = st.tabs(
    ["ダッシュボード", "NISA積立", "予算設定", "口座・カード", "固定費", "初期設定"]
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
        display_df["account"] = display_df["account_id"].map(account_name_map).fillna("未設定")
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
            set_setting(SETTING_TSUMITATE_YTD_BEFORE, tsumitate_ytd)
            set_setting(SETTING_TSUMITATE_LIFETIME_BEFORE, tsumitate_lifetime)
            set_setting(SETTING_GROWTH_YTD_BEFORE, growth_ytd)
            set_setting(SETTING_GROWTH_LIFETIME_BEFORE, growth_lifetime)
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
            rec_account_labels = {"未設定": None}
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
                account_label = account_name_map.get(row.account_id, "未設定")
                st.write(f"{account_label}（毎月{row.day_of_month}日）")
            with row_cols[4]:
                if st.button(":material/delete:", key=f"del_recurring_{row.id}", type="tertiary"):
                    delete_recurring_expense(row.id)
                    st.rerun()
