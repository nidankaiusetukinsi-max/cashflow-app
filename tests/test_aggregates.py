import pandas as pd

from aggregates import compute_net_worth
from db import (
    SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND,
    SETTING_GROWTH_LIFETIME_BEFORE_WIFE,
    SETTING_INITIAL_CASH,
    SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND,
    SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE,
)

BASE_SETTINGS = {
    SETTING_INITIAL_CASH: 0.0,
    SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND: 0.0,
    SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE: 0.0,
    SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND: 0.0,
    SETTING_GROWTH_LIFETIME_BEFORE_WIFE: 0.0,
}


def _accounts(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["id", "owner", "name", "kind", "initial_balance"])


def _transactions(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(
        rows, columns=["id", "date", "type", "category", "amount", "memo", "account_id", "to_account_id", "owner"]
    )
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_empty_state_is_all_zero():
    summary = compute_net_worth(_transactions([]), _accounts([]), BASE_SETTINGS, owners=["夫", "嫁"])
    assert summary.current_balance == 0
    assert summary.total_assets == 0
    assert summary.owner_total_assets == {"夫": 0, "嫁": 0}


def test_initial_cash_plus_untagged_income_and_expense():
    settings = {**BASE_SETTINGS, SETTING_INITIAL_CASH: 100_000.0}
    transactions = _transactions(
        [
            {"id": 1, "date": "2026-01-01", "type": "income", "category": "副業", "amount": 50_000, "account_id": None, "to_account_id": None, "owner": None},
            {"id": 2, "date": "2026-01-02", "type": "expense", "category": "食費", "amount": 10_000, "account_id": None, "to_account_id": None, "owner": None},
        ]
    )
    summary = compute_net_worth(transactions, _accounts([]), settings, owners=["夫", "嫁"])
    # 100,000 (initial cash) + 50,000 (income) - 10,000 (expense) = 140,000
    assert summary.current_balance == 140_000
    assert summary.shared_cash == 140_000
    assert summary.total_assets == 140_000


def test_account_expense_reduces_that_accounts_balance_only():
    accounts = _accounts([{"id": 1, "owner": "夫", "name": "みずほ銀行", "kind": "bank", "initial_balance": 200_000}])
    transactions = _transactions(
        [{"id": 1, "date": "2026-01-01", "type": "expense", "category": "食費", "amount": 30_000, "account_id": 1, "to_account_id": None, "owner": None}]
    )
    summary = compute_net_worth(transactions, accounts, BASE_SETTINGS, owners=["夫", "嫁"])
    assert summary.account_balances[1] == 170_000
    assert summary.current_balance == 170_000
    assert summary.owner_account_totals["夫"] == 170_000
    assert summary.owner_account_totals["嫁"] == 0


def test_transfer_moves_balance_between_accounts_without_changing_total():
    accounts = _accounts(
        [
            {"id": 1, "owner": "夫", "name": "A銀行", "kind": "bank", "initial_balance": 100_000},
            {"id": 2, "owner": "夫", "name": "B銀行", "kind": "bank", "initial_balance": 0},
        ]
    )
    transactions = _transactions(
        [{"id": 1, "date": "2026-01-01", "type": "transfer", "category": "振替", "amount": 40_000, "account_id": 1, "to_account_id": 2, "owner": None}]
    )
    summary = compute_net_worth(transactions, accounts, BASE_SETTINGS, owners=["夫", "嫁"])
    assert summary.account_balances[1] == 60_000
    assert summary.account_balances[2] == 40_000
    assert summary.current_balance == 100_000


def test_transfer_to_cash_increases_shared_cash_and_drains_account():
    accounts = _accounts([{"id": 1, "owner": "夫", "name": "A銀行", "kind": "bank", "initial_balance": 50_000}])
    transactions = _transactions(
        [{"id": 1, "date": "2026-01-01", "type": "transfer", "category": "振替", "amount": 20_000, "account_id": 1, "to_account_id": None, "owner": None}]
    )
    summary = compute_net_worth(transactions, accounts, BASE_SETTINGS, owners=["夫", "嫁"])
    assert summary.account_balances[1] == 30_000
    assert summary.shared_cash == 20_000
    assert summary.current_balance == 50_000


def test_nisa_investment_splits_by_owner_and_unassigned_bucket():
    settings = {**BASE_SETTINGS, SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND: 100_000.0}
    transactions = _transactions(
        [
            {"id": 1, "date": "2026-01-01", "type": "investment", "category": "つみたて投資枠", "amount": 30_000, "account_id": None, "to_account_id": None, "owner": "夫"},
            {"id": 2, "date": "2026-01-02", "type": "investment", "category": "成長投資枠", "amount": 20_000, "account_id": None, "to_account_id": None, "owner": None},
        ]
    )
    summary = compute_net_worth(transactions, _accounts([]), settings, owners=["夫", "嫁"])
    assert summary.owner_nisa_totals["夫"] == 130_000
    assert summary.owner_nisa_totals["嫁"] == 0
    assert summary.unassigned_nisa_total == 20_000
    # invariant: shared_cash(現金・預金の共通分) + owner totals + unassigned nisa == total_assets
    reconciled = summary.shared_cash + sum(summary.owner_total_assets.values()) + summary.unassigned_nisa_total
    assert reconciled == summary.total_assets
