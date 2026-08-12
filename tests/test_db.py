from datetime import date

from db import (
    _next_month,
    _plan_recurring_expense_postings,
    _plan_recurring_investment_postings,
    _resolve_historical_amount,
)


def test_next_month_rolls_within_year():
    assert _next_month(2026, 5) == (2026, 6)


def test_next_month_rolls_over_year_boundary():
    assert _next_month(2026, 12) == (2027, 1)


def test_resolve_historical_amount_falls_back_when_no_history():
    assert _resolve_historical_amount([], date(2026, 6, 1), 1000, 0) == (1000, 0)


def test_resolve_historical_amount_falls_back_when_all_history_is_after_as_of_date():
    history = [(2000, 0, date(2026, 8, 1), 1)]
    assert _resolve_historical_amount(history, date(2026, 6, 1), 1000, 0) == (1000, 0)


def test_resolve_historical_amount_picks_the_most_recent_entry_at_or_before_as_of_date():
    history = [
        (1000, 0, date(2000, 1, 1), 1),
        (1500, 0, date(2026, 3, 1), 2),
        (2000, 0, date(2026, 9, 1), 3),
    ]
    # 2026-06-01 falls between the March and September changes, so the March rate applies.
    assert _resolve_historical_amount(history, date(2026, 6, 1), 999, 0) == (1500, 0)


def test_resolve_historical_amount_includes_bonus_amount():
    history = [(3000, 50_000, date(2026, 1, 1), 1)]
    assert _resolve_historical_amount(history, date(2026, 6, 1), 3000, 0) == (3000, 50_000)


def test_resolve_historical_amount_breaks_ties_on_effective_from_by_id():
    # Two entries recorded for the exact same effective_from date: the one inserted later
    # (higher id) should win, matching "ORDER BY effective_from DESC, id DESC".
    history = [
        (1000, 0, date(2026, 1, 1), 1),
        (1200, 0, date(2026, 1, 1), 2),
    ]
    assert _resolve_historical_amount(history, date(2026, 1, 1), 999, 0) == (1200, 0)


def test_resolve_historical_amount_investment_reuse_keeps_old_rate_before_change():
    # _historical_recurring_investment_amount reuses _resolve_historical_amount with the bonus
    # slot always zeroed (recurring investments have no bonus concept). This pins the core
    # regression this reuse exists to prevent: a mid-backfill rate increase must not get applied
    # to months that fall before the change's effective_from.
    history = [
        (10_000, 0, date(2000, 1, 1), 1),  # created-at amount, effective from far in the past
        (30_000, 0, date(2026, 4, 1), 2),  # raised to 30,000 starting 2026-04
    ]
    # A month before the raise (e.g. backfilled Feb contribution) keeps the old 10,000 rate.
    assert _resolve_historical_amount(history, date(2026, 2, 27), 10_000, 0) == (10_000, 0)
    # The month the raise takes effect (and any month after) uses the new 30,000 rate.
    assert _resolve_historical_amount(history, date(2026, 4, 27), 10_000, 0) == (30_000, 0)


def test_plan_recurring_expense_postings_no_prior_history_only_applies_current_month_when_day_reached():
    today = date(2026, 6, 27)
    postings = _plan_recurring_expense_postings(
        today=today,
        last_date=None,
        day_of_month=27,
        end_year=None,
        end_month=None,
        history=[(1000, 0, date(2000, 1, 1), 1)],
        fallback_amount=1000,
        fallback_bonus_amount=0,
        bonus_month_1=None,
        bonus_month_2=None,
        name="家賃",
        skipped_months=set(),
    )
    assert postings == [(date(2026, 6, 27), 1000, "家賃（固定費自動引き落とし）")]


def test_plan_recurring_expense_postings_waits_until_day_of_month_is_reached():
    today = date(2026, 6, 10)
    postings = _plan_recurring_expense_postings(
        today=today,
        last_date=None,
        day_of_month=27,
        end_year=None,
        end_month=None,
        history=[],
        fallback_amount=1000,
        fallback_bonus_amount=0,
        bonus_month_1=None,
        bonus_month_2=None,
        name="家賃",
        skipped_months=set(),
    )
    assert postings == []


def test_plan_recurring_expense_postings_backfills_missed_months():
    today = date(2026, 6, 27)
    postings = _plan_recurring_expense_postings(
        today=today,
        last_date=date(2026, 3, 27),
        day_of_month=27,
        end_year=None,
        end_month=None,
        history=[],
        fallback_amount=1000,
        fallback_bonus_amount=0,
        bonus_month_1=None,
        bonus_month_2=None,
        name="家賃",
        skipped_months=set(),
    )
    assert [posting[0] for posting in postings] == [date(2026, 4, 27), date(2026, 5, 27), date(2026, 6, 27)]


def test_plan_recurring_expense_postings_stops_after_end_year_month():
    today = date(2026, 6, 27)
    postings = _plan_recurring_expense_postings(
        today=today,
        last_date=date(2025, 12, 27),
        day_of_month=27,
        end_year=2026,
        end_month=2,
        history=[],
        fallback_amount=1000,
        fallback_bonus_amount=0,
        bonus_month_1=None,
        bonus_month_2=None,
        name="車のローン",
        skipped_months=set(),
    )
    assert [posting[0] for posting in postings] == [date(2026, 1, 27), date(2026, 2, 27)]


def test_plan_recurring_expense_postings_adds_bonus_amount_on_bonus_months():
    today = date(2026, 6, 27)
    postings = _plan_recurring_expense_postings(
        today=today,
        last_date=date(2026, 5, 27),
        day_of_month=27,
        end_year=None,
        end_month=None,
        history=[],
        fallback_amount=30_000,
        fallback_bonus_amount=100_000,
        bonus_month_1=6,
        bonus_month_2=12,
        name="車のローン",
        skipped_months=set(),
    )
    assert postings == [(date(2026, 6, 27), 130_000, "車のローン（固定費自動引き落とし・ボーナス加算）")]


def test_plan_recurring_expense_postings_excludes_skipped_months_but_continues():
    today = date(2026, 6, 27)
    postings = _plan_recurring_expense_postings(
        today=today,
        last_date=date(2026, 4, 27),
        day_of_month=27,
        end_year=None,
        end_month=None,
        history=[],
        fallback_amount=1000,
        fallback_bonus_amount=0,
        bonus_month_1=None,
        bonus_month_2=None,
        name="家賃",
        skipped_months={(2026, 5)},
    )
    assert [posting[0] for posting in postings] == [date(2026, 6, 27)]


def test_plan_recurring_expense_postings_uses_amount_in_effect_on_each_posting_date():
    today = date(2026, 6, 27)
    history = [
        (10_000, 0, date(2000, 1, 1), 1),
        (20_000, 0, date(2026, 6, 1), 2),
    ]
    postings = _plan_recurring_expense_postings(
        today=today,
        last_date=date(2026, 4, 27),
        day_of_month=27,
        end_year=None,
        end_month=None,
        history=history,
        fallback_amount=10_000,
        fallback_bonus_amount=0,
        bonus_month_1=None,
        bonus_month_2=None,
        name="サブスク",
        skipped_months=set(),
    )
    assert postings == [
        (date(2026, 5, 27), 10_000, "サブスク（固定費自動引き落とし）"),
        (date(2026, 6, 27), 20_000, "サブスク（固定費自動引き落とし）"),
    ]


def test_plan_recurring_investment_postings_backfills_and_uses_historical_amount():
    today = date(2026, 6, 27)
    history = [
        (10_000, 0, date(2000, 1, 1), 1),
        (30_000, 0, date(2026, 6, 1), 2),
    ]
    postings = _plan_recurring_investment_postings(
        today=today,
        last_date=date(2026, 4, 27),
        day_of_month=27,
        history=history,
        fallback_amount=10_000,
        owner="夫",
        skipped_months=set(),
    )
    assert postings == [
        (date(2026, 5, 27), 10_000, "夫の定期積立"),
        (date(2026, 6, 27), 30_000, "夫の定期積立"),
    ]


def test_plan_recurring_investment_postings_excludes_skipped_months():
    today = date(2026, 6, 27)
    postings = _plan_recurring_investment_postings(
        today=today,
        last_date=date(2026, 4, 27),
        day_of_month=27,
        history=[],
        fallback_amount=5_000,
        owner="嫁",
        skipped_months={(2026, 5), (2026, 6)},
    )
    assert postings == []
