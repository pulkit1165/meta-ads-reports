"""
Quarterly Planner — Generate day/week/month/quarter projections.

Given starting orders, ending orders, blend, and time horizon, generate
a full ramp plan with profit/revenue/ad-spend per period.
"""

from profit_formula import (
    compute_profit,
    blended_profit_per_order,
    blended_roas,
    format_inr,
    DEFAULT_CATEGORY_PROFIT,
)


def linear_ramp(start_orders: int, end_orders: int, periods: int) -> list:
    """Return a list of orders/period that linearly ramps from start to end."""
    if periods <= 1:
        return [end_orders]
    increment = (end_orders - start_orders) / (periods - 1)
    return [int(round(start_orders + i * increment)) for i in range(periods)]


def generate_weekly_plan(weeks: int = 12,
                          start_orders: int = 4000,
                          end_orders: int = 15000,
                          mix: dict = None,
                          aov: float = 850,
                          profits: dict = None,
                          days_per_week: int = 7) -> list:
    """
    Build a week-by-week plan.

    Returns list of dicts: [{week, orders_per_day, daily_profit, weekly_profit, cumulative, ...}, ...]
    """
    profits = profits or DEFAULT_CATEGORY_PROFIT
    if mix is None:
        mix = {
            'skin': 0.20, 'hair': 0.10, 'perfume': 0.05,
            'jewellery': 0.30, 'crystals': 0.15, 'stock_clearance': 0.20,
        }
    profit_per_order = blended_profit_per_order(mix, profits)
    roas = blended_roas(mix)

    ramp = linear_ramp(start_orders, end_orders, weeks)
    plan = []
    cumulative = 0

    for w_idx, orders in enumerate(ramp, start=1):
        daily_rev = orders * aov
        daily_profit = orders * profit_per_order
        daily_ad = daily_rev / roas if roas > 0 else 0
        weekly_profit = daily_profit * days_per_week
        weekly_revenue = daily_rev * days_per_week
        weekly_ad = daily_ad * days_per_week
        cumulative += weekly_profit

        plan.append({
            'week': w_idx,
            'orders_per_day': orders,
            'daily_revenue': round(daily_rev, 0),
            'daily_profit': round(daily_profit, 0),
            'daily_ad_spend': round(daily_ad, 0),
            'weekly_revenue': round(weekly_revenue, 0),
            'weekly_profit': round(weekly_profit, 0),
            'weekly_ad_spend': round(weekly_ad, 0),
            'cumulative_profit': round(cumulative, 0),
        })

    return plan


def generate_monthly_plan(months: int = 10,
                           start_orders: int = 4000,
                           end_orders: int = 15000,
                           mix: dict = None,
                           aov: float = 850,
                           days_per_month: int = 30,
                           cap_orders: int = None,
                           festive_months: list = None,
                           festive_boost_pct: float = 0.10) -> list:
    """
    Build a month-by-month plan with optional cap and festive boost.

    Args:
        festive_months: list of month numbers (1-indexed) to apply festive boost
        festive_boost_pct: % uplift to profit during festive months
        cap_orders: if set, orders won't exceed this value
    """
    if mix is None:
        mix = {
            'skin': 0.20, 'hair': 0.10, 'perfume': 0.05,
            'jewellery': 0.30, 'crystals': 0.15, 'stock_clearance': 0.20,
        }
    profit_per_order = blended_profit_per_order(mix)
    roas = blended_roas(mix)
    festive_months = festive_months or []

    ramp = linear_ramp(start_orders, end_orders, months)
    if cap_orders:
        ramp = [min(o, cap_orders) for o in ramp]

    plan = []
    cumulative = 0
    for m_idx, orders in enumerate(ramp, start=1):
        is_festive = m_idx in festive_months
        boost = 1 + festive_boost_pct if is_festive else 1.0
        daily_rev = orders * aov
        daily_profit = orders * profit_per_order * boost
        daily_ad = daily_rev / roas if roas > 0 else 0
        monthly_profit = daily_profit * days_per_month
        monthly_revenue = daily_rev * days_per_month
        monthly_ad = daily_ad * days_per_month
        cumulative += monthly_profit

        plan.append({
            'month': m_idx,
            'orders_per_day': orders,
            'is_festive': is_festive,
            'daily_profit': round(daily_profit, 0),
            'monthly_revenue': round(monthly_revenue, 0),
            'monthly_profit': round(monthly_profit, 0),
            'monthly_ad_spend': round(monthly_ad, 0),
            'cumulative_profit': round(cumulative, 0),
            'ebitda_pct': round(daily_profit / daily_rev * 100, 2),
        })

    return plan


def print_weekly_plan(plan: list):
    """Pretty-print a weekly plan."""
    print(f"{'Week':<6} {'Orders':>8} {'Daily Profit':>15} {'Weekly Profit':>15} {'Cumulative':>13}")
    print("-" * 70)
    for w in plan:
        print(f"W{w['week']:<5} {w['orders_per_day']:>8,} "
              f"{format_inr(w['daily_profit']):>15} "
              f"{format_inr(w['weekly_profit']):>15} "
              f"{format_inr(w['cumulative_profit']):>13}")
    if plan:
        print(f"\nTotal: {format_inr(plan[-1]['cumulative_profit'])}")


def print_monthly_plan(plan: list):
    """Pretty-print a monthly plan."""
    print(f"{'Month':<8} {'Orders/Day':>10} {'Festive':>8} {'Monthly Profit':>15} {'Cumulative':>13} {'EBITDA%':>8}")
    print("-" * 78)
    for m in plan:
        festive = '✓' if m['is_festive'] else ''
        print(f"M{m['month']:<7} {m['orders_per_day']:>10,} {festive:>8} "
              f"{format_inr(m['monthly_profit']):>15} "
              f"{format_inr(m['cumulative_profit']):>13} "
              f"{m['ebitda_pct']:>7.1f}%")
    if plan:
        print(f"\nTotal: {format_inr(plan[-1]['cumulative_profit'])}")


if __name__ == '__main__':
    print("=" * 70)
    print("QUARTERLY PLAN — 12 weeks, 4K → 15K orders/day")
    print("=" * 70)
    weekly = generate_weekly_plan(weeks=12, start_orders=4000, end_orders=15000)
    print_weekly_plan(weekly)
    print()

    print("=" * 70)
    print("10-MONTH PLAN — Ramp to 15K cap, festive months 8-9")
    print("=" * 70)
    monthly = generate_monthly_plan(
        months=10,
        start_orders=4000,
        end_orders=15000,
        cap_orders=15000,
        festive_months=[8, 9],
        festive_boost_pct=0.10,
    )
    print_monthly_plan(monthly)
