"""PostgreSQL (Neon) persistence for the cash flow tracker."""

import functools

import pandas as pd
import psycopg2
import psycopg2.extensions
import streamlit as st

INCOME_CATEGORIES = ["給与", "副業", "投資", "その他収入"]
EXPENSE_CATEGORIES = ["食費", "住居", "光熱費", "交通", "娯楽", "医療", "育児", "その他支出"]
NISA_CATEGORIES = ["つみたて投資枠", "成長投資枠"]

# 2024年開始の新NISA制度の上限額。生涯枠は簿価残高方式（売却で枠が復活する）だが、
# ここでは単純に「これまでの拠出累計」で消化率を近似する。
NISA_TSUMITATE_ANNUAL_LIMIT = 1_200_000
NISA_GROWTH_ANNUAL_LIMIT = 2_400_000
NISA_LIFETIME_LIMIT = 18_000_000
NISA_GROWTH_LIFETIME_LIMIT = 12_000_000

# 初期設定（アプリ利用開始前からの既存残高）のキー。
SETTING_INITIAL_CASH = "initial_cash"
SETTING_TSUMITATE_LIFETIME_BEFORE = "tsumitate_lifetime_before"
SETTING_GROWTH_LIFETIME_BEFORE = "growth_lifetime_before"
SETTING_TSUMITATE_YTD_BEFORE = "tsumitate_ytd_before"
SETTING_GROWTH_YTD_BEFORE = "growth_ytd_before"

SETTINGS_KEYS = [
    SETTING_INITIAL_CASH,
    SETTING_TSUMITATE_LIFETIME_BEFORE,
    SETTING_GROWTH_LIFETIME_BEFORE,
    SETTING_TSUMITATE_YTD_BEFORE,
    SETTING_GROWTH_YTD_BEFORE,
]


@st.cache_resource
def get_connection() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(st.secrets["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('income', 'expense', 'investment')),
                category TEXT NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                memo TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                category TEXT PRIMARY KEY,
                monthly_limit DOUBLE PRECISION NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value DOUBLE PRECISION NOT NULL
            )
            """
        )
    conn.commit()
    return conn


def _with_reconnect(func):
    """Retry once with a fresh connection if Neon closed the cached one while idle."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            get_connection.clear()
            return func(*args, **kwargs)

    return wrapper


@_with_reconnect
def add_transaction(date: str, type_: str, category: str, amount: float, memo: str) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions (date, type, category, amount, memo) VALUES (%s, %s, %s, %s, %s)",
            (date, type_, category, amount, memo),
        )
    conn.commit()


@_with_reconnect
def get_transactions() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, date, type, category, amount, memo FROM transactions ORDER BY date DESC, id DESC",
        conn,
    )
    df["date"] = pd.to_datetime(df["date"])
    return df


@_with_reconnect
def delete_transactions(ids: list[int]) -> None:
    if not ids:
        return
    conn = get_connection()
    with conn.cursor() as cur:
        placeholders = ",".join("%s" for _ in ids)
        cur.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)
    conn.commit()


@_with_reconnect
def set_budget(category: str, monthly_limit: float) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO budgets (category, monthly_limit) VALUES (%s, %s) "
            "ON CONFLICT (category) DO UPDATE SET monthly_limit = excluded.monthly_limit",
            (category, monthly_limit),
        )
    conn.commit()


@_with_reconnect
def delete_budget(category: str) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM budgets WHERE category = %s", (category,))
    conn.commit()


@_with_reconnect
def get_budgets() -> dict[str, float]:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT category, monthly_limit FROM budgets")
        rows = cur.fetchall()
    return dict(rows)


@_with_reconnect
def set_setting(key: str, value: float) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()


@_with_reconnect
def get_settings() -> dict[str, float]:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        rows = cur.fetchall()
    values = dict(rows)
    return {key: values.get(key, 0.0) for key in SETTINGS_KEYS}
