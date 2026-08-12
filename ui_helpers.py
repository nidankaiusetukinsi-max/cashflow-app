"""Pure helper functions for streamlit_app.py's forms and dashboard filtering.

Split out from streamlit_app.py so this logic can be unit-tested directly: importing
streamlit_app.py itself isn't practical for tests since it executes Streamlit UI/DB calls
(st.set_page_config, the password gate, st.stop(), ...) at module import time. Mirrors the
same rationale as aggregates.py/advice.py/forecast.py.
"""

from datetime import date

import pandas as pd

from timeutil import today_jst

ACCOUNT_KINDS_CASH_LABEL = "現金"

TIME_RANGES = ["1ヶ月", "6ヶ月", "1年", "今年", "すべて"]

MONTH_SELECT_OPTIONS = ["なし"] + [f"{m}月" for m in range(1, 13)]


def time_range_start(df: pd.DataFrame, time_range: str, today: pd.Timestamp) -> pd.Timestamp | None:
    """The lower date bound implied by a dashboard time-range choice, or None if unbounded.

    For "すべて" there's no fixed lower bound, but callers that need to prorate a period-based
    figure (e.g. salary) still need *some* start date, so this returns the earliest transaction
    date in `df` for that case instead of None when data exists.
    """
    if time_range == "1ヶ月":
        # 単純な日数(30日)ではなく暦月で1ヶ月前を計算する(月末日は自動的にクランプされる)。
        return today - pd.DateOffset(months=1)
    if time_range == "6ヶ月":
        return today - pd.DateOffset(months=6)
    if time_range == "1年":
        return today - pd.DateOffset(years=1)
    if time_range == "今年":
        return pd.Timestamp(date(today.year, 1, 1))
    if time_range == "すべて":
        return df["date"].min() if not df.empty else None
    return None


def filter_by_time_range(df: pd.DataFrame, time_range: str) -> pd.DataFrame:
    if time_range == "すべて" or df.empty:
        return df

    # 「今日」を基準にする。最新取引日を基準にすると、未来日の取引が1件でもあると
    # 集計期間全体がずれ、直近に記帳が無い年は「今年」なのに去年のデータが出てしまう。
    today = pd.Timestamp(today_jst())
    min_date = time_range_start(df, time_range, today)
    if min_date is None:
        return df
    filtered: pd.DataFrame = df[df["date"] >= min_date]
    return filtered


def resolved_default(key: str, legacy: int, present_keys: set[str], settings_: dict[str, float]) -> int:
    """A form's pre-filled default: the saved value if it was ever explicitly set, else a legacy fallback.

    get_settings() can't distinguish "never saved" from "saved as 0" (both come back as 0.0),
    so a plain `int(settings[key]) or legacy` would keep resurrecting the legacy value every time
    a user deliberately resets a field to 0 (e.g. the yearly NISA YTD reset). present_keys (from
    get_present_setting_keys()) makes that distinction explicit.
    """
    return int(settings_[key]) if key in present_keys else legacy


def build_account_labels(accounts: pd.DataFrame, account_kinds: dict[str, str]) -> dict[str, int | None]:
    """Selectbox options for "which account/card": '現金' (cash, no account) plus every registered account."""
    labels: dict[str, int | None] = {ACCOUNT_KINDS_CASH_LABEL: None}
    for row in accounts.itertuples():
        labels[f"{row.owner}: {row.name}（{account_kinds[row.kind]}）"] = row.id
    return labels


def safe_index(options: list, value, default: int = 0) -> int:
    """options.index(value), or `default` if value isn't among options.

    Used to pre-fill a selectbox's index from stored data that may reference a value no
    longer in the current option list (a deleted category, a legacy blank owner, ...).
    """
    return options.index(value) if value in options else default


def account_select_index(account_labels: dict[str, int | None], current_account_id: int | None) -> int:
    """Position of `current_account_id` among build_account_labels()'s values, for
    pre-filling an edit form's account selectbox."""
    return next(
        (i for i, acc_id in enumerate(account_labels.values()) if acc_id == current_account_id), 0
    )


def month_select_index(month: int | None) -> int:
    """Index into MONTH_SELECT_OPTIONS for a stored bonus month (None/0 -> "なし")."""
    return int(month) if pd.notna(month) and month else 0


def parse_month_label(label: str) -> int | None:
    return None if label == "なし" else int(label.replace("月", ""))


def category_edit_options(categories: list[str], current_category: str) -> tuple[list[str], int]:
    """Selectbox (options, index) for editing an existing transaction's category.

    If `current_category` was since deleted from the category list, plain safe_index() would
    silently fall back to index 0 - the FIRST remaining category - so an unsuspecting user who
    just clicks "保存" without noticing would have that transaction's category silently
    rewritten to something unrelated. Instead, keep the (now-orphaned) category visible and
    selected as its own option, so nothing changes unless the user deliberately picks something
    else. Callers should show a warning near the selectbox when the returned index is the
    injected 0 for a category not in `categories`.
    """
    if current_category in categories:
        return categories, categories.index(current_category)
    return [current_category, *categories], 0


def progress_ratio(value: float, limit: float) -> float:
    """value/limit clamped to [0.0, 1.0], safe to pass straight into st.progress().

    NISA contribution totals can go negative (a recorded sale/withdrawal can outweigh the
    contributions made so far - see the "投資(NISA)" amount fields in streamlit_app.py), and
    st.progress() raises StreamlitAPIException for anything outside [0, 1] - which would take
    down the whole rerun, not just this one progress bar. A plain `min(value/limit, 1.0)` only
    guards the upper bound.
    """
    return max(0.0, min(value / limit, 1.0))


def csv_safe_value(value):
    """Prefix a leading apostrophe onto free-text values that Excel/Sheets would otherwise
    interpret as a formula when opening a CSV export (=, +, -, @ at the start of a cell).

    Applies to user-entered memo text specifically: it's the one exported column that isn't
    drawn from a controlled vocabulary (category/type/owner are all from fixed lists).
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value
