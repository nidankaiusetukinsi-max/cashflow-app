"""PostgreSQL (Neon) persistence for the cash flow tracker."""

import functools
import time
from contextlib import contextmanager
from datetime import date as date_

import pandas as pd
import psycopg2
import psycopg2.extensions
import psycopg2.pool
import streamlit as st

from timeutil import today_jst


class RecordInUseError(Exception):
    """A delete was refused because the record is still referenced elsewhere.

    `usage` maps a human-readable reference kind (e.g. "transactions") to how many rows
    reference it, so callers can build a specific message without a second round trip.
    """

    def __init__(self, usage: dict[str, int]):
        self.usage = usage
        super().__init__(f"record still in use: {usage}")


class CategoryNameTakenError(Exception):
    """A rename was refused because a category with the target name already exists.

    Renaming into an existing name would require merging two categories' histories
    (transactions, budgets, recurring expenses) into one, which rename_expense_category/
    rename_income_category do not attempt - the caller should ask the user to pick a
    different name, or delete the unused category first if that's really the intent.
    """

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"category name already exists: {name}")


class ReservedCategoryNameError(Exception):
    """An income category was refused because its name is reserved for salary tracking.

    Salary is tracked exclusively via the annual-income settings (SETTING_ANNUAL_INCOME_
    HUSBAND/WIFE, prorated into dashboard/advice income totals) specifically so it is never
    double-entered as a transaction too. A free-text income category named e.g. "給与" would
    invite exactly that: a user logging real paychecks under it on top of the prorated salary
    already baked into every income total. Blocking the obvious names at creation time backs
    up the existing UI warning text with an actual guard.
    """

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"reserved category name: {name}")


RESERVED_INCOME_CATEGORY_NAMES = {"給与", "給料"}

DEFAULT_INCOME_CATEGORIES = ["副業", "投資", "その他収入"]
DEFAULT_EXPENSE_CATEGORIES = ["食費", "住居", "光熱費", "交通", "娯楽", "医療", "育児", "その他支出"]
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
SETTING_ANNUAL_INCOME_HUSBAND = "annual_income_husband"
SETTING_ANNUAL_INCOME_WIFE = "annual_income_wife"

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

# NISA拠出額（夫婦それぞれ）。非課税枠は一人ずつに割り当てられるため個別に管理する。
SETTING_TSUMITATE_YTD_BEFORE_HUSBAND = "tsumitate_ytd_before_husband"
SETTING_TSUMITATE_YTD_BEFORE_WIFE = "tsumitate_ytd_before_wife"
SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND = "tsumitate_lifetime_before_husband"
SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE = "tsumitate_lifetime_before_wife"
SETTING_GROWTH_YTD_BEFORE_HUSBAND = "growth_ytd_before_husband"
SETTING_GROWTH_YTD_BEFORE_WIFE = "growth_ytd_before_wife"
SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND = "growth_lifetime_before_husband"
SETTING_GROWTH_LIFETIME_BEFORE_WIFE = "growth_lifetime_before_wife"

# 上記YTD拠出額が「どの年の分か」。年をまたいでも古い値を足し続けないよう、
# 保存時の年と一致する場合にのみ有効とする（advice.pyのeffective_nisa_ytd_before参照）。
SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND = "tsumitate_ytd_before_year_husband"
SETTING_TSUMITATE_YTD_BEFORE_YEAR_WIFE = "tsumitate_ytd_before_year_wife"
SETTING_GROWTH_YTD_BEFORE_YEAR_HUSBAND = "growth_ytd_before_year_husband"
SETTING_GROWTH_YTD_BEFORE_YEAR_WIFE = "growth_ytd_before_year_wife"

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
    SETTING_TSUMITATE_YTD_BEFORE_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_WIFE,
    SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND,
    SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE,
    SETTING_GROWTH_YTD_BEFORE_HUSBAND,
    SETTING_GROWTH_YTD_BEFORE_WIFE,
    SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND,
    SETTING_GROWTH_LIFETIME_BEFORE_WIFE,
    SETTING_ANNUAL_INCOME_HUSBAND,
    SETTING_ANNUAL_INCOME_WIFE,
    SETTING_TSUMITATE_YTD_BEFORE_YEAR_HUSBAND,
    SETTING_TSUMITATE_YTD_BEFORE_YEAR_WIFE,
    SETTING_GROWTH_YTD_BEFORE_YEAR_HUSBAND,
    SETTING_GROWTH_YTD_BEFORE_YEAR_WIFE,
]


def _run_migrations(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                owner TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('bank', 'card')),
                initial_balance INTEGER NOT NULL DEFAULT 0
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
                amount INTEGER NOT NULL,
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
        cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS owner TEXT")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_investments (
                id SERIAL PRIMARY KEY,
                owner TEXT NOT NULL,
                category TEXT NOT NULL CHECK (category IN ('つみたて投資枠', '成長投資枠')),
                amount INTEGER NOT NULL,
                account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                day_of_month INTEGER NOT NULL CHECK (day_of_month BETWEEN 1 AND 28)
            )
            """
        )
        cur.execute(
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS "
            "recurring_investment_id INTEGER REFERENCES recurring_investments(id) ON DELETE SET NULL"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_expenses (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                amount INTEGER NOT NULL,
                account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                day_of_month INTEGER NOT NULL CHECK (day_of_month BETWEEN 1 AND 28)
            )
            """
        )
        # 車のローンなど、支払いが終わる時期が決まっている固定費・ボーナス月の増額払いに対応。
        cur.execute("ALTER TABLE recurring_expenses ADD COLUMN IF NOT EXISTS end_year INTEGER")
        cur.execute("ALTER TABLE recurring_expenses ADD COLUMN IF NOT EXISTS end_month INTEGER")
        cur.execute(
            "ALTER TABLE recurring_expenses ADD COLUMN IF NOT EXISTS "
            "bonus_amount INTEGER NOT NULL DEFAULT 0"
        )
        cur.execute("ALTER TABLE recurring_expenses ADD COLUMN IF NOT EXISTS bonus_month_1 INTEGER")
        cur.execute("ALTER TABLE recurring_expenses ADD COLUMN IF NOT EXISTS bonus_month_2 INTEGER")
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
        # owner は「夫」「嫁」固定のPython定数(OWNERS)側で管理する運用に寄せ、
        # 家族構成の変化に備えてDB側のCHECK制約は撤廃しておく(既存DBにも冪等に適用)。
        cur.execute("ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_owner_check")
        cur.execute("ALTER TABLE recurring_investments DROP CONSTRAINT IF EXISTS recurring_investments_owner_check")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                category TEXT PRIMARY KEY,
                monthly_limit INTEGER NOT NULL
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS expense_categories (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL
            )
            """
        )
        cur.execute("SELECT COUNT(*) FROM expense_categories")
        if cur.fetchone()[0] == 0:
            for category_name in DEFAULT_EXPENSE_CATEGORIES:
                cur.execute(
                    "INSERT INTO expense_categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                    (category_name,),
                )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS income_categories (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL
            )
            """
        )
        cur.execute("SELECT COUNT(*) FROM income_categories")
        if cur.fetchone()[0] == 0:
            for category_name in DEFAULT_INCOME_CATEGORIES:
                cur.execute(
                    "INSERT INTO income_categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                    (category_name,),
                )
        # 自動記帳された特定の月だけを「意図的に削除した」と記録し、次回のバックフィルで
        # 復活させないようにする（delete_transactions / apply_recurring_* を参照）。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_expense_skips (
                recurring_expense_id INTEGER NOT NULL REFERENCES recurring_expenses(id) ON DELETE CASCADE,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                PRIMARY KEY (recurring_expense_id, year, month)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_investment_skips (
                recurring_investment_id INTEGER NOT NULL REFERENCES recurring_investments(id) ON DELETE CASCADE,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                PRIMARY KEY (recurring_investment_id, year, month)
            )
            """
        )
        # 固定費の金額変更履歴。apply_recurring_expenses/resume_recurring_expense が過去月を
        # 記帳する際、「今の設定額」ではなく「その月時点で有効だった金額」を参照できるようにする
        # (値上げ後に過去の未記帳分をバックフィルすると、値上げ前の月まで新料金で記帳されてしまうため)。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_expense_amount_history (
                id SERIAL PRIMARY KEY,
                recurring_expense_id INTEGER NOT NULL REFERENCES recurring_expenses(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL,
                bonus_amount INTEGER NOT NULL DEFAULT 0,
                effective_from DATE NOT NULL
            )
            """
        )
        # ボーナス加算月も金額と同様に履歴として管理する。以前はrecurring_expenses側の「今の」
        # bonus_month_1/2を常に参照していたため、ボーナス月の設定を変えた後に金額を過去日付に
        # さかのぼって修正すると、reconcile処理(_reconcile_posted_expense_amounts)が過去の記帳
        # にまで「今の」ボーナス月を適用してしまい、正しく計上済みだった過去のボーナス加算額が
        # 静かに消えたり、逆に付いたりする不具合があった。金額と同じ履歴行でバージョン管理する
        # ことで、各記帳日の時点で実際に有効だったボーナス月を参照できるようにする。
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'recurring_expense_amount_history' AND column_name = 'bonus_month_1'"
        )
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE recurring_expense_amount_history ADD COLUMN bonus_month_1 INTEGER")
            cur.execute("ALTER TABLE recurring_expense_amount_history ADD COLUMN bonus_month_2 INTEGER")
            # 既存の履歴行にはボーナス月の記録がないため、移行時点の設定値でベストエフォート補完
            # する(それ以前の変更で実際に何月だったかを示す記録は残っていないため)。
            cur.execute(
                "UPDATE recurring_expense_amount_history h "
                "SET bonus_month_1 = r.bonus_month_1, bonus_month_2 = r.bonus_month_2 "
                "FROM recurring_expenses r WHERE h.recurring_expense_id = r.id"
            )
        # 既存の固定費には、その時点の設定額を「十分過去から有効」として1件だけ登録しておく。
        # こうしておけば以降は常に履歴テーブル経由で参照でき、fallback分岐を特別扱いしなくて済む。
        cur.execute(
            """
            INSERT INTO recurring_expense_amount_history
                (recurring_expense_id, amount, bonus_amount, bonus_month_1, bonus_month_2, effective_from)
            SELECT r.id, r.amount, r.bonus_amount, r.bonus_month_1, r.bonus_month_2, DATE '2000-01-01'
            FROM recurring_expenses r
            WHERE NOT EXISTS (
                SELECT 1 FROM recurring_expense_amount_history h WHERE h.recurring_expense_id = r.id
            )
            """
        )
        # 定期積立(NISA)の金額変更履歴。recurring_expense_amount_history と同じ理由で、
        # apply_recurring_investments/resume_recurring_investment が過去月をバックフィルする際に
        # 「今の設定額」ではなく「その月時点で有効だった金額」を参照できるようにする
        # (でないと、増額後に未記帳分をバックフィルすると値上げ前の月まで新額で記帳されてしまい、
        # NISA年間/生涯上限の消化率が実際の拠出額とズレてしまう)。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_investment_amount_history (
                id SERIAL PRIMARY KEY,
                recurring_investment_id INTEGER NOT NULL REFERENCES recurring_investments(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL,
                effective_from DATE NOT NULL
            )
            """
        )
        cur.execute(
            """
            INSERT INTO recurring_investment_amount_history (recurring_investment_id, amount, effective_from)
            SELECT r.id, r.amount, DATE '2000-01-01'
            FROM recurring_investments r
            WHERE NOT EXISTS (
                SELECT 1 FROM recurring_investment_amount_history h WHERE h.recurring_investment_id = r.id
            )
            """
        )
        # 金額は円単位の整数のみが入力されるため、浮動小数点の丸め誤差を避けて INTEGER に統一する
        # (settings.value だけは物価上昇率など小数値も扱う汎用キー・バリュー store なので対象外)。
        for table, column in (
            ("transactions", "amount"),
            ("accounts", "initial_balance"),
            ("recurring_expenses", "amount"),
            ("recurring_expenses", "bonus_amount"),
            ("recurring_investments", "amount"),
            ("budgets", "monthly_limit"),
        ):
            cur.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s",
                (table, column),
            )
            row = cur.fetchone()
            if row and row[0] == "double precision":
                cur.execute(
                    f"ALTER TABLE {table} ALTER COLUMN {column} TYPE INTEGER USING ROUND({column})::INTEGER"
                )
        # 定期費用/定期積立の自動記帳が同じ月に二重挿入されるのを、アプリ側のキャッシュだけでなく
        # DB制約でも防ぐ(複数プロセス構成やキャッシュクリア時の保険)。
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_recurring_expense_month "
            "ON transactions (recurring_expense_id, date_trunc('month', date::timestamp)) "
            "WHERE recurring_expense_id IS NOT NULL"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_recurring_investment_month "
            "ON transactions (recurring_investment_id, date_trunc('month', date::timestamp)) "
            "WHERE recurring_investment_id IS NOT NULL"
        )
        # ログイン試行回数のロックアウトはクライアント単位(IPアドレス等)で管理する。以前は
        # settingsテーブルの単一グローバルキーで管理していたが、それだと誰か一人が失敗し
        # 続けるだけで、リクエスト元に関わらず正規ユーザー全員をロックアウトできてしまう
        # (DoSの温床になる)ため、client_keyごとに独立した行を持つ専用テーブルに分離した。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                client_key TEXT PRIMARY KEY,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until DOUBLE PRECISION NOT NULL DEFAULT 0
            )
            """
        )
    conn.commit()


@st.cache_resource
def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """A small connection pool shared across a Streamlit process.

    Streamlit reruns each user's session in its own thread, so a single shared
    psycopg2 connection is not safe when more than one person (e.g. both spouses)
    uses the app at the same time. A pool hands each concurrent request its own
    connection instead.

    Sized at 8 (not 5) because streamlit_app.py fetches its independent top-level
    queries concurrently via a 4-worker ThreadPoolExecutor (see _load_dashboard_data) to
    cut the ~12 sequential DB round-trips a single rerun used to make (measured at ~110ms
    each against a remote Neon region) down to a few parallel batches. 8 leaves headroom
    for two people's reruns to each grab up to 4 connections at once without exhausting
    the pool (ThreadedConnectionPool.getconn raises immediately rather than waiting).
    """
    conn_pool = psycopg2.pool.ThreadedConnectionPool(1, 8, st.secrets["DATABASE_URL"])
    conn = conn_pool.getconn()
    try:
        _run_migrations(conn)
    finally:
        conn_pool.putconn(conn)
    return conn_pool


@contextmanager
def _connection():
    """Check out a pooled connection; commit on success, roll back on error.

    A connection that fails with a connection-level error (e.g. Neon closed it
    while idle) is discarded from the pool instead of being returned, so the next
    checkout gets a fresh one.
    """
    conn_pool = get_pool()
    conn = conn_pool.getconn()
    try:
        yield conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        conn_pool.putconn(conn, close=True)
        raise
    except Exception:
        conn.rollback()
        conn_pool.putconn(conn)
        raise
    else:
        conn.commit()
        conn_pool.putconn(conn)


@contextmanager
def _cursor():
    with _connection() as conn:
        with conn.cursor() as cur:
            yield cur


def _with_reconnect(func):
    """Retry once if the pooled connection turned out to be dead (e.g. Neon idle-close)."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            return func(*args, **kwargs)

    return wrapper


@_with_reconnect
def add_transaction(
    date: str,
    type_: str,
    category: str,
    amount: float,
    memo: str,
    account_id: int | None = None,
    owner: str | None = None,
) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO transactions (date, type, category, amount, memo, account_id, owner) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (date, type_, category, amount, memo, account_id, owner),
        )


@_with_reconnect
def get_transactions() -> pd.DataFrame:
    with _connection() as conn:
        df = pd.read_sql_query(
            "SELECT id, date, type, category, amount, memo, account_id, to_account_id, owner, "
            "recurring_expense_id, recurring_investment_id "
            "FROM transactions ORDER BY date DESC, id DESC",
            conn,
        )
    df["date"] = pd.to_datetime(df["date"])
    return df


@_with_reconnect
def update_transaction(
    transaction_id: int,
    date: str,
    type_: str,
    category: str,
    amount: float,
    memo: str,
    account_id: int | None,
    owner: str | None,
) -> None:
    """Edit a manually-entered transaction in place.

    Restricted (at the streamlit_app.py call site) to transactions that aren't a transfer
    and aren't auto-posted from a recurring expense/investment, since those have extra
    invariants (two-sided balance effects, or the recurring backfill's "MAX(date) already
    posted" assumption) that a free-form edit here could quietly break.
    """
    with _cursor() as cur:
        cur.execute(
            "UPDATE transactions SET date = %s, type = %s, category = %s, amount = %s, "
            "memo = %s, account_id = %s, owner = %s WHERE id = %s",
            (date, type_, category, amount, memo, account_id, owner, transaction_id),
        )


@_with_reconnect
def add_transfer(
    date: str, from_account_id: int | None, to_account_id: int | None, amount: float, memo: str
) -> None:
    """Record a transfer. Either endpoint may be None to represent cash (現金, untracked as an account)."""
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO transactions (date, type, category, amount, memo, account_id, to_account_id) "
            "VALUES (%s, 'transfer', '振替', %s, %s, %s, %s)",
            (date, amount, memo, from_account_id, to_account_id),
        )


@_with_reconnect
def delete_transactions(ids: list[int]) -> None:
    """Delete transactions, remembering any auto-generated recurring months among them.

    Without this, deleting the most recent auto-posted instance of a recurring expense/
    investment would make apply_recurring_* treat that month as "not yet applied" again
    and silently recreate it the next time the app runs.
    """
    if not ids:
        return
    with _cursor() as cur:
        placeholders = ",".join("%s" for _ in ids)
        cur.execute(
            f"SELECT recurring_expense_id, recurring_investment_id, date "
            f"FROM transactions WHERE id IN ({placeholders})",
            ids,
        )
        for rec_expense_id, rec_investment_id, txn_date in cur.fetchall():
            if rec_expense_id is not None:
                cur.execute(
                    "INSERT INTO recurring_expense_skips (recurring_expense_id, year, month) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (rec_expense_id, txn_date.year, txn_date.month),
                )
            if rec_investment_id is not None:
                cur.execute(
                    "INSERT INTO recurring_investment_skips (recurring_investment_id, year, month) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (rec_investment_id, txn_date.year, txn_date.month),
                )
        cur.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)


@_with_reconnect
def set_budget(category: str, monthly_limit: float) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO budgets (category, monthly_limit) VALUES (%s, %s) "
            "ON CONFLICT (category) DO UPDATE SET monthly_limit = excluded.monthly_limit",
            (category, monthly_limit),
        )


@_with_reconnect
def delete_budget(category: str) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM budgets WHERE category = %s", (category,))


@_with_reconnect
def get_budgets() -> dict[str, float]:
    with _cursor() as cur:
        cur.execute("SELECT category, monthly_limit FROM budgets")
        rows = cur.fetchall()
    return dict(rows)


@_with_reconnect
def set_setting(key: str, value: float) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


@_with_reconnect
def get_settings() -> dict[str, float]:
    with _cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        rows = cur.fetchall()
    values = dict(rows)
    return {key: values.get(key, 0.0) for key in SETTINGS_KEYS}


@_with_reconnect
def get_present_setting_keys() -> set[str]:
    """Which settings keys have ever been explicitly saved (even if the saved value is 0).

    get_settings() always returns a value for every key, defaulting missing ones to 0.0,
    so callers can't tell "never configured" apart from "explicitly set to 0" from that dict
    alone. UI code that pre-fills a form with a fallback default (e.g. a legacy combined value)
    needs that distinction, or a genuine 0 gets silently overwritten by the fallback every time
    the form reloads (see the NISA YTD / annual income reset flow in streamlit_app.py).
    """
    with _cursor() as cur:
        cur.execute("SELECT key FROM settings")
        rows = cur.fetchall()
    return {row[0] for row in rows}


@_with_reconnect
def add_account(owner: str, name: str, kind: str, initial_balance: float) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (owner, name, kind, initial_balance) VALUES (%s, %s, %s, %s)",
            (owner, name, kind, initial_balance),
        )


def _account_usage(cur: psycopg2.extensions.cursor, account_id: int) -> dict[str, int]:
    cur.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id = %s OR to_account_id = %s",
        (account_id, account_id),
    )
    transaction_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM recurring_expenses WHERE account_id = %s", (account_id,))
    recurring_expense_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM recurring_investments WHERE account_id = %s", (account_id,))
    recurring_investment_count = cur.fetchone()[0]
    return {
        "transactions": transaction_count,
        "recurring_expenses": recurring_expense_count,
        "recurring_investments": recurring_investment_count,
    }


@_with_reconnect
def get_account_usage(account_id: int) -> dict[str, int]:
    """Count references to an account, to warn before deleting one with history.

    accounts.id is referenced via ON DELETE SET NULL everywhere, so deleting an account
    with existing transactions doesn't fail - it silently detaches them, which folds
    that history's owner attribution into the shared/untagged cash bucket instead of
    staying with the person who actually owned the account.
    """
    with _cursor() as cur:
        return _account_usage(cur, account_id)


@_with_reconnect
def delete_account(account_id: int, *, force: bool = False) -> None:
    """Delete an account.

    Raises RecordInUseError if it's still referenced by transactions/recurring expenses/
    recurring investments, unless force=True. This check used to live only at the
    streamlit_app.py call site; enforcing it here too means any future caller (a script,
    another UI) can't silently detach an account's history into the shared/untagged cash
    bucket by skipping the check.
    """
    with _cursor() as cur:
        if not force:
            usage = _account_usage(cur, account_id)
            if any(usage.values()):
                raise RecordInUseError(usage)
        cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))


@_with_reconnect
def get_accounts() -> pd.DataFrame:
    with _connection() as conn:
        df = pd.read_sql_query(
            "SELECT id, owner, name, kind, initial_balance FROM accounts ORDER BY owner, id",
            conn,
        )
    return df


@_with_reconnect
def add_recurring_expense(
    name: str,
    category: str,
    amount: float,
    account_id: int | None,
    day_of_month: int,
    end_year: int | None = None,
    end_month: int | None = None,
    bonus_amount: float = 0,
    bonus_month_1: int | None = None,
    bonus_month_2: int | None = None,
) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO recurring_expenses "
            "(name, category, amount, account_id, day_of_month, end_year, end_month, "
            "bonus_amount, bonus_month_1, bonus_month_2) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                name,
                category,
                amount,
                account_id,
                day_of_month,
                end_year,
                end_month,
                bonus_amount,
                bonus_month_1,
                bonus_month_2,
            ),
        )
        rec_id = cur.fetchone()[0]
        # 新規作成時点の金額とボーナス月を「作成月の初日から有効」として記録しておく。
        # apply_recurring_expenses は作成月より前を遡ってバックフィルしないため、この開始日で
        # 以降のすべての記帳をカバーできる。
        today = today_jst()
        cur.execute(
            "INSERT INTO recurring_expense_amount_history "
            "(recurring_expense_id, amount, bonus_amount, bonus_month_1, bonus_month_2, effective_from) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (rec_id, amount, bonus_amount, bonus_month_1, bonus_month_2, date_(today.year, today.month, 1)),
        )


@_with_reconnect
def update_recurring_expense(
    recurring_id: int,
    name: str,
    category: str,
    amount: float,
    account_id: int | None,
    day_of_month: int,
    end_year: int | None = None,
    end_month: int | None = None,
    bonus_amount: float = 0,
    bonus_month_1: int | None = None,
    bonus_month_2: int | None = None,
    change_effective_from: date_ | None = None,
) -> None:
    """Update a recurring expense's settings.

    If the amount, bonus_amount, or either bonus month actually changed, records the new values
    into the change history as of `change_effective_from` (defaults to today) so that
    apply_recurring_expenses/resume_recurring_expense keep using the OLD amount/bonus schedule
    for any month before that date instead of silently rewriting past months with today's
    settings. Bonus months are versioned in the same history row as the amount (not read live
    from recurring_expenses) specifically so that changing which months are bonus months doesn't
    get retroactively applied when reconciling an unrelated, separately-backdated amount change.
    Also reconciles (UPDATEs) any transaction already posted on/after `change_effective_from` to
    the newly-resolved amount/bonus, so backdating a correction (e.g. "rent actually went up last
    month") fixes the already-posted month too, not just future backfills.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT amount, bonus_amount, bonus_month_1, bonus_month_2 "
            "FROM recurring_expenses WHERE id = %s",
            (recurring_id,),
        )
        old_row = cur.fetchone()
        cur.execute(
            "UPDATE recurring_expenses SET name = %s, category = %s, amount = %s, account_id = %s, "
            "day_of_month = %s, end_year = %s, end_month = %s, bonus_amount = %s, "
            "bonus_month_1 = %s, bonus_month_2 = %s WHERE id = %s",
            (
                name,
                category,
                amount,
                account_id,
                day_of_month,
                end_year,
                end_month,
                bonus_amount,
                bonus_month_1,
                bonus_month_2,
                recurring_id,
            ),
        )
        new_values = (amount, bonus_amount, bonus_month_1, bonus_month_2)
        if old_row is not None and new_values != tuple(old_row):
            effective_from = change_effective_from or today_jst()
            cur.execute(
                "INSERT INTO recurring_expense_amount_history "
                "(recurring_expense_id, amount, bonus_amount, bonus_month_1, bonus_month_2, effective_from) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (recurring_id, amount, bonus_amount, bonus_month_1, bonus_month_2, effective_from),
            )
            _reconcile_posted_expense_amounts(cur, recurring_id, effective_from, name)


@_with_reconnect
def delete_recurring_expense(recurring_id: int) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM recurring_expenses WHERE id = %s", (recurring_id,))


@_with_reconnect
def get_recurring_expenses() -> pd.DataFrame:
    with _connection() as conn:
        df = pd.read_sql_query(
            "SELECT id, name, category, amount, account_id, day_of_month, "
            "end_year, end_month, bonus_amount, bonus_month_1, bonus_month_2 "
            "FROM recurring_expenses ORDER BY day_of_month, id",
            conn,
        )
    return df


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _resolve_historical_amount(
    history: list[tuple],
    as_of_date: date_,
    fallback: tuple,
) -> tuple:
    """Pick the payload in effect on `as_of_date` from a list of change-history rows shaped
    (*payload, effective_from, id).

    Pure logic (no DB access), split out from _historical_recurring_expense_amount so it can
    be unit-tested directly. Ties on effective_from are broken by id, matching the original
    "ORDER BY effective_from DESC, id DESC LIMIT 1" query. Generic over the payload shape so the
    same resolver serves recurring expenses ((amount, bonus_amount, bonus_month_1, bonus_month_2))
    and recurring investments ((amount,)) alike - versioning the bonus month(s) alongside the
    amount, instead of always reading the recurring expense's current live bonus_month_1/2,
    matters because otherwise a later change to which months are bonus months would get
    retroactively applied when reconciling/backfilling postings dated under a different schedule.
    """
    candidates = [row for row in history if row[-2] <= as_of_date]
    if not candidates:
        return fallback
    row = max(candidates, key=lambda row: (row[-2], row[-1]))
    return row[:-2]


def _plan_recurring_expense_postings(
    today: date_,
    last_date: date_ | None,
    day_of_month: int,
    end_year: int | None,
    end_month: int | None,
    history: list[tuple[int, int, int | None, int | None, date_, int]],
    fallback_amount: int,
    fallback_bonus_amount: int,
    fallback_bonus_month_1: int | None,
    fallback_bonus_month_2: int | None,
    name: str,
    skipped_months: set[tuple[int, int]],
) -> list[tuple[date_, int, str]]:
    """The (date, amount, memo) rows apply_recurring_expenses should insert for one recurring
    expense, walking forward from the month after `last_date` (or the current month, if there's
    no prior posting) up to today.

    `history` rows are (amount, bonus_amount, bonus_month_1, bonus_month_2, effective_from, id).
    The bonus month(s) are resolved per posting date alongside the amount rather than taken from
    the caller's current live setting (see _resolve_historical_amount's docstring for why).

    Pure logic (no DB access), split out from apply_recurring_expenses so the backfill/bonus-
    month/end-date/skip decisions can be unit-tested directly instead of only through a live
    database (see test_db.py).
    """
    if last_date:
        year, month = _next_month(last_date.year, last_date.month)
    else:
        year, month = today.year, today.month

    postings: list[tuple[date_, int, str]] = []
    while (year, month) <= (today.year, today.month):
        if (year, month) == (today.year, today.month) and today.day < day_of_month:
            break
        if end_year and (year, month) > (int(end_year), int(end_month or 12)):
            break
        if (year, month) not in skipped_months:
            applied_date = date_(year, month, day_of_month)
            hist_amount, hist_bonus_amount, hist_bonus_month_1, hist_bonus_month_2 = _resolve_historical_amount(
                history,
                applied_date,
                (fallback_amount, fallback_bonus_amount, fallback_bonus_month_1, fallback_bonus_month_2),
            )
            applied_amount = hist_amount
            memo = f"{name}（固定費自動引き落とし）"
            if hist_bonus_amount and month in (hist_bonus_month_1, hist_bonus_month_2):
                applied_amount += hist_bonus_amount
                memo = f"{name}（固定費自動引き落とし・ボーナス加算）"
            postings.append((applied_date, applied_amount, memo))
        year, month = _next_month(year, month)
    return postings


def _plan_recurring_investment_postings(
    today: date_,
    last_date: date_ | None,
    day_of_month: int,
    history: list[tuple[int, date_, int]],
    fallback_amount: int,
    owner: str,
    skipped_months: set[tuple[int, int]],
) -> list[tuple[date_, int, str]]:
    """The (date, amount, memo) rows apply_recurring_investments should insert for one recurring
    investment. See _plan_recurring_expense_postings for why this is a pure function.
    """
    if last_date:
        year, month = _next_month(last_date.year, last_date.month)
    else:
        year, month = today.year, today.month

    postings: list[tuple[date_, int, str]] = []
    while (year, month) <= (today.year, today.month):
        if (year, month) == (today.year, today.month) and today.day < day_of_month:
            break
        if (year, month) not in skipped_months:
            applied_date = date_(year, month, day_of_month)
            (applied_amount,) = _resolve_historical_amount(history, applied_date, (fallback_amount,))
            postings.append((applied_date, applied_amount, f"{owner}の定期積立"))
        year, month = _next_month(year, month)
    return postings


def _historical_recurring_expense_amount(
    cur: psycopg2.extensions.cursor,
    recurring_expense_id: int,
    as_of_date: date_,
    fallback_amount: int,
    fallback_bonus_amount: int,
    fallback_bonus_month_1: int | None = None,
    fallback_bonus_month_2: int | None = None,
) -> tuple[int, int, int | None, int | None]:
    """The (amount, bonus_amount, bonus_month_1, bonus_month_2) actually in effect on
    `as_of_date`, per the change history."""
    cur.execute(
        "SELECT amount, bonus_amount, bonus_month_1, bonus_month_2, effective_from, id "
        "FROM recurring_expense_amount_history WHERE recurring_expense_id = %s",
        (recurring_expense_id,),
    )
    return _resolve_historical_amount(
        cur.fetchall(),
        as_of_date,
        (fallback_amount, fallback_bonus_amount, fallback_bonus_month_1, fallback_bonus_month_2),
    )


def _reconcile_posted_expense_amounts(
    cur: psycopg2.extensions.cursor,
    recurring_expense_id: int,
    effective_from: date_,
    name: str,
) -> None:
    """Correct already-posted transactions whose date falls on/after a (possibly backdated)
    amount/bonus-month change.

    update_recurring_expense() recording a new history row only changes what future backfills use
    UNLESS this also runs: without it, setting change_effective_from to a past date would silently
    leave months that were already posted (with the old amount/bonus schedule, before the
    correction) untouched, even though the edit form's help text tells the user those months get
    the new values too. Resolves amount AND bonus month per transaction's own date via
    _historical_recurring_expense_amount (not the caller's current live settings), so a
    transaction dated before this change keeps whatever schedule actually applied to it.
    """
    cur.execute(
        "SELECT id, date, amount FROM transactions WHERE recurring_expense_id = %s AND date >= %s",
        (recurring_expense_id, effective_from),
    )
    for txn_id, txn_date, old_amount in cur.fetchall():
        hist_amount, hist_bonus_amount, hist_bonus_month_1, hist_bonus_month_2 = (
            _historical_recurring_expense_amount(cur, recurring_expense_id, txn_date, 0, 0, None, None)
        )
        new_amount = hist_amount
        memo = f"{name}（固定費自動引き落とし）"
        if hist_bonus_amount and txn_date.month in (hist_bonus_month_1, hist_bonus_month_2):
            new_amount += hist_bonus_amount
            memo = f"{name}（固定費自動引き落とし・ボーナス加算）"
        if new_amount != old_amount:
            cur.execute(
                "UPDATE transactions SET amount = %s, memo = %s WHERE id = %s",
                (new_amount, memo, txn_id),
            )


def _historical_recurring_investment_amount(
    cur: psycopg2.extensions.cursor,
    recurring_investment_id: int,
    as_of_date: date_,
    fallback_amount: int,
) -> int:
    """The amount that was actually in effect on `as_of_date`, per the change history.

    Reuses _resolve_historical_amount rather than duplicating its tie-breaking logic for a
    second, near-identical resolver.
    """
    cur.execute(
        "SELECT amount, effective_from, id FROM recurring_investment_amount_history "
        "WHERE recurring_investment_id = %s",
        (recurring_investment_id,),
    )
    (resolved_amount,) = _resolve_historical_amount(cur.fetchall(), as_of_date, (fallback_amount,))
    return resolved_amount


def _reconcile_posted_investment_amounts(
    cur: psycopg2.extensions.cursor,
    recurring_investment_id: int,
    effective_from: date_,
    owner: str,
) -> None:
    """Correct already-posted contributions whose date falls on/after a (possibly backdated) amount change.

    See _reconcile_posted_expense_amounts for why this is needed in addition to recording history.
    """
    cur.execute(
        "SELECT id, date, amount FROM transactions "
        "WHERE recurring_investment_id = %s AND date >= %s",
        (recurring_investment_id, effective_from),
    )
    for txn_id, txn_date, old_amount in cur.fetchall():
        new_amount = _historical_recurring_investment_amount(cur, recurring_investment_id, txn_date, 0)
        if new_amount != old_amount:
            cur.execute(
                "UPDATE transactions SET amount = %s, memo = %s WHERE id = %s",
                (new_amount, f"{owner}の定期積立", txn_id),
            )


@_with_reconnect
def apply_recurring_expenses() -> None:
    """Insert this month's (and any missed past months') expense transaction for each recurring expense.

    Stops once the optional end year/month has passed, and adds the optional bonus amount
    on either of the two configured bonus months (e.g. a car loan's June/December top-up).
    Missed months are backfilled from the month after the most recently recorded transaction
    for that recurring expense, so skipping a month of opening the app doesn't silently drop it.
    A brand-new recurring expense with no prior transactions only applies the current month,
    since there is no record of when it was actually meant to start.
    """
    today = today_jst()
    with _cursor() as cur:
        cur.execute(
            "SELECT id, name, category, amount, account_id, day_of_month, "
            "end_year, end_month, bonus_amount, bonus_month_1, bonus_month_2 FROM recurring_expenses"
        )
        rows = cur.fetchall()
        for (
            rec_id,
            name,
            category,
            amount,
            account_id,
            day_of_month,
            end_year,
            end_month,
            bonus_amount,
            bonus_month_1,
            bonus_month_2,
        ) in rows:
            cur.execute(
                "SELECT MAX(date) FROM transactions WHERE recurring_expense_id = %s", (rec_id,)
            )
            last_date = cur.fetchone()[0]
            cur.execute(
                "SELECT year, month FROM recurring_expense_skips WHERE recurring_expense_id = %s",
                (rec_id,),
            )
            skipped_months = {(y, m) for y, m in cur.fetchall()}
            cur.execute(
                "SELECT amount, bonus_amount, bonus_month_1, bonus_month_2, effective_from, id "
                "FROM recurring_expense_amount_history WHERE recurring_expense_id = %s",
                (rec_id,),
            )
            history = cur.fetchall()
            postings = _plan_recurring_expense_postings(
                today,
                last_date,
                day_of_month,
                end_year,
                end_month,
                history,
                amount,
                bonus_amount,
                bonus_month_1,
                bonus_month_2,
                name,
                skipped_months,
            )
            for applied_date, applied_amount, memo in postings:
                cur.execute(
                    "INSERT INTO transactions "
                    "(date, type, category, amount, memo, account_id, recurring_expense_id) "
                    "VALUES (%s, 'expense', %s, %s, %s, %s, %s) "
                    "ON CONFLICT (recurring_expense_id, date_trunc('month', date::timestamp)) "
                    "WHERE recurring_expense_id IS NOT NULL DO NOTHING",
                    (applied_date, category, applied_amount, memo, account_id, rec_id),
                )


@_with_reconnect
def get_recurring_expense_skips() -> pd.DataFrame:
    """List months that were skipped because the auto-posted transaction for them was deleted."""
    with _connection() as conn:
        df = pd.read_sql_query(
            "SELECT s.recurring_expense_id, s.year, s.month, r.name "
            "FROM recurring_expense_skips s "
            "JOIN recurring_expenses r ON r.id = s.recurring_expense_id "
            "ORDER BY s.year DESC, s.month DESC",
            conn,
        )
    return df


@_with_reconnect
def resume_recurring_expense(recurring_expense_id: int, year: int, month: int) -> None:
    """Undo a skipped month: clear the skip flag and post that month's transaction now.

    apply_recurring_expenses() only ever walks forward from the latest recorded transaction,
    so merely deleting the skip row wouldn't cause it to revisit an older month once later
    months have already been posted. Posting directly here sidesteps that.
    """
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM recurring_expense_skips "
            "WHERE recurring_expense_id = %s AND year = %s AND month = %s",
            (recurring_expense_id, year, month),
        )
        cur.execute(
            "SELECT name, category, amount, account_id, day_of_month, "
            "bonus_amount, bonus_month_1, bonus_month_2 FROM recurring_expenses WHERE id = %s",
            (recurring_expense_id,),
        )
        row = cur.fetchone()
        if row is None:
            return
        name, category, amount, account_id, day_of_month, bonus_amount, bonus_month_1, bonus_month_2 = row
        applied_date = date_(year, month, day_of_month)
        hist_amount, hist_bonus_amount, hist_bonus_month_1, hist_bonus_month_2 = (
            _historical_recurring_expense_amount(
                cur, recurring_expense_id, applied_date, amount, bonus_amount, bonus_month_1, bonus_month_2
            )
        )
        applied_amount = hist_amount
        memo = f"{name}（固定費自動引き落とし）"
        if hist_bonus_amount and month in (hist_bonus_month_1, hist_bonus_month_2):
            applied_amount += hist_bonus_amount
            memo = f"{name}（固定費自動引き落とし・ボーナス加算）"
        cur.execute(
            "INSERT INTO transactions "
            "(date, type, category, amount, memo, account_id, recurring_expense_id) "
            "VALUES (%s, 'expense', %s, %s, %s, %s, %s) "
            "ON CONFLICT (recurring_expense_id, date_trunc('month', date::timestamp)) "
            "WHERE recurring_expense_id IS NOT NULL DO NOTHING",
            (applied_date, category, applied_amount, memo, account_id, recurring_expense_id),
        )


@_with_reconnect
def add_recurring_investment(
    owner: str, category: str, amount: float, account_id: int | None, day_of_month: int
) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO recurring_investments (owner, category, amount, account_id, day_of_month) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (owner, category, amount, account_id, day_of_month),
        )
        rec_id = cur.fetchone()[0]
        # 新規作成時点の金額を「作成月の初日から有効」として記録しておく
        # (add_recurring_expense と同じ理由。apply_recurring_investments は作成月より前を
        # 遡ってバックフィルしないため、この開始日で以降のすべての記帳をカバーできる)。
        today = today_jst()
        cur.execute(
            "INSERT INTO recurring_investment_amount_history "
            "(recurring_investment_id, amount, effective_from) VALUES (%s, %s, %s)",
            (rec_id, amount, date_(today.year, today.month, 1)),
        )


@_with_reconnect
def update_recurring_investment(
    recurring_id: int,
    owner: str,
    category: str,
    amount: float,
    account_id: int | None,
    day_of_month: int,
    change_effective_from: date_ | None = None,
) -> None:
    """Update a recurring investment's settings.

    If the amount actually changed, records it into the change history as of
    `change_effective_from` (defaults to today), mirroring update_recurring_expense: this keeps
    apply_recurring_investments/resume_recurring_investment using the OLD amount for any month
    before that date, and reconciles any contribution already posted on/after that date to the
    newly-resolved amount (see _reconcile_posted_investment_amounts).
    """
    with _cursor() as cur:
        cur.execute("SELECT amount FROM recurring_investments WHERE id = %s", (recurring_id,))
        old_row = cur.fetchone()
        cur.execute(
            "UPDATE recurring_investments SET owner = %s, category = %s, amount = %s, "
            "account_id = %s, day_of_month = %s WHERE id = %s",
            (owner, category, amount, account_id, day_of_month, recurring_id),
        )
        if old_row is not None and amount != old_row[0]:
            effective_from = change_effective_from or today_jst()
            cur.execute(
                "INSERT INTO recurring_investment_amount_history "
                "(recurring_investment_id, amount, effective_from) VALUES (%s, %s, %s)",
                (recurring_id, amount, effective_from),
            )
            _reconcile_posted_investment_amounts(cur, recurring_id, effective_from, owner)


@_with_reconnect
def delete_recurring_investment(recurring_id: int) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM recurring_investments WHERE id = %s", (recurring_id,))


@_with_reconnect
def get_recurring_investments() -> pd.DataFrame:
    with _connection() as conn:
        df = pd.read_sql_query(
            "SELECT id, owner, category, amount, account_id, day_of_month "
            "FROM recurring_investments ORDER BY day_of_month, id",
            conn,
        )
    return df


@_with_reconnect
def apply_recurring_investments() -> None:
    """Insert this month's (and any missed past months') NISA contribution transaction.

    Uses the same backfill approach as apply_recurring_expenses: gaps since the last
    recorded contribution are filled in, but a brand-new recurring investment only
    applies from the current month onward.
    """
    today = today_jst()
    with _cursor() as cur:
        cur.execute(
            "SELECT id, owner, category, amount, account_id, day_of_month FROM recurring_investments"
        )
        rows = cur.fetchall()
        for rec_id, owner, category, amount, account_id, day_of_month in rows:
            cur.execute(
                "SELECT MAX(date) FROM transactions WHERE recurring_investment_id = %s", (rec_id,)
            )
            last_date = cur.fetchone()[0]
            cur.execute(
                "SELECT year, month FROM recurring_investment_skips WHERE recurring_investment_id = %s",
                (rec_id,),
            )
            skipped_months = {(y, m) for y, m in cur.fetchall()}
            cur.execute(
                "SELECT amount, effective_from, id FROM recurring_investment_amount_history "
                "WHERE recurring_investment_id = %s",
                (rec_id,),
            )
            history = cur.fetchall()
            postings = _plan_recurring_investment_postings(
                today, last_date, day_of_month, history, amount, owner, skipped_months
            )
            for applied_date, applied_amount, memo in postings:
                cur.execute(
                    "INSERT INTO transactions "
                    "(date, type, category, amount, memo, account_id, owner, recurring_investment_id) "
                    "VALUES (%s, 'investment', %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (recurring_investment_id, date_trunc('month', date::timestamp)) "
                    "WHERE recurring_investment_id IS NOT NULL DO NOTHING",
                    (applied_date, category, applied_amount, memo, account_id, owner, rec_id),
                )


@_with_reconnect
def get_recurring_investment_skips() -> pd.DataFrame:
    """List months that were skipped because the auto-posted contribution for them was deleted."""
    with _connection() as conn:
        df = pd.read_sql_query(
            "SELECT s.recurring_investment_id, s.year, s.month, r.owner, r.category "
            "FROM recurring_investment_skips s "
            "JOIN recurring_investments r ON r.id = s.recurring_investment_id "
            "ORDER BY s.year DESC, s.month DESC",
            conn,
        )
    return df


@_with_reconnect
def resume_recurring_investment(recurring_investment_id: int, year: int, month: int) -> None:
    """Undo a skipped month: clear the skip flag and post that month's contribution now.

    See resume_recurring_expense() for why this posts directly instead of relying on
    apply_recurring_investments() to backfill it on the next run.
    """
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM recurring_investment_skips "
            "WHERE recurring_investment_id = %s AND year = %s AND month = %s",
            (recurring_investment_id, year, month),
        )
        cur.execute(
            "SELECT owner, category, amount, account_id, day_of_month "
            "FROM recurring_investments WHERE id = %s",
            (recurring_investment_id,),
        )
        row = cur.fetchone()
        if row is None:
            return
        owner, category, amount, account_id, day_of_month = row
        applied_date = date_(year, month, day_of_month)
        applied_amount = _historical_recurring_investment_amount(
            cur, recurring_investment_id, applied_date, amount
        )
        cur.execute(
            "INSERT INTO transactions "
            "(date, type, category, amount, memo, account_id, owner, recurring_investment_id) "
            "VALUES (%s, 'investment', %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (recurring_investment_id, date_trunc('month', date::timestamp)) "
            "WHERE recurring_investment_id IS NOT NULL DO NOTHING",
            (applied_date, category, applied_amount, f"{owner}の定期積立", account_id, owner, recurring_investment_id),
        )


@_with_reconnect
def add_child(name: str | None, birth_year: int) -> None:
    with _cursor() as cur:
        cur.execute("INSERT INTO children (name, birth_year) VALUES (%s, %s)", (name, birth_year))


@_with_reconnect
def delete_child(child_id: int) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM children WHERE id = %s", (child_id,))


@_with_reconnect
def get_children() -> pd.DataFrame:
    with _connection() as conn:
        df = pd.read_sql_query(
            "SELECT id, name, birth_year FROM children ORDER BY birth_year DESC, id", conn
        )
    return df


@_with_reconnect
def add_expense_category(name: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO expense_categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,)
        )


def _expense_category_usage(cur: psycopg2.extensions.cursor, name: str) -> dict[str, int]:
    cur.execute("SELECT COUNT(*) FROM recurring_expenses WHERE category = %s", (name,))
    recurring_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM budgets WHERE category = %s", (name,))
    budget_count = cur.fetchone()[0]
    # transactions.category is free-text (no FK), but a manually-entered transaction still
    # references this category by name. Without this check, deleting a category that's only
    # used by past manual transactions would silently orphan them: they'd no longer appear in
    # the category selectbox, and editing one would fall back to whatever category happens to
    # be first in the list instead of the transaction's actual category (see _safe_index).
    cur.execute("SELECT COUNT(*) FROM transactions WHERE category = %s", (name,))
    transaction_count = cur.fetchone()[0]
    return {
        "recurring_expenses": recurring_count,
        "budgets": budget_count,
        "transactions": transaction_count,
    }


@_with_reconnect
def get_expense_category_usage(name: str) -> dict[str, int]:
    """Count references to an expense category from recurring expenses, budgets, and transactions.

    Used to warn before deleting a category that is still in use, since none of these tables
    has a foreign key back to expense_categories (deleting one there wouldn't clean these up).
    """
    with _cursor() as cur:
        return _expense_category_usage(cur, name)


@_with_reconnect
def delete_expense_category(name: str, *, force: bool = False) -> None:
    """Delete an expense category.

    Raises RecordInUseError if it's still referenced by a recurring expense or a budget,
    unless force=True (see delete_account for why this check lives here and not only at
    the UI call site).
    """
    with _cursor() as cur:
        if not force:
            usage = _expense_category_usage(cur, name)
            if any(usage.values()):
                raise RecordInUseError(usage)
        cur.execute("DELETE FROM expense_categories WHERE name = %s", (name,))


@_with_reconnect
def get_expense_categories() -> list[str]:
    with _cursor() as cur:
        cur.execute("SELECT name FROM expense_categories ORDER BY id")
        rows = cur.fetchall()
    return [row[0] for row in rows]


@_with_reconnect
def rename_expense_category(old_name: str, new_name: str) -> None:
    """Rename an expense category everywhere it's referenced, in one transaction.

    Unlike delete_expense_category, this works even while the category is in use: it's the
    only way to fix a typo'd/duplicated category name without abandoning its history, since
    transactions.category/recurring_expenses.category/budgets.category are free text with no
    FK back to expense_categories, and delete_expense_category refuses to delete a category
    that's still referenced. Raises CategoryNameTakenError instead of merging if `new_name`
    already exists (merging two categories' budgets/history is a separate, unimplemented
    concern - go through delete_expense_category on the now-unused old name to merge manually).
    """
    with _cursor() as cur:
        cur.execute("SELECT 1 FROM expense_categories WHERE name = %s", (new_name,))
        if cur.fetchone() is not None:
            raise CategoryNameTakenError(new_name)
        cur.execute("UPDATE expense_categories SET name = %s WHERE name = %s", (new_name, old_name))
        cur.execute(
            "UPDATE transactions SET category = %s WHERE category = %s AND type = 'expense'",
            (new_name, old_name),
        )
        cur.execute(
            "UPDATE recurring_expenses SET category = %s WHERE category = %s", (new_name, old_name)
        )
        cur.execute("UPDATE budgets SET category = %s WHERE category = %s", (new_name, old_name))


@_with_reconnect
def add_income_category(name: str) -> None:
    if name in RESERVED_INCOME_CATEGORY_NAMES:
        raise ReservedCategoryNameError(name)
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO income_categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,)
        )


def _income_category_usage(cur: psycopg2.extensions.cursor, name: str) -> dict[str, int]:
    cur.execute("SELECT COUNT(*) FROM transactions WHERE category = %s", (name,))
    transaction_count = cur.fetchone()[0]
    return {"transactions": transaction_count}


@_with_reconnect
def get_income_category_usage(name: str) -> dict[str, int]:
    """Count references to an income category from transactions (see get_expense_category_usage)."""
    with _cursor() as cur:
        return _income_category_usage(cur, name)


@_with_reconnect
def delete_income_category(name: str, *, force: bool = False) -> None:
    """Delete an income category.

    Raises RecordInUseError if it's still referenced by a transaction, unless force=True
    (see delete_account/delete_expense_category for why this check lives here and not only
    at the UI call site).
    """
    with _cursor() as cur:
        if not force:
            usage = _income_category_usage(cur, name)
            if any(usage.values()):
                raise RecordInUseError(usage)
        cur.execute("DELETE FROM income_categories WHERE name = %s", (name,))


@_with_reconnect
def get_income_categories() -> list[str]:
    with _cursor() as cur:
        cur.execute("SELECT name FROM income_categories ORDER BY id")
        rows = cur.fetchall()
    return [row[0] for row in rows]


@_with_reconnect
def rename_income_category(old_name: str, new_name: str) -> None:
    """Rename an income category everywhere it's referenced (see rename_expense_category).

    Also refuses to rename into RESERVED_INCOME_CATEGORY_NAMES, for the same reason
    add_income_category refuses to create them: it would let salary be logged as a
    transaction under a different name than the reserved one, silently reintroducing the
    exact double-count with the annual-income settings this guard exists to prevent.
    """
    if new_name in RESERVED_INCOME_CATEGORY_NAMES:
        raise ReservedCategoryNameError(new_name)
    with _cursor() as cur:
        cur.execute("SELECT 1 FROM income_categories WHERE name = %s", (new_name,))
        if cur.fetchone() is not None:
            raise CategoryNameTakenError(new_name)
        cur.execute("UPDATE income_categories SET name = %s WHERE name = %s", (new_name, old_name))
        cur.execute(
            "UPDATE transactions SET category = %s WHERE category = %s AND type = 'income'",
            (new_name, old_name),
        )


@_with_reconnect
def get_login_attempt(client_key: str) -> tuple[int, float]:
    """(failed_attempts, locked_until) for this client, defaulting to (0, 0.0) if never seen."""
    with _cursor() as cur:
        cur.execute(
            "SELECT failed_attempts, locked_until FROM login_attempts WHERE client_key = %s",
            (client_key,),
        )
        row = cur.fetchone()
    return tuple(row) if row else (0, 0.0)


@_with_reconnect
def record_login_failure(client_key: str, max_attempts: int, lockout_seconds: float) -> int:
    """Increment this client's failure count and lock it out once max_attempts is reached.

    Keyed by client_key (e.g. IP address) rather than a single global counter, so one
    attacker repeatedly guessing wrong can't lock every legitimate user out indefinitely -
    only their own key gets locked. Returns the new failure count.
    """
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO login_attempts (client_key, failed_attempts) VALUES (%s, 1) "
            "ON CONFLICT (client_key) DO UPDATE SET "
            "failed_attempts = login_attempts.failed_attempts + 1 "
            "RETURNING failed_attempts",
            (client_key,),
        )
        attempts = cur.fetchone()[0]
        if attempts >= max_attempts:
            cur.execute(
                "UPDATE login_attempts SET locked_until = %s WHERE client_key = %s",
                (time.time() + lockout_seconds, client_key),
            )
    return attempts


@_with_reconnect
def reset_login_attempts(client_key: str) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM login_attempts WHERE client_key = %s", (client_key,))
