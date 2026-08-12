"""Account-balance / net-worth aggregation.

This used to live inline at module level in streamlit_app.py, which meant the single
most money-critical calculation in the app (who owns how much, split across accounts
and NISA contributions) had zero test coverage. Pulled out into pure functions here so
it can be exercised with plain DataFrames/dicts, independent of Streamlit or the DB.
"""

from dataclasses import dataclass, field

import pandas as pd

from db import (
    OWNERS,
    SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND,
    SETTING_GROWTH_LIFETIME_BEFORE_WIFE,
    SETTING_INITIAL_CASH,
    SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND,
    SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE,
)

OWNER_NISA_LIFETIME_KEYS = {
    ("夫", "つみたて投資枠"): SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND,
    ("嫁", "つみたて投資枠"): SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE,
    ("夫", "成長投資枠"): SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND,
    ("嫁", "成長投資枠"): SETTING_GROWTH_LIFETIME_BEFORE_WIFE,
}


@dataclass
class NetWorthSummary:
    account_balances: dict[int, float]
    untagged_net: float
    shared_cash: float
    current_balance: float
    nisa_lifetime_total: float
    total_assets: float
    owner_account_totals: dict[str, float] = field(default_factory=dict)
    owner_nisa_totals: dict[str, float] = field(default_factory=dict)
    owner_total_assets: dict[str, float] = field(default_factory=dict)
    unassigned_nisa_total: float = 0.0


def compute_net_worth(
    transactions_to_date: pd.DataFrame,
    accounts_df: pd.DataFrame,
    settings: dict[str, float],
    owners: list[str] = OWNERS,
) -> NetWorthSummary:
    """Roll up account balances and household/owner net worth as of "today".

    `transactions_to_date` must already be filtered to transactions on or before today
    (future-dated rows would prematurely count money that hasn't moved yet). Mirrors the
    invariant that shared_cash + sum(owner_total_assets) + unassigned_nisa_total ==
    total_assets, so the per-owner breakdown always reconciles with the household total.
    """
    signed_amount = transactions_to_date["amount"].where(
        transactions_to_date["type"] == "income", -transactions_to_date["amount"]
    )

    # groupby drops NaN keys by default, so this naturally excludes untagged (cash) rows.
    net_by_account = signed_amount.groupby(transactions_to_date["account_id"]).sum()
    transfer_in = (
        transactions_to_date[transactions_to_date["type"] == "transfer"]
        .groupby("to_account_id")["amount"]
        .sum()
    )
    net_by_account = net_by_account.add(transfer_in, fill_value=0)
    account_balances: dict[int, float] = {
        row.id: row.initial_balance + net_by_account.get(row.id, 0.0) for row in accounts_df.itertuples()
    }

    # 現金(未登録)宛の振替(振替先が「現金」)は to_account_id が無いため上の transfer_in
    # には乗らない。signed_amount 側(振替元がaccount_idを持つ行)はnet_by_accountで、
    # 振替先が現金の行はここでuntagged_netに加算して両建てで正しく反映する。
    transfer_to_cash = transactions_to_date.loc[
        (transactions_to_date["type"] == "transfer") & (transactions_to_date["to_account_id"].isna()),
        "amount",
    ].sum()
    untagged_net = (
        signed_amount[transactions_to_date["account_id"].isna()].sum() + transfer_to_cash
    )
    shared_cash = settings[SETTING_INITIAL_CASH] + untagged_net
    current_balance = shared_cash + sum(account_balances.values())

    nisa_lifetime_total = (
        transactions_to_date.loc[transactions_to_date["type"] == "investment", "amount"].sum()
        + settings[SETTING_TSUMITATE_LIFETIME_BEFORE_HUSBAND]
        + settings[SETTING_TSUMITATE_LIFETIME_BEFORE_WIFE]
        + settings[SETTING_GROWTH_LIFETIME_BEFORE_HUSBAND]
        + settings[SETTING_GROWTH_LIFETIME_BEFORE_WIFE]
    )
    total_assets = current_balance + nisa_lifetime_total

    # 夫婦それぞれの資産（口座残高 + NISA拠出累計）。所有者未設定のNISA取引は「世帯共通NISA」として
    # 明示的な第3のバケットに集計し、shared_cash + Σowner_total_assets + unassigned_nisa_total が
    # 常に total_assets と一致するようにする(内訳の合計が総額と食い違わないようにするため)。
    investment_all = transactions_to_date[transactions_to_date["type"] == "investment"]
    owner_account_totals: dict[str, float] = {}
    owner_nisa_totals: dict[str, float] = {}
    owner_total_assets: dict[str, float] = {}
    for owner in owners:
        owner_account_ids = accounts_df.loc[accounts_df["owner"] == owner, "id"]
        owner_account_totals[owner] = sum(account_balances.get(aid, 0.0) for aid in owner_account_ids)
        owner_investment_all = investment_all[investment_all["owner"] == owner]
        owner_nisa_totals[owner] = (
            owner_investment_all["amount"].sum()
            + settings[OWNER_NISA_LIFETIME_KEYS[(owner, "つみたて投資枠")]]
            + settings[OWNER_NISA_LIFETIME_KEYS[(owner, "成長投資枠")]]
        )
        owner_total_assets[owner] = owner_account_totals[owner] + owner_nisa_totals[owner]
    unassigned_nisa_total = investment_all.loc[investment_all["owner"].isna(), "amount"].sum()

    return NetWorthSummary(
        account_balances=account_balances,
        untagged_net=untagged_net,
        shared_cash=shared_cash,
        current_balance=current_balance,
        nisa_lifetime_total=nisa_lifetime_total,
        total_assets=total_assets,
        owner_account_totals=owner_account_totals,
        owner_nisa_totals=owner_nisa_totals,
        owner_total_assets=owner_total_assets,
        unassigned_nisa_total=unassigned_nisa_total,
    )
