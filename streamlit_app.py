"""Personal cash flow tracker with NISA tracking and budget advice."""

import hmac
import time
from datetime import date

import altair as alt
import pandas as pd

import streamlit as st
from advice import effective_nisa_ytd_before, generate_advice, year_elapsed_ratio
from aggregates import OWNER_NISA_LIFETIME_KEYS, compute_net_worth
from timeutil import today_jst
from ui_helpers import (
    MONTH_SELECT_OPTIONS,
    TIME_RANGES,
    account_select_index,
    build_account_labels as _build_account_labels,
    category_edit_options,
    csv_safe_value,
    filter_by_time_range,
    month_select_index,
    parse_month_label,
    resolved_default,
    safe_index,
    time_range_start,
)
from db import (
    ACCOUNT_KINDS,
    NISA_CATEGORIES,
    NISA_GROWTH_ANNUAL_LIMIT,
    NISA_GROWTH_LIFETIME_LIMIT,
    NISA_LIFETIME_LIMIT,
    NISA_TSUMITATE_ANNUAL_LIMIT,
    OWNERS,
    RecordInUseError,
    SETTING_ANNUAL_EXPENSE_TARGET,
    SETTING_ANNUAL_INCOME_HUSBAND,
    SETTING_ANNUAL_INCOME_WIFE,
    SETTING_CHILDCARE_ANNUAL_COST,
    SETTING_CHILDCARE_END_AGE,
    SETTING_GROWTH_LIFETIME_BEFORE,
    SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND,
    SETTING_GROWTH_LIFETIME_BEFORE_WIFE,
    SETTING_GROWTH_YTD_BEFORE,
    SETTING_GROWTH_YTD_BEFORE_HUSBAND,
    SETTING_GROWTH_YTD_BEFORE_WIFE,
    SETTING_GROWTH_YTD_BEFORE_YEAR_HUSBAND,
    SETTING_GROWTH_YTD_BEFORE_YEAR_WIFE,
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
    SETTING_ANNUAL_INCOME,
    SETTING_TSUMITATE_YTD_BEFORE,
    SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_WIFE,
    SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_YEAR_WIFE,
    SETTING_WIFE_BIRTH_YEAR,
    SETTING_WIFE_PENSION_ANNUAL,
    SETTING_WIFE_PENSION_START_AGE,
    SETTING_WIFE_RETIREMENT_AGE,
    add_account,
    add_child,
    add_expense_category,
    add_income_category,
    add_recurring_expense,
    add_recurring_investment,
    add_transaction,
    add_transfer,
    apply_recurring_expenses,
    apply_recurring_investments,
    delete_account,
    delete_budget,
    delete_child,
    delete_expense_category,
    delete_income_category,
    delete_recurring_expense,
    delete_recurring_investment,
    delete_transactions,
    get_account_usage,
    get_accounts,
    get_budgets,
    get_children,
    get_expense_categories,
    get_expense_category_usage,
    get_income_categories,
    get_income_category_usage,
    get_login_attempt,
    get_present_setting_keys,
    get_recurring_expense_skips,
    get_recurring_expenses,
    get_recurring_investment_skips,
    get_recurring_investments,
    get_settings,
    get_transactions,
    record_login_failure,
    reset_login_attempts,
    resume_recurring_expense,
    resume_recurring_investment,
    set_budget,
    set_setting,
    update_recurring_expense,
    update_recurring_investment,
    update_transaction,
)
from forecast import build_childcare_forecast, build_expense_forecast, build_life_events

st.set_page_config(
    page_title="キャッシュフロー管理",
    page_icon=":material/payments:",
    layout="wide",
)


MAX_PASSWORD_ATTEMPTS = 5
PASSWORD_LOCKOUT_SECONDS = 300


def _client_key() -> str:
    """Best-effort per-client identity for the login lockout (see check_password).

    Falls back to a shared "unknown" bucket if the runtime can't report a client IP
    (older Streamlit, or a proxy that doesn't forward one) - no worse than a fully global
    counter in that case, and strictly better whenever a real IP is available.
    """
    try:
        ip = st.context.ip_address
    except Exception:
        ip = None
    return ip or "unknown"


def check_password() -> bool:
    """Show a password gate; return True once the correct password is entered.

    Locks out further attempts for a while after too many wrong guesses, since the
    password itself may be short and this app can be reachable outside localhost. The
    lockout is keyed per client (_client_key), not a single global counter - a global
    counter would let anyone who keeps guessing wrong lock out the real users too, since
    it doesn't matter who sent the failing request. The attempt count/lockout is persisted
    in the DB (not st.session_state), since a session-local counter is trivially bypassed
    by opening a fresh browser session.
    """
    client_key = _client_key()

    def password_entered() -> None:
        # A stale/duplicate on_change event can still arrive after the widget itself
        # was unmounted (e.g. once already logged in), when "password_input" is gone
        # from session_state. Ignore it instead of crashing the whole app on a KeyError.
        if "password_input" not in st.session_state:
            return
        # compare_digest requires equal-length ASCII str or bytes; encoding to utf-8 first
        # avoids a TypeError if the configured password contains non-ASCII characters.
        entered = st.session_state["password_input"].encode("utf-8")
        expected = st.secrets["APP_PASSWORD"].encode("utf-8")
        if hmac.compare_digest(entered, expected):
            st.session_state["password_correct"] = True
            reset_login_attempts(client_key)
            del st.session_state["password_input"]
        else:
            st.session_state["password_correct"] = False
            record_login_failure(client_key, MAX_PASSWORD_ATTEMPTS, PASSWORD_LOCKOUT_SECONDS)

    if st.session_state.get("password_correct"):
        return True

    _, locked_until = get_login_attempt(client_key)
    remaining = locked_until - time.time()
    if remaining > 0:
        st.error(f"試行回数が上限に達しました。{int(remaining) + 1}秒後に再試行してください。")
        return False

    st.text_input("パスワード", type="password", on_change=password_entered, key="password_input")
    if st.session_state.get("password_correct") is False:
        st.error("パスワードが違います。")
    return False


if not check_password():
    st.stop()


TYPE_LABELS = {"income": "収入", "expense": "支出", "investment": "投資(NISA)", "transfer": "振替"}


# =============================================================================
# Data
# =============================================================================

@st.cache_data(ttl=3600)
def _apply_recurring_once(today_: date) -> None:
    """Run the recurring-expense/investment application at most once per hour.

    Streamlit reruns this whole script on every widget interaction, so calling these
    directly here would hit the database on every click. They're idempotent, so caching
    the (no-op) return value for a while is enough to cut that down without needing a
    scheduled job.
    """
    apply_recurring_expenses()
    apply_recurring_investments()


_apply_recurring_once(today_jst())

all_transactions = get_transactions()
budgets = get_budgets()
settings = get_settings()
present_setting_keys = get_present_setting_keys()
accounts_df = get_accounts()
recurring_df = get_recurring_expenses()
expense_categories = get_expense_categories()
income_categories = get_income_categories()

account_name_map: dict[int, str] = {row.id: f"{row.owner}: {row.name}" for row in accounts_df.itertuples()}


def build_account_labels(accounts: pd.DataFrame) -> dict[str, int | None]:
    return _build_account_labels(accounts, ACCOUNT_KINDS)


def confirm_delete(
    icon_label: str, key: str, message: str = "本当に削除しますか？この操作は取り消せません。"
) -> bool:
    """A delete trigger that requires a second confirming click inside a popover.

    A single click used to delete immediately with no undo, which is easy to trigger by
    accident on financial records. Wrapping the trigger in a popover adds a deliberate
    second step without disturbing the surrounding (often narrow) column layout.
    """
    with st.popover(icon_label):
        st.write(message)
        return st.button("削除する", key=f"{key}_confirm_delete", type="primary")

# 残高・当期集計は「今日以前」の取引だけを基準にする。未来日の取引を計算に含めると、まだ
# 発生していない金額で現在の残高・当年NISA進捗・当月予算実績が汚染されてしまうため。
# all_transactions 自体は取引履歴タブでの表示・削除操作のために未来日データも残しておく。
_today_ts = pd.Timestamp(today_jst())
transactions_to_date = all_transactions[all_transactions["date"] <= _today_ts]

net_worth = compute_net_worth(transactions_to_date, accounts_df, settings, owners=OWNERS)
account_balances = net_worth.account_balances
current_balance = net_worth.current_balance
total_assets = net_worth.total_assets
owner_account_totals = net_worth.owner_account_totals
owner_nisa_totals = net_worth.owner_nisa_totals
owner_total_assets = net_worth.owner_total_assets
shared_cash = net_worth.shared_cash
unassigned_nisa_total = net_worth.unassigned_nisa_total


# =============================================================================
# Sidebar: new transaction form
# =============================================================================

with st.sidebar:
    st.markdown("### 取引を追加")
    with st.form("add_transaction", clear_on_submit=True):
        entry_date = st.date_input("日付", value=today_jst(), max_value=today_jst())
        entry_type = st.segmented_control(
            "種別",
            options=["収入", "支出", "投資(NISA)"],
            default="支出",
            key="entry_type",
        )
        if entry_type == "収入":
            categories = income_categories
        elif entry_type == "投資(NISA)":
            categories = NISA_CATEGORIES
        else:
            categories = expense_categories
        entry_category = st.selectbox("カテゴリ", options=categories)
        if entry_type == "支出":
            entry_amount = st.number_input(
                "金額",
                step=100,
                help="返金・返品の場合はマイナスの金額を入力すると、そのカテゴリの支出から差し引かれます。",
            )
        elif entry_type == "投資(NISA)":
            entry_amount = st.number_input(
                "金額",
                step=100,
                help=(
                    "売却・解約の場合はマイナスの金額を入力すると拠出累計額から差し引かれます"
                    "（新NISAの正確な非課税枠復活ルールではなく、拠出累計額ベースの簡易的な近似です）。"
                ),
            )
        else:
            entry_amount = st.number_input("金額", min_value=0, step=100)
        entry_memo = st.text_input("メモ", label_visibility="collapsed", placeholder="メモ（任意）")

        entry_owner = None
        if entry_type == "投資(NISA)":
            entry_owner = st.selectbox("所有者", options=OWNERS, key="entry_owner")

        account_labels = build_account_labels(accounts_df)
        entry_account_label = st.selectbox("口座/カード", options=list(account_labels.keys()))

        if st.form_submit_button("追加", type="primary"):
            if entry_amount == 0:
                st.error("金額を入力してください。")
            else:
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

        st.markdown("### 資産状況（夫婦別・全体）")
        asset_summary_cols = st.columns(len(OWNERS) + 1)
        for i, owner in enumerate(OWNERS):
            with asset_summary_cols[i]:
                st.metric(
                    f"{owner}の資産（口座 + NISA）",
                    f"¥{owner_total_assets[owner]:,.0f}",
                    help=(
                        f"口座・カード残高 ¥{owner_account_totals[owner]:,.0f} "
                        f"+ NISA拠出累計 ¥{owner_nisa_totals[owner]:,.0f}"
                    ),
                )
        with asset_summary_cols[-1]:
            st.metric(
                "世帯全体の総資産",
                f"¥{total_assets:,.0f}",
                help=(
                    f"夫・嫁それぞれの資産 + 現金・預金の共通分 ¥{shared_cash:,.0f}"
                    + (
                        f" + 所有者未設定の世帯共通NISA ¥{unassigned_nisa_total:,.0f}"
                        if unassigned_nisa_total
                        else ""
                    )
                    + " の合計です。"
                ),
            )
        if unassigned_nisa_total:
            st.caption(
                f"所有者が未設定のNISA取引 ¥{unassigned_nisa_total:,.0f} は「世帯共通NISA」として"
                "総資産に含めていますが、夫・嫁いずれの内訳にも含まれていません。"
                "「NISA積立」タブから取引履歴を確認し、可能であれば所有者を記録し直してください。"
            )

        if not accounts_df.empty:
            st.markdown("**口座・カード別残高**")
            balance_cols = st.columns(min(len(accounts_df), 4) or 1)
            for i, row in enumerate(accounts_df.itertuples()):
                with balance_cols[i % len(balance_cols)]:
                    st.metric(f"{row.owner}: {row.name}", f"¥{account_balances[row.id]:,.0f}")

        time_range = st.segmented_control(
            "期間", options=TIME_RANGES, default="すべて", key="dashboard_time_range"
        )
        time_range = time_range or "すべて"
        filtered = filter_by_time_range(transactions_to_date, time_range)

        # 期間内の手取り年収(給与)を日割りで按分し、副収入(取引で記録した収入)に加算する。
        # 給与を記録した設定はここに含めないと「収入合計」が実態(=健全化アドバイスが使う定義と同じ)
        # より著しく過小表示になるため。ただし「すべて」はアプリ利用開始から現在までの全期間が
        # 対象になり得るため、"今の"年収設定を何年も遡って掛け合わせると実態と無関係に過大表示に
        # なってしまう(過去に年収が違った・無収入期間があった場合など)。日割りは期間の長さが
        # 明確な範囲(1ヶ月/6ヶ月/1年/今年)に限定し、「すべて」では記録された収入取引のみを使う。
        period_start = time_range_start(transactions_to_date, time_range, _today_ts)
        annual_salary = settings[SETTING_ANNUAL_INCOME_HUSBAND] + settings[SETTING_ANNUAL_INCOME_WIFE]
        if time_range != "すべて" and period_start is not None:
            period_days = max((_today_ts - period_start).days, 0)
            prorated_salary = annual_salary / 365 * period_days
        else:
            prorated_salary = 0.0

        total_income = prorated_salary + filtered.loc[filtered["type"] == "income", "amount"].sum()
        total_expense = filtered.loc[filtered["type"] == "expense", "amount"].sum()
        total_investment = filtered.loc[filtered["type"] == "investment", "amount"].sum()

        kpi_cols = st.columns(3)
        with kpi_cols[0]:
            if time_range == "すべて":
                income_help = (
                    "「すべて」の期間は手取り年収の日割りを含みません（現在の年収設定を何年も遡って"
                    "適用すると実態と無関係な金額になるため）。「取引を追加」で記録した収入"
                    "(副業・投資・その他収入)のみの合計です。"
                )
            else:
                income_help = (
                    f"初期設定の手取り年収を期間の日数分だけ日割りした ¥{prorated_salary:,.0f} に、"
                    "「取引を追加」で記録した収入(副業・投資・その他収入)を加えた金額です。"
                )
            st.metric("収入合計（期間内）", f"¥{total_income:,.0f}", help=income_help)
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
        for tip in generate_advice(transactions_to_date, budgets, settings):
            st.info(tip, icon=":material/lightbulb:")

        expense_target = settings[SETTING_ANNUAL_EXPENSE_TARGET]
        if expense_target > 0:
            st.markdown("### 年間支出目標との比較")
            this_year_num = today_jst().year
            elapsed_ratio = year_elapsed_ratio(today_jst())
            this_year_all = transactions_to_date[transactions_to_date["date"].dt.year == this_year_num]

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
                # 振替(transfer)は口座間の付け替えで世帯の収支ではないため除外する。
                # 含めてしまうと出金側だけがマイナス計上され、振替のたびに累計収支が
                # 実態より下振れする（口座残高側は transfer_in で正しく相殺済み）。
                cash_flow = filtered[~filtered["type"].isin(["investment", "transfer"])].copy()
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

        # 上の「期間」選択と連動させる。filter_by_time_range は下限のみを課すフィルタなので、
        # 未来日の取引（通常は入力できないが念のため）はどの期間を選んでも表示され続ける。
        history_source = filter_by_time_range(all_transactions, time_range)
        history_filter_cols = st.columns([2, 1])
        with history_filter_cols[0]:
            history_category_options = sorted(history_source["category"].dropna().unique().tolist())
            selected_history_categories = st.multiselect(
                "カテゴリで絞り込み", options=history_category_options, key="history_category_filter"
            )
        if selected_history_categories:
            history_source = history_source[history_source["category"].isin(selected_history_categories)]
        with history_filter_cols[1]:
            st.markdown("&nbsp;")
            st.caption(f"{len(history_source):,}件を表示中")

        export_cols = st.columns([1, 3])
        with export_cols[0]:
            export_df = history_source.drop(
                columns=["account_id", "to_account_id", "recurring_expense_id", "recurring_investment_id"]
            ).copy()
            export_df["type"] = export_df["type"].map(TYPE_LABELS)
            # メモは自由入力のため、"="などで始まる値がExcel/スプレッドシートで数式として
            # 解釈されてしまう(CSVインジェクション)のを防ぐ。
            export_df["memo"] = export_df["memo"].map(csv_safe_value)
            st.download_button(
                ":material/download: CSVでダウンロード",
                data=export_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"取引履歴_{today_jst().isoformat()}.csv",
                mime="text/csv",
                key="download_transactions_csv",
            )

        display_df = history_source.copy()
        display_df["type"] = display_df["type"].map(TYPE_LABELS)
        display_df["account"] = display_df["account_id"].map(account_name_map).fillna("現金")
        is_transfer = history_source["type"] == "transfer"
        if is_transfer.any():
            # 全件（振替なし含む）に対してmapしてから絞り込む。振替が1件も無い状態で
            # 先に0件へ絞り込んでからmap+文字列結合すると、空Seriesのdtypeが数値のまま
            # 残り、" → "との結合でTypeErrorになるため。
            # account_id/to_account_id が無い(=現金)行は fillna で「現金」にしてから結合する。
            # astype(str)を先にかけると欠損値が文字列"nan"になってしまうため、fillnaが先。
            transfer_label = (
                history_source["account_id"].map(account_name_map).fillna("現金")
                + " → "
                + history_source["to_account_id"].map(account_name_map).fillna("現金")
            )
            display_df.loc[is_transfer, "account"] = transfer_label.loc[is_transfer]

        event = st.dataframe(
            display_df,
            column_config={
                "id": None,
                "account_id": None,
                "to_account_id": None,
                "recurring_expense_id": None,
                "recurring_investment_id": None,
                # 投資(NISA)取引以外は常にNULLで空欄ノイズになるため非表示にする。
                # 所有者別の内訳は「NISA積立」タブの積立履歴テーブルで別途確認できる。
                "owner": None,
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
            action_cols = st.columns([1, 1]) if len(selected_ids) == 1 else st.columns([1])
            with action_cols[0]:
                if confirm_delete(
                    f":material/delete: 選択した{len(selected_ids)}件を削除",
                    key="transactions",
                    message=f"選択した{len(selected_ids)}件を削除しますか？この操作は取り消せません。",
                ):
                    delete_transactions(selected_ids)
                    st.rerun()
            if len(selected_ids) == 1:
                selected_row = history_source.loc[history_source["id"] == selected_ids[0]].iloc[0]
                # 振替は口座2つにまたがる特殊な意味を持ち、固定費/定期積立の自動記帳は
                # バックフィルの「MAX(date)は記帳済み」という前提を編集で壊しうるため、
                # その場での編集は手入力した収入・支出・投資取引のみに限定する。
                is_editable = (
                    selected_row["type"] != "transfer"
                    and pd.isna(selected_row["recurring_expense_id"])
                    and pd.isna(selected_row["recurring_investment_id"])
                )
                with action_cols[1]:
                    if is_editable:
                        if st.button(":material/edit: 選択した取引を編集", key="edit_transaction_button"):
                            st.session_state["editing_transaction_id"] = int(selected_ids[0])
                            st.rerun()
                    else:
                        st.caption(
                            "振替、または固定費/定期積立の自動記帳による取引は編集できません"
                            "（固定費/定期積立タブから設定を変更するか、削除してください）。"
                        )

        editing_transaction_id = st.session_state.get("editing_transaction_id")
        if editing_transaction_id is not None:
            match = all_transactions.loc[all_transactions["id"] == editing_transaction_id]
            if match.empty:
                del st.session_state["editing_transaction_id"]
            else:
                edit_row = match.iloc[0]
                with st.form("edit_transaction_form"):
                    st.markdown("**取引を編集**")
                    edit_cols = st.columns([1, 1, 1, 2])
                    with edit_cols[0]:
                        edit_date = st.date_input(
                            "日付", value=edit_row["date"].date(), max_value=today_jst(), key="edit_txn_date"
                        )
                    with edit_cols[1]:
                        edit_type_options = ["収入", "支出", "投資(NISA)"]
                        edit_type_label = st.selectbox(
                            "種別",
                            options=edit_type_options,
                            index=safe_index(edit_type_options, TYPE_LABELS[edit_row["type"]]),
                            key="edit_txn_type",
                        )
                    if edit_type_label == "収入":
                        edit_categories = income_categories
                    elif edit_type_label == "投資(NISA)":
                        edit_categories = NISA_CATEGORIES
                    else:
                        edit_categories = expense_categories
                    with edit_cols[2]:
                        # カテゴリが後から削除されていた場合、単純な safe_index だと「見つからない
                        # ので先頭の別カテゴリが無警告で選択済み表示される」→気づかず保存すると
                        # 取引のカテゴリが静かに書き換わってしまう。category_edit_options は元の
                        # カテゴリ名をそのまま選択肢に残すことでこれを防ぐ（詳細はui_helpers.py参照）。
                        edit_category_options, edit_category_index = category_edit_options(
                            edit_categories, edit_row["category"]
                        )
                        edit_category = st.selectbox(
                            "カテゴリ",
                            options=edit_category_options,
                            index=edit_category_index,
                            key="edit_txn_category",
                        )
                        if edit_row["category"] not in edit_categories:
                            st.caption(
                                ":material/warning: このカテゴリは削除済みです。"
                                "このまま保存すると同じ名前のカテゴリとして残ります。"
                            )
                    with edit_cols[3]:
                        edit_memo = st.text_input(
                            "メモ",
                            value=edit_row["memo"] if pd.notna(edit_row["memo"]) else "",
                            key="edit_txn_memo",
                        )

                    detail_cols = st.columns(3)
                    with detail_cols[0]:
                        if edit_type_label == "支出":
                            edit_amount = st.number_input(
                                "金額",
                                step=100,
                                value=int(edit_row["amount"]),
                                help="返金・返品の場合はマイナスの金額を入力できます。",
                                key="edit_txn_amount",
                            )
                        elif edit_type_label == "投資(NISA)":
                            edit_amount = st.number_input(
                                "金額",
                                step=100,
                                value=int(edit_row["amount"]),
                                help="売却・解約の場合はマイナスの金額を入力できます（拠出累計額から差し引かれます）。",
                                key="edit_txn_amount",
                            )
                        else:
                            edit_amount = st.number_input(
                                "金額",
                                min_value=0,
                                step=100,
                                value=int(edit_row["amount"]),
                                key="edit_txn_amount",
                            )
                    edit_owner = None
                    with detail_cols[1]:
                        if edit_type_label == "投資(NISA)":
                            edit_owner = st.selectbox(
                                "所有者",
                                options=OWNERS,
                                index=safe_index(OWNERS, edit_row["owner"]),
                                key="edit_txn_owner",
                            )
                    with detail_cols[2]:
                        edit_account_labels = build_account_labels(accounts_df)
                        current_account_id = (
                            None if pd.isna(edit_row["account_id"]) else int(edit_row["account_id"])
                        )
                        account_default_index = account_select_index(edit_account_labels, current_account_id)
                        edit_account_label = st.selectbox(
                            "口座/カード",
                            options=list(edit_account_labels.keys()),
                            index=account_default_index,
                            key="edit_txn_account",
                        )

                    save_cols = st.columns(2)
                    with save_cols[0]:
                        save_clicked = st.form_submit_button("保存", type="primary")
                    with save_cols[1]:
                        cancel_clicked = st.form_submit_button("キャンセル")

                    if save_clicked:
                        if edit_amount == 0:
                            st.error("金額を入力してください。")
                        else:
                            edit_type_map = {"収入": "income", "支出": "expense", "投資(NISA)": "investment"}
                            update_transaction(
                                int(editing_transaction_id),
                                date=edit_date.isoformat(),
                                type_=edit_type_map[edit_type_label],
                                category=edit_category,
                                amount=edit_amount,
                                memo=edit_memo,
                                account_id=edit_account_labels[edit_account_label],
                                owner=edit_owner,
                            )
                            del st.session_state["editing_transaction_id"]
                            st.rerun()
                    if cancel_clicked:
                        del st.session_state["editing_transaction_id"]
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

    investment_df = transactions_to_date[transactions_to_date["type"] == "investment"]
    this_year = today_jst().year
    investment_this_year = investment_df[investment_df["date"].dt.year == this_year]

    owner_lifetime_keys = OWNER_NISA_LIFETIME_KEYS

    for owner in OWNERS:
        st.markdown(f"### {owner}のNISA")
        owner_investment_df = investment_df[investment_df["owner"] == owner]
        owner_investment_this_year = investment_this_year[investment_this_year["owner"] == owner]

        tsumitate_this_year = (
            owner_investment_this_year.loc[
                owner_investment_this_year["category"] == "つみたて投資枠", "amount"
            ].sum()
            + effective_nisa_ytd_before(settings, owner, "つみたて投資枠", today_jst())
        )
        growth_this_year = (
            owner_investment_this_year.loc[owner_investment_this_year["category"] == "成長投資枠", "amount"].sum()
            + effective_nisa_ytd_before(settings, owner, "成長投資枠", today_jst())
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
            ri_account_labels = build_account_labels(accounts_df)
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
            editing_key = f"editing_recurring_investment_{row.id}"

            if st.session_state.get(editing_key):
                with st.form(f"edit_recurring_investment_{row.id}"):
                    e_cols = st.columns([1, 1, 1, 2, 1])
                    with e_cols[0]:
                        e_owner = st.selectbox(
                            "所有者",
                            options=OWNERS,
                            index=safe_index(OWNERS, row.owner),
                            key=f"e_ri_owner_{row.id}",
                        )
                    with e_cols[1]:
                        e_category = st.selectbox(
                            "枠",
                            options=NISA_CATEGORIES,
                            index=safe_index(NISA_CATEGORIES, row.category),
                            key=f"e_ri_category_{row.id}",
                        )
                    with e_cols[2]:
                        e_amount = st.number_input(
                            "金額", min_value=0, step=1000, value=int(row.amount), key=f"e_ri_amount_{row.id}"
                        )
                    with e_cols[3]:
                        e_account_labels = build_account_labels(accounts_df)
                        current_account_id = None if pd.isna(row.account_id) else int(row.account_id)
                        e_account_label = st.selectbox(
                            "引き落とし口座/カード",
                            options=list(e_account_labels.keys()),
                            index=account_select_index(e_account_labels, current_account_id),
                            key=f"e_ri_account_{row.id}",
                        )
                    with e_cols[4]:
                        e_day = st.number_input(
                            "積立日",
                            min_value=1,
                            max_value=28,
                            value=int(row.day_of_month),
                            step=1,
                            key=f"e_ri_day_{row.id}",
                        )

                    e_ri_effective_from = st.date_input(
                        "金額変更の適用開始日",
                        value=today_jst(),
                        help=(
                            "金額を変更した場合、この日付より前の月は元の金額のまま、この日付以降の月"
                            "（既に記帳済みの取引も含めて自動的に金額が更新されます）は新しい金額が使われます。"
                            "実際に積立額を変更した月の1日を指定してください。"
                        ),
                        key=f"e_ri_effective_from_{row.id}",
                    )

                    save_cols = st.columns(2)
                    with save_cols[0]:
                        save_clicked = st.form_submit_button("保存", type="primary")
                    with save_cols[1]:
                        cancel_clicked = st.form_submit_button("キャンセル")

                    if save_clicked:
                        update_recurring_investment(
                            row.id,
                            e_owner,
                            e_category,
                            e_amount,
                            e_account_labels[e_account_label],
                            int(e_day),
                            change_effective_from=e_ri_effective_from,
                        )
                        del st.session_state[editing_key]
                        st.rerun()
                    if cancel_clicked:
                        del st.session_state[editing_key]
                        st.rerun()
            else:
                row_cols = st.columns([1, 1, 1, 2, 1, 1])
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
                    if st.button(":material/edit:", key=f"edit_recurring_investment_{row.id}", type="tertiary"):
                        st.session_state[editing_key] = True
                        st.rerun()
                with row_cols[5]:
                    if confirm_delete(":material/delete:", key=f"del_recurring_investment_{row.id}"):
                        delete_recurring_investment(row.id)
                        st.rerun()

    investment_skips_df = get_recurring_investment_skips()
    if not investment_skips_df.empty:
        st.markdown("#### スキップ中の月")
        st.caption(
            "自動記帳された積立を削除すると、その月は「意図的にスキップした」として記録され、"
            "以後自動的には再記帳されません。誤って削除した場合はここから再記帳できます。"
        )
        for skip in investment_skips_df.itertuples():
            skip_cols = st.columns([3, 1])
            with skip_cols[0]:
                st.write(f"{skip.owner}の{skip.category}: {int(skip.year)}年{int(skip.month)}月")
            with skip_cols[1]:
                if st.button(
                    "再記帳する",
                    key=f"resume_recurring_investment_{skip.recurring_investment_id}_{skip.year}_{skip.month}",
                    type="tertiary",
                ):
                    resume_recurring_investment(
                        skip.recurring_investment_id, int(skip.year), int(skip.month)
                    )
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
    if not expense_categories:
        st.info("下の「カテゴリの管理」から支出カテゴリを追加すると、予算を設定できます。")
    else:
        with st.form("set_budget", clear_on_submit=True):
            budget_cols = st.columns([2, 2, 1])
            with budget_cols[0]:
                budget_category = st.selectbox("カテゴリ", options=expense_categories)
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
                if confirm_delete(":material/delete:", key=f"del_budget_{category}"):
                    delete_budget(category)
                    st.rerun()

    st.markdown("---")
    st.markdown("#### 支出カテゴリの管理")
    st.caption(
        "支出カテゴリは自由に追加・削除できます。ただし、固定費・予算・過去の取引で"
        "使用中のカテゴリは削除できません（先にそちらのカテゴリを変更してください）。"
    )
    with st.form("add_expense_category", clear_on_submit=True):
        cat_cols = st.columns([3, 1])
        with cat_cols[0]:
            new_category_name = st.text_input("新しいカテゴリ名", placeholder="例: ペット、趣味")
        with cat_cols[1]:
            st.markdown("&nbsp;")
            if st.form_submit_button("追加", type="primary"):
                if new_category_name.strip():
                    add_expense_category(new_category_name.strip())
                    st.rerun()
                else:
                    st.error("カテゴリ名を入力してください。")

    for category in expense_categories:
        cat_row_cols = st.columns([3, 1])
        with cat_row_cols[0]:
            st.write(category)
        with cat_row_cols[1]:
            if len(expense_categories) <= 1:
                st.caption("最後の1つは削除できません")
            elif confirm_delete(":material/delete:", key=f"del_category_{category}"):
                usage = get_expense_category_usage(category)
                in_use = [
                    label
                    for label, count in (
                        (f"固定費{usage['recurring_expenses']}件", usage["recurring_expenses"]),
                        (f"予算{usage['budgets']}件", usage["budgets"]),
                        (f"取引{usage['transactions']}件", usage["transactions"]),
                    )
                    if count
                ]
                if in_use:
                    st.error(
                        f"「{category}」は{'・'.join(in_use)}で使用中のため削除できません。"
                        "先に該当の固定費・予算・取引のカテゴリを変更してください。"
                    )
                else:
                    try:
                        delete_expense_category(category)
                        st.rerun()
                    except RecordInUseError:
                        st.error(
                            f"「{category}」は他で使用中のため削除できませんでした。"
                            "画面を更新して再度お試しください。"
                        )

    st.markdown("---")
    st.markdown("#### 収入カテゴリの管理")
    st.caption(
        "収入カテゴリも自由に追加・削除できます（給与は「初期設定」タブの手取り年収で別途管理します）。"
        "ただし、過去の取引で使用中のカテゴリは削除できません（先にその取引のカテゴリを変更してください）。"
    )
    with st.form("add_income_category", clear_on_submit=True):
        income_cat_cols = st.columns([3, 1])
        with income_cat_cols[0]:
            new_income_category_name = st.text_input(
                "新しいカテゴリ名", placeholder="例: ボーナス、還付金", key="new_income_category_name"
            )
        with income_cat_cols[1]:
            st.markdown("&nbsp;")
            if st.form_submit_button("追加", type="primary", key="add_income_category_submit"):
                if new_income_category_name.strip():
                    add_income_category(new_income_category_name.strip())
                    st.rerun()
                else:
                    st.error("カテゴリ名を入力してください。")

    for category in income_categories:
        income_cat_row_cols = st.columns([3, 1])
        with income_cat_row_cols[0]:
            st.write(category)
        with income_cat_row_cols[1]:
            if len(income_categories) <= 1:
                st.caption("最後の1つは削除できません")
            elif confirm_delete(":material/delete:", key=f"del_income_category_{category}"):
                usage = get_income_category_usage(category)
                if usage["transactions"]:
                    st.error(
                        f"「{category}」は取引{usage['transactions']}件で使用中のため削除できません。"
                        "先に該当の取引のカテゴリを変更してください。"
                    )
                else:
                    try:
                        delete_income_category(category)
                        st.rerun()
                    except RecordInUseError:
                        st.error(
                            f"「{category}」は他で使用中のため削除できませんでした。"
                            "画面を更新して再度お試しください。"
                        )


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

        st.caption(
            "「今年の拠出額」は入力した年のみ有効です。年をまたいでも自動で足され続けることはなく、"
            "翌年になったら0から記録し直してください（生涯拠出累計額は年をまたいでも保持されます）。"
        )

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
        nisa_owner_year_keys = {
            "夫": (SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND, SETTING_GROWTH_YTD_BEFORE_YEAR_HUSBAND),
            "嫁": (SETTING_TSUMITATE_YTD_BEFORE_YEAR_WIFE, SETTING_GROWTH_YTD_BEFORE_YEAR_WIFE),
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
        # 夫の欄がまだ一度も明示保存されておらず、旧合算値がそのまま初期表示されている場合は、
        # 「これは夫個人の拠出額ではなく夫婦合算の旧設定値」であることに気づかず保存し、
        # 嫁の分を追加入力して二重計上してしまうリスクがあるため警告する。
        if any(legacy_nisa_defaults["夫"]) and not any(
            key in present_setting_keys for key in nisa_owner_keys["夫"]
        ):
            st.warning(
                "つみたて/成長投資枠の「夫」欄には、夫婦別管理に移行する前の**世帯合算値**が"
                "初期表示されています。夫個人の拠出額ではない可能性があるため、内容を確認してから"
                "保存してください。そのまま保存して嫁の分を追加入力すると、合計が二重計上されます。"
            )

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
                    value=resolved_default(ytd_t_key, legacy_ytd_t, present_setting_keys, settings),
                    key=f"nisa_{nisa_owner}_ytd_t",
                )
            with o_cols[1]:
                o_life_t = st.number_input(
                    "つみたて: 生涯拠出累計額",
                    min_value=0,
                    step=1000,
                    value=resolved_default(life_t_key, legacy_life_t, present_setting_keys, settings),
                    key=f"nisa_{nisa_owner}_life_t",
                )
            with o_cols[2]:
                o_ytd_g = st.number_input(
                    "成長: 今年の拠出額",
                    min_value=0,
                    step=1000,
                    value=resolved_default(ytd_g_key, legacy_ytd_g, present_setting_keys, settings),
                    key=f"nisa_{nisa_owner}_ytd_g",
                )
            with o_cols[3]:
                o_life_g = st.number_input(
                    "成長: 生涯拠出累計額",
                    min_value=0,
                    step=1000,
                    value=resolved_default(life_g_key, legacy_life_g, present_setting_keys, settings),
                    key=f"nisa_{nisa_owner}_life_g",
                )
            nisa_owner_inputs[nisa_owner] = (o_ytd_t, o_life_t, o_ytd_g, o_life_g)

        st.markdown("##### 手取り年収（夫婦それぞれ）")
        st.caption(
            "毎月の給与を記録しなくても、この設定値（÷12）が月々の収入実績として健全化アドバイスの計算に自動的に使われます。"
            "「取引を追加」の収入カテゴリ（副業・投資・その他収入）は、この年収設定とは別に上乗せされる"
            "副収入用です。給与そのものをここに重複して記録しないでください。"
        )
        income_keys = {"夫": SETTING_ANNUAL_INCOME_HUSBAND, "嫁": SETTING_ANNUAL_INCOME_WIFE}
        # 夫婦別に分ける前の合算値が残っていれば、夫の欄に初期値として引き継ぐ（データ消失防止）。
        legacy_income_defaults = {"夫": int(settings[SETTING_ANNUAL_INCOME]), "嫁": 0}
        if legacy_income_defaults["夫"] and income_keys["夫"] not in present_setting_keys:
            st.warning(
                "「夫」の手取り年収欄には、夫婦別管理に移行する前の**世帯合算の年収**が"
                "初期表示されています。内容を確認してから保存してください。そのまま保存して"
                "嫁の年収を追加入力すると、世帯合計収入が二重計上されます。"
            )
        income_cols = st.columns(2)
        annual_income_inputs: dict[str, int] = {}
        for i, income_owner in enumerate(OWNERS):
            with income_cols[i]:
                annual_income_inputs[income_owner] = st.number_input(
                    f"{income_owner}の手取り年収",
                    min_value=0,
                    step=10000,
                    value=resolved_default(
                        income_keys[income_owner],
                        legacy_income_defaults[income_owner],
                        present_setting_keys,
                        settings,
                    ),
                    key=f"income_{income_owner}",
                )

        st.markdown("##### 支出設定")
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
                ytd_year_t_key, ytd_year_g_key = nisa_owner_year_keys[nisa_owner]
                # 値が変わった時だけ「今年の分」として年をスタンプする。無関係な項目を
                # 保存しただけで、翌年以降にも古い値の適用が延長されてしまわないように。
                if o_ytd_t != settings[ytd_t_key]:
                    set_setting(ytd_year_t_key, today_jst().year)
                if o_ytd_g != settings[ytd_g_key]:
                    set_setting(ytd_year_g_key, today_jst().year)
                set_setting(ytd_t_key, o_ytd_t)
                set_setting(life_t_key, o_life_t)
                set_setting(ytd_g_key, o_ytd_g)
                set_setting(life_g_key, o_life_g)
            for income_owner, income_value in annual_income_inputs.items():
                set_setting(income_keys[income_owner], income_value)
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
                    if confirm_delete(":material/delete:", key=f"del_account_{row.id}"):
                        usage = get_account_usage(row.id)
                        in_use = [
                            label
                            for label, count in (
                                (f"取引{usage['transactions']}件", usage["transactions"]),
                                (f"固定費{usage['recurring_expenses']}件", usage["recurring_expenses"]),
                                (f"定期積立{usage['recurring_investments']}件", usage["recurring_investments"]),
                            )
                            if count
                        ]
                        if in_use:
                            st.error(
                                f"この口座は{'・'.join(in_use)}で使用中のため削除できません。"
                                "削除すると過去の履歴の所有者が「世帯共通」扱いになってしまうため、"
                                "先に該当の取引・固定費・定期積立を整理するか、口座を変更してください。"
                            )
                        else:
                            try:
                                delete_account(row.id)
                                st.rerun()
                            except RecordInUseError:
                                st.error(
                                    "この口座は他で使用中のため削除できませんでした。"
                                    "画面を更新して再度お試しください。"
                                )

    if len(accounts_df) >= 1:
        st.markdown("#### 口座間の振替")
        st.caption(
            "口座間、または口座↔現金でお金を移動した場合はここから記録します"
            "（ATMでの引き出し・入金は「現金」を振替元/振替先に選んでください）。"
            "振替元の残高が減り、振替先の残高が増えます。"
        )
        with st.form("add_transfer", clear_on_submit=True):
            transfer_cols = st.columns([1, 2, 2, 2, 2])
            transfer_account_labels = build_account_labels(accounts_df)
            with transfer_cols[0]:
                transfer_date = st.date_input(
                    "日付", value=today_jst(), max_value=today_jst(), key="transfer_date"
                )
            with transfer_cols[1]:
                transfer_from_label = st.selectbox(
                    "振替元", options=list(transfer_account_labels.keys()), key="transfer_from"
                )
            with transfer_cols[2]:
                transfer_to_label = st.selectbox(
                    "振替先", options=list(transfer_account_labels.keys()), key="transfer_to"
                )
            with transfer_cols[3]:
                transfer_amount = st.number_input("金額", min_value=0, step=1000, key="transfer_amount")
            with transfer_cols[4]:
                transfer_memo = st.text_input(
                    "メモ", placeholder="メモ（任意）", key="transfer_memo"
                )

            if st.form_submit_button("振替を記録", type="primary"):
                from_id = transfer_account_labels[transfer_from_label]
                to_id = transfer_account_labels[transfer_to_label]
                if from_id == to_id:
                    st.error("振替元と振替先は異なるものを選んでください。")
                elif transfer_amount == 0:
                    st.error("金額を入力してください。")
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
        st.info("口座を1つ以上登録すると、口座↔現金・口座間の振替を記録できます。")


# =============================================================================
# Tab: 固定費
# =============================================================================

with tab_recurring:
    st.caption(
        "家賃やサブスク、車のローンなど毎月決まった日に発生する固定費を登録すると、"
        "その日を過ぎてからアプリを開いたタイミングで自動的に支出として記録され、指定した口座/カードの残高にも反映されます。"
    )

    with st.form("add_recurring", clear_on_submit=True):
        rec_cols = st.columns([2, 1, 1, 2, 1])
        with rec_cols[0]:
            rec_name = st.text_input("名称", placeholder="例: 家賃、サブスク、車のローン")
        with rec_cols[1]:
            rec_category = st.selectbox("カテゴリ", options=expense_categories, key="rec_category")
        with rec_cols[2]:
            rec_amount = st.number_input("金額", min_value=0, step=100, key="rec_amount")
        with rec_cols[3]:
            rec_account_labels = build_account_labels(accounts_df)
            rec_account_label = st.selectbox(
                "引き落とし口座/カード", options=list(rec_account_labels.keys()), key="rec_account"
            )
        with rec_cols[4]:
            rec_day = st.number_input("引き落とし日", min_value=1, max_value=28, value=27, step=1, key="rec_day")

        st.markdown("###### 支払い終了日・ボーナス払い（車のローンなど、任意）")
        st.caption("支払いが終わる時期が決まっている場合や、ボーナス月に増額返済がある場合に設定してください。")
        end_cols = st.columns(2)
        with end_cols[0]:
            rec_end_year = st.number_input(
                "支払い終了年（西暦）", min_value=0, max_value=2200, step=1, value=0, key="rec_end_year"
            )
        with end_cols[1]:
            rec_end_month = st.number_input(
                "支払い終了月", min_value=0, max_value=12, step=1, value=0, key="rec_end_month"
            )

        bonus_cols = st.columns(3)
        with bonus_cols[0]:
            rec_bonus_amount = st.number_input(
                "ボーナス月の加算返済額", min_value=0, step=1000, value=0, key="rec_bonus_amount"
            )
        with bonus_cols[1]:
            rec_bonus_month_1_label = st.selectbox(
                "ボーナス月1", options=MONTH_SELECT_OPTIONS, index=6, key="rec_bonus_month_1"
            )
        with bonus_cols[2]:
            rec_bonus_month_2_label = st.selectbox(
                "ボーナス月2", options=MONTH_SELECT_OPTIONS, index=12, key="rec_bonus_month_2"
            )

        if st.form_submit_button("登録", type="primary"):
            if rec_name.strip():
                bonus_month_1 = parse_month_label(rec_bonus_month_1_label)
                bonus_month_2 = parse_month_label(rec_bonus_month_2_label)
                add_recurring_expense(
                    rec_name.strip(),
                    rec_category,
                    rec_amount,
                    rec_account_labels[rec_account_label],
                    int(rec_day),
                    end_year=int(rec_end_year) or None,
                    end_month=int(rec_end_month) or None,
                    bonus_amount=rec_bonus_amount,
                    bonus_month_1=bonus_month_1,
                    bonus_month_2=bonus_month_2,
                )
                st.rerun()
            else:
                st.error("名称を入力してください。")

    st.markdown("#### 登録済みの固定費")
    if recurring_df.empty:
        st.info("まだ固定費が登録されていません。")
    else:
        for row in recurring_df.itertuples():
            editing_key = f"editing_recurring_{row.id}"

            if st.session_state.get(editing_key):
                with st.form(f"edit_recurring_{row.id}"):
                    edit_cols = st.columns([2, 1, 1, 2, 1])
                    with edit_cols[0]:
                        e_name = st.text_input("名称", value=row.name, key=f"e_name_{row.id}")
                    with edit_cols[1]:
                        e_category = st.selectbox(
                            "カテゴリ",
                            options=expense_categories,
                            index=safe_index(expense_categories, row.category),
                            key=f"e_category_{row.id}",
                        )
                    with edit_cols[2]:
                        e_amount = st.number_input(
                            "金額", min_value=0, step=100, value=int(row.amount), key=f"e_amount_{row.id}"
                        )
                    with edit_cols[3]:
                        e_account_labels = build_account_labels(accounts_df)
                        current_account_id = None if pd.isna(row.account_id) else int(row.account_id)
                        e_account_label = st.selectbox(
                            "引き落とし口座/カード",
                            options=list(e_account_labels.keys()),
                            index=account_select_index(e_account_labels, current_account_id),
                            key=f"e_account_{row.id}",
                        )
                    with edit_cols[4]:
                        e_day = st.number_input(
                            "引き落とし日",
                            min_value=1,
                            max_value=28,
                            value=int(row.day_of_month),
                            step=1,
                            key=f"e_day_{row.id}",
                        )

                    e_effective_from = st.date_input(
                        "金額変更の適用開始日",
                        value=today_jst(),
                        help=(
                            "金額またはボーナス加算額を変更した場合、この日付より前の月は元の金額のまま、"
                            "この日付以降の月（既に記帳済みの取引も含めて自動的に金額が更新されます）は"
                            "新しい金額が使われます。実際に値上げ・値下げされた月の1日を指定してください。"
                        ),
                        key=f"e_effective_from_{row.id}",
                    )

                    st.caption("支払い終了日・ボーナス払い（任意）")
                    e_end_cols = st.columns(2)
                    with e_end_cols[0]:
                        e_end_year = st.number_input(
                            "支払い終了年（西暦）",
                            min_value=0,
                            max_value=2200,
                            step=1,
                            value=int(row.end_year) if pd.notna(row.end_year) else 0,
                            key=f"e_end_year_{row.id}",
                        )
                    with e_end_cols[1]:
                        e_end_month = st.number_input(
                            "支払い終了月",
                            min_value=0,
                            max_value=12,
                            step=1,
                            value=int(row.end_month) if pd.notna(row.end_month) else 0,
                            key=f"e_end_month_{row.id}",
                        )

                    e_bonus_cols = st.columns(3)
                    with e_bonus_cols[0]:
                        e_bonus_amount = st.number_input(
                            "ボーナス月の加算返済額",
                            min_value=0,
                            step=1000,
                            value=int(row.bonus_amount) if pd.notna(row.bonus_amount) else 0,
                            key=f"e_bonus_amount_{row.id}",
                        )
                    with e_bonus_cols[1]:
                        e_bonus_month_1_label = st.selectbox(
                            "ボーナス月1",
                            options=MONTH_SELECT_OPTIONS,
                            index=month_select_index(row.bonus_month_1),
                            key=f"e_bonus_month_1_{row.id}",
                        )
                    with e_bonus_cols[2]:
                        e_bonus_month_2_label = st.selectbox(
                            "ボーナス月2",
                            options=MONTH_SELECT_OPTIONS,
                            index=month_select_index(row.bonus_month_2),
                            key=f"e_bonus_month_2_{row.id}",
                        )

                    save_cols = st.columns(2)
                    with save_cols[0]:
                        save_clicked = st.form_submit_button("保存", type="primary")
                    with save_cols[1]:
                        cancel_clicked = st.form_submit_button("キャンセル")

                    if save_clicked:
                        if e_name.strip():
                            e_bonus_month_1 = parse_month_label(e_bonus_month_1_label)
                            e_bonus_month_2 = parse_month_label(e_bonus_month_2_label)
                            update_recurring_expense(
                                row.id,
                                e_name.strip(),
                                e_category,
                                e_amount,
                                e_account_labels[e_account_label],
                                int(e_day),
                                end_year=int(e_end_year) or None,
                                end_month=int(e_end_month) or None,
                                bonus_amount=e_bonus_amount,
                                bonus_month_1=e_bonus_month_1,
                                bonus_month_2=e_bonus_month_2,
                                change_effective_from=e_effective_from,
                            )
                            del st.session_state[editing_key]
                            st.rerun()
                        else:
                            st.error("名称を入力してください。")
                    if cancel_clicked:
                        del st.session_state[editing_key]
                        st.rerun()
            else:
                row_cols = st.columns([2, 1, 1, 2, 1, 1])
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
                    if st.button(":material/edit:", key=f"edit_recurring_{row.id}", type="tertiary"):
                        st.session_state[editing_key] = True
                        st.rerun()
                with row_cols[5]:
                    if confirm_delete(":material/delete:", key=f"del_recurring_{row.id}"):
                        delete_recurring_expense(row.id)
                        st.rerun()

                detail_parts = []
                if pd.notna(row.end_year) and row.end_year:
                    end_label = f"{int(row.end_year)}年"
                    if pd.notna(row.end_month) and row.end_month:
                        end_label += f"{int(row.end_month)}月"
                    detail_parts.append(f"支払い終了: {end_label}")
                if pd.notna(row.bonus_amount) and row.bonus_amount:
                    bonus_months = [
                        f"{int(m)}月" for m in (row.bonus_month_1, row.bonus_month_2) if pd.notna(m) and m
                    ]
                    detail_parts.append(f"ボーナス加算: ¥{row.bonus_amount:,.0f}（{', '.join(bonus_months)}）")
                if detail_parts:
                    st.caption(" / ".join(detail_parts))

    expense_skips_df = get_recurring_expense_skips()
    if not expense_skips_df.empty:
        st.markdown("---")
        st.markdown("#### スキップ中の月")
        st.caption(
            "自動記帳された取引を削除すると、その月は「意図的にスキップした」として記録され、"
            "以後自動的には再記帳されません。誤って削除した場合はここから再記帳できます。"
        )
        for skip in expense_skips_df.itertuples():
            skip_cols = st.columns([3, 1])
            with skip_cols[0]:
                st.write(f"{skip.name}: {int(skip.year)}年{int(skip.month)}月")
            with skip_cols[1]:
                if st.button(
                    "再記帳する",
                    key=f"resume_recurring_expense_{skip.recurring_expense_id}_{skip.year}_{skip.month}",
                    type="tertiary",
                ):
                    resume_recurring_expense(skip.recurring_expense_id, int(skip.year), int(skip.month))
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
                int(today_jst().year - husband_birth_year_setting) if husband_birth_year_setting > 0 else 35
            )
            husband_age = st.number_input(
                "夫の現在の年齢", min_value=0, max_value=120, step=1, value=husband_age_default
            )
        with base_cols[1]:
            wife_birth_year_setting = settings[SETTING_WIFE_BIRTH_YEAR]
            wife_age_default = (
                int(today_jst().year - wife_birth_year_setting) if wife_birth_year_setting > 0 else 35
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
            set_setting(SETTING_HUSBAND_BIRTH_YEAR, today_jst().year - husband_age)
            set_setting(SETTING_WIFE_BIRTH_YEAR, today_jst().year - wife_age)
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
                add_child(child_name.strip() or None, today_jst().year - child_age)
                st.rerun()

    children_df = get_children()
    if not children_df.empty:
        for row in children_df.itertuples():
            row_cols = st.columns([2, 1, 1])
            with row_cols[0]:
                st.write(row.name if pd.notna(row.name) else "(名前未設定)")
            with row_cols[1]:
                st.write(f"{today_jst().year - row.birth_year}歳")
            with row_cols[2]:
                if confirm_delete(":material/delete:", key=f"del_child_{row.id}"):
                    delete_child(row.id)
                    st.rerun()

    st.markdown("---")
    st.markdown("#### 将来の支出・年金収入シミュレーション")

    if settings[SETTING_ANNUAL_EXPENSE_TARGET] > 0:
        base_annual_expense = settings[SETTING_ANNUAL_EXPENSE_TARGET]
    else:
        recent_cutoff = pd.Timestamp(today_jst()) - pd.Timedelta(days=365)
        base_annual_expense = transactions_to_date.loc[
            (transactions_to_date["type"] == "expense") & (transactions_to_date["date"] >= recent_cutoff),
            "amount",
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
