"""SQLite persistence for the cash flow tracker."""

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "cashflow.db"

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


def _migrate_transactions_type_check(conn: sqlite3.Connection) -> None:
    """Widen the legacy `type IN ('income', 'expense')` check to include 'investment'."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='transactions'"
    ).fetchone()
    if row is None or "'investment'" in row[0]:
        return
    conn.execute("ALTER TABLE transactions RENAME TO transactions_old")
    conn.execute(
        """
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('income', 'expense', 'investment')),
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            memo TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO transactions (id, date, type, category, amount, memo) "
        "SELECT id, date, type, category, amount, memo FROM transactions_old"
    )
    conn.execute("DROP TABLE transactions_old")
    conn.commit()


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('income', 'expense', 'investment')),
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            memo TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (
            category TEXT PRIMARY KEY,
            monthly_limit REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL
        )
        """
    )
    conn.commit()
    _migrate_transactions_type_check(conn)
    return conn


def add_transaction(date: str, type_: str, category: str, amount: float, memo: str) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO transactions (date, type, category, amount, memo) VALUES (?, ?, ?, ?, ?)",
        (date, type_, category, amount, memo),
    )
    conn.commit()


def get_transactions() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, date, type, category, amount, memo FROM transactions ORDER BY date DESC, id DESC",
        conn,
        parse_dates=["date"],
    )
    return df


def delete_transactions(ids: list[int]) -> None:
    if not ids:
        return
    conn = get_connection()
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)
    conn.commit()


def set_budget(category: str, monthly_limit: float) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO budgets (category, monthly_limit) VALUES (?, ?) "
        "ON CONFLICT(category) DO UPDATE SET monthly_limit = excluded.monthly_limit",
        (category, monthly_limit),
    )
    conn.commit()


def delete_budget(category: str) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM budgets WHERE category = ?", (category,))
    conn.commit()


def get_budgets() -> dict[str, float]:
    conn = get_connection()
    rows = conn.execute("SELECT category, monthly_limit FROM budgets").fetchall()
    return dict(rows)


def set_setting(key: str, value: float) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_settings() -> dict[str, float]:
    conn = get_connection()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    values = dict(rows)
    return {key: values.get(key, 0.0) for key in SETTINGS_KEYS}
