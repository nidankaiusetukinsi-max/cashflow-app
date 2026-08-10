"""PostgreSQL (Neon) persistence for the cash flow tracker."""

import functools
from datetime import date as date_

import pandas as pd
import psycopg2
import psycopg2.extensions
import streamlit as st

INCOME_CATEGORIES = ["副業", "投資", "その他収入"]
EXPENSE_CATEGORIES = ["食費", "住居", "光熱費", "交通", "娯楽", "医療", "育児", "その他支出"]
NISA_CATEGORIES = ["つみたて投資枠", "成長投資枠"]

OWNERS = ["夫", "嫁"]
ACCOUNT_KINDS = {"bank": "銀行口座", "card": "クレジットカード"}

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
SETTING_ANNUAL_INCOME = "annual_income"
SETTING_ANNUAL_EXPENSE_TARGET = "annual_expense_target"

# ライフプラン（将来予測）関連のキー。
SETTING_HUSBAND_BIRTH_YEAR = "husband_birth_year"
SETTING_WIFE_BIRTH_YEAR = "wife_birth_year"
SETTING_INFLATION_RATE = "inflation_rate"
SETTING_MORTGAGE_MONTHLY_PAYMENT = "mortgage_monthly_payment"
SETTING_MORTGAGE_PAYOFF_YEAR = "mortgage_payoff_year"
SETTING_HUSBAND_RETIREMENT_AGE = "husband_retirement_age"
SETTING_WIFE_RETIREMENT_AGE = "wife_retirement_age"
SETTING_HUSBAND_PENSION_START_AGE = "husband_pension_start_age"
SETTING_HUSBAND_PENSION_ANNUAL = "husband_pension_annual"
SETTING_WIFE_PENSION_START_AGE = "wife_pension_start_age"
SETTING_WIFE_PENSION_ANNUAL = "wife_pension_annual"
SETTING_CHILDCARE_ANNUAL_COST = "childcare_annual_cost"
SETTING_CHILDCARE_END_AGE = "childcare_end_age"

SETTINGS_KEYS = [
    SETTING_INITIAL_CASH,
    SETTING_TSUMITATE_LIFETIME_BEFORE,
    SETTING_GROWTH_LIFETIME_BEFORE,
    SETTING_TSUMITATE_YTD_BEFORE,
    SETTING_GROWTH_YTD_BEFORE,
    SETTING_ANNUAL_INCOME,
    SETTING_ANNUAL_EXPENSE_TARGET,
    SETTING_HUSBAND_BIRTH_YEAR,
    SETTING_WIFE_BIRTH_YEAR,
    SETTING_INFLATION_RATE,
    SETTING_MORTGAGE_MONTHLY_PAYMENT,
    SETTING_MORTGAGE_PAYOFF_YEAR,
    SETTING_HUSBAND_RETIREMENT_AGE,
    SETTING_WIFE_RETIREMENT_AGE,
    SETTING_HUSBAND_PENSION_START_AGE,
    SETTING_HUSBAND_PENSION_ANNUAL,
    SETTING_WIFE_PENSION_START_AGE,
    SETTING_WIFE_PENSION_ANNUAL,
    SETTING_CHILDCARE_ANNUAL_COST,
    SETTING_CHILDCARE_END_AGE,
]


@st.cache_resource
def get_connection() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(st.secrets["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                owner TEXT NOT NULL CHECK (owner IN ('夫', '嫁')),
                name TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('bank', 'card')),
                initial_balance DOUBLE PRECISION NOT NULL DEFAULT 0
            )
            """
        )
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
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS "
            "account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL"
        )
        cur.execute(
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS "
            "to_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_expenses (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                day_of_month INTEGER NOT NULL CHECK (day_of_month BETWEEN 1 AND 28)
            )
            """
        )
        cur.execute(
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS "
            "recurring_expense_id INTEGER REFERENCES recurring_expenses(id) ON DELETE SET NULL"
        )
        # 既存DBの type CHECK 制約に 'transfer' を許可するよう毎回冪等に更新する。
        cur.execute("ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_type_check")
        cur.execute(
            "ALTER TABLE transactions ADD CONSTRAINT transactions_type_check "
            "CHECK (type IN ('income', 'expense', 'investment', 'transfer'))"
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS children (
                id SERIAL PRIMARY KEY,
                name TEXT,
                birth_year INTEGER NOT NULL
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
def add_transaction(
    date: str, type_: str, category: str, amount: float, memo: str, account_id: int | None = None
) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions (date, type, category, amount, memo, account_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (date, type_, category, amount, memo, account_id),
        )
    conn.commit()


@_with_reconnect
def get_transactions() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, date, type, category, amount, memo, account_id, to_account_id "
        "FROM transactions ORDER BY date DESC, id DESC",
        conn,
    )
    df["date"] = pd.to_datetime(df["date"])
    return df


@_with_reconnect
def add_transfer(date: str, from_account_id: int, to_account_id: int, amount: float, memo: str) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions (date, type, category, amount, memo, account_id, to_account_id) "
            "VALUES (%s, 'transfer', '振替', %s, %s, %s, %s)",
            (date, amount, memo, from_account_id, to_account_id),
        )
    conn.commit()


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


@_with_reconnect
def add_account(owner: str, name: str, kind: str, initial_balance: float) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (owner, name, kind, initial_balance) VALUES (%s, %s, %s, %s)",
            (owner, name, kind, initial_balance),
        )
    conn.commit()


@_with_reconnect
def delete_account(account_id: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
    conn.commit()


@_with_reconnect
def get_accounts() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, owner, name, kind, initial_balance FROM accounts ORDER BY owner, id",
        conn,
    )
    return df


@_with_reconnect
def add_recurring_expense(
    name: str, category: str, amount: float, account_id: int | None, day_of_month: int
) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recurring_expenses (name, category, amount, account_id, day_of_month) "
            "VALUES (%s, %s, %s, %s, %s)",
            (name, category, amount, account_id, day_of_month),
        )
    conn.commit()


@_with_reconnect
def delete_recurring_expense(recurring_id: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM recurring_expenses WHERE id = %s", (recurring_id,))
    conn.commit()


@_with_reconnect
def get_recurring_expenses() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, name, category, amount, account_id, day_of_month "
        "FROM recurring_expenses ORDER BY day_of_month, id",
        conn,
    )
    return df


@_with_reconnect
def apply_recurring_expenses() -> None:
    """Insert this month's expense transaction for each recurring expense once its day has passed."""
    conn = get_connection()
    today = date_.today()
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, category, amount, account_id, day_of_month FROM recurring_expenses")
        rows = cur.fetchall()
        for rec_id, name, category, amount, account_id, day_of_month in rows:
            if today.day < day_of_month:
                continue
            cur.execute(
                "SELECT 1 FROM transactions WHERE recurring_expense_id = %s "
                "AND EXTRACT(YEAR FROM date) = %s AND EXTRACT(MONTH FROM date) = %s LIMIT 1",
                (rec_id, today.year, today.month),
            )
            if cur.fetchone():
                continue
            applied_date = date_(today.year, today.month, day_of_month)
            cur.execute(
                "INSERT INTO transactions (date, type, category, amount, memo, account_id, recurring_expense_id) "
                "VALUES (%s, 'expense', %s, %s, %s, %s, %s)",
                (applied_date, category, amount, f"{name}（固定費自動引き落とし）", account_id, rec_id),
            )
    conn.commit()


@_with_reconnect
def add_child(name: str | None, birth_year: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO children (name, birth_year) VALUES (%s, %s)", (name, birth_year))
    conn.commit()


@_with_reconnect
def delete_child(child_id: int) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM children WHERE id = %s", (child_id,))
    conn.commit()


@_with_reconnect
def get_children() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, name, birth_year FROM children ORDER BY birth_year DESC, id", conn
    )
    return df
