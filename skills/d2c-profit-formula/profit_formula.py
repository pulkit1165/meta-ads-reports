"""
D2C Profit Formula — Core Math

Computes per-order economics, lever sensitivity, and profit projections for
Studd Muffyn's D2C portfolio.

Usage:
    from profit_formula import compute_profit, lever_check

    result = compute_profit(orders_per_day=10000, roas=2.75, tax_pct=7, delivery_pct=85)
    print(result['daily_profit'], result['profit_pct'])
"""

# Default per-order profit by category (winners-only, Q3 2026 calibration)
DEFAULT_CATEGORY_PROFIT = {
    'skin':            185,   # Sunkissed, AM/PM, Peptides, Hyaluronic, Triderma
    'hair':            165,   # Xtreme Combo, Phus Phus, Booster Kit
    'perfume':         205,   # B1G1, B2G1, Solid
    'jewellery':       290,   # China Pendants (Tortoise/Owl ₹342) + Velore (₹194) mix
    'crystals':        240,   # Peacock Frame, Selenite, Money Bowl
    'neutra':          230,   # Kapikachu, Bro Neutra, Berberine
    'stock_clearance': 40,    # Bundle + B2B + Marketplace partial recovery
}

DEFAULT_CATEGORY_ROAS = {
    'skin':            3.0,
    'hair':            3.35,
    'perfume':         2.3,
    'jewellery':       2.5,
    'crystals':        2.5,
    'neutra':          2.4,
    'stock_clearance': 1.5,
}

DEFAULT_CATEGORY_TAX = {
    'skin':            18,
    'hair':            18,
    'perfume':         18,
    'jewellery':       4,
    'crystals':        4,
    'neutra':          5,
    'stock_clearance': 12,
}


def lever_check(roas: float, tax_pct: float, delivery_pct: float,
                base_pct: float = 6.0) -> dict:
    """
    Compute profit % using the 3-lever sensitivity formula.

    base 6% + (ROAS - 2.5) × 10 + (8 - Tax%) + (Delivery% - 80)

    Returns the math broken down step-by-step.
    """
    roas_gain = (roas - 2.5) * 10
    tax_gain = 8 - tax_pct
    delivery_gain = delivery_pct - 80
    total = base_pct + roas_gain + tax_gain + delivery_gain

    return {
        'base': base_pct,
        'roas_gain': round(roas_gain, 2),
        'tax_gain': round(tax_gain, 2),
        'delivery_gain': round(delivery_gain, 2),
        'profit_pct': round(total, 2),
        'verification': f"{base_pct} + {roas_gain:.1f} + {tax_gain:.1f} + {delivery_gain:.1f} = {total:.1f}%",
    }


def blended_profit_per_order(mix: dict, profits: dict = None) -> float:
    """
    Compute weighted-average profit per order given a category mix.

    Args:
        mix: dict of {category: share_pct as decimal 0..1} summing to 1.0
        profits: optional override of per-category ₹ profit (defaults to DEFAULT_CATEGORY_PROFIT)

    Returns:
        Weighted blended profit per order in ₹
    """
    profits = profits or DEFAULT_CATEGORY_PROFIT
    if abs(sum(mix.values()) - 1.0) > 0.001:
        raise ValueError(f"Mix shares must sum to 1.0, got {sum(mix.values()):.3f}")
    return sum(mix.get(cat, 0) * profits.get(cat, 0) for cat in mix)


def blended_tax(mix: dict, taxes: dict = None) -> float:
    """Weighted-average tax percentage for a category mix."""
    taxes = taxes or DEFAULT_CATEGORY_TAX
    return sum(mix.get(cat, 0) * taxes.get(cat, 0) for cat in mix)


def blended_roas(mix: dict, roas_map: dict = None, aov: float = 850) -> float:
    """
    Weighted-average ROAS — computed via revenue/spend ratio
    (not just simple mix average, since ROAS is a ratio).
    """
    roas_map = roas_map or DEFAULT_CATEGORY_ROAS
    total_rev = sum(mix.get(cat, 0) * aov for cat in mix)
    total_spend = sum(mix.get(cat, 0) * aov / roas_map.get(cat, 1) for cat in mix)
    return total_rev / total_spend if total_spend else 0


def compute_profit(orders_per_day: int,
                   mix: dict = None,
                   roas: float = None,
                   tax_pct: float = None,
                   delivery_pct: float = 80,
                   aov: float = 850,
                   profits: dict = None,
                   roas_map: dict = None,
                   taxes: dict = None) -> dict:
    """
    Full profit calculation given orders/day + blend + lever values.

    Returns dict with:
        daily_revenue, daily_profit, daily_ad_spend, profit_per_order,
        profit_pct, monthly_profit, monthly_revenue, monthly_ad_spend,
        annual_profit, annual_revenue, annual_ad_spend, blend_tax, blend_roas
    """
    if mix is None:
        # Default: the 35/30/15/20 quarterly blend with stock clearance
        mix = {
            'skin': 0.20, 'hair': 0.10, 'perfume': 0.05,
            'jewellery': 0.30, 'crystals': 0.15, 'stock_clearance': 0.20,
        }

    profit_per_order = blended_profit_per_order(mix, profits)
    daily_revenue = orders_per_day * aov
    daily_profit = orders_per_day * profit_per_order
    profit_pct = profit_per_order / aov * 100

    if roas is None:
        roas = blended_roas(mix, roas_map, aov)
    daily_ad_spend = daily_revenue / roas if roas > 0 else 0

    if tax_pct is None:
        tax_pct = blended_tax(mix, taxes)

    return {
        'orders_per_day': orders_per_day,
        'aov': aov,
        'profit_per_order': round(profit_per_order, 2),
        'profit_pct': round(profit_pct, 2),
        'daily_revenue': round(daily_revenue, 0),
        'daily_profit': round(daily_profit, 0),
        'daily_ad_spend': round(daily_ad_spend, 0),
        'monthly_revenue': round(daily_revenue * 30, 0),
        'monthly_profit': round(daily_profit * 30, 0),
        'monthly_ad_spend': round(daily_ad_spend * 30, 0),
        'annual_revenue': round(daily_revenue * 365, 0),
        'annual_profit': round(daily_profit * 365, 0),
        'annual_ad_spend': round(daily_ad_spend * 365, 0),
        'blended_roas': round(roas, 2),
        'blended_tax': round(tax_pct, 2),
        'delivery_pct': delivery_pct,
        'mix': mix,
    }


def orders_needed_for_profit(target_profit: float,
                              days: int,
                              mix: dict = None,
                              aov: float = 850,
                              profits: dict = None) -> int:
    """Reverse-solve: how many orders/day do we need to hit a profit target?"""
    if mix is None:
        mix = {
            'skin': 0.20, 'hair': 0.10, 'perfume': 0.05,
            'jewellery': 0.30, 'crystals': 0.15, 'stock_clearance': 0.20,
        }
    profit_per_order = blended_profit_per_order(mix, profits)
    if profit_per_order <= 0:
        raise ValueError("Blended profit/order must be > 0")
    return int(round(target_profit / (profit_per_order * days)))


def format_inr(value: float) -> str:
    """Format ₹ values in lakhs (L) or crores (cr) for display."""
    if value >= 1e7:
        return f"₹{value/1e7:.2f} cr"
    elif value >= 1e5:
        return f"₹{value/1e5:.2f} L"
    elif value >= 1e3:
        return f"₹{value/1e3:.0f} K"
    return f"₹{value:.0f}"


if __name__ == '__main__':
    # Example: 10,000 orders/day at the proposed blend
    result = compute_profit(orders_per_day=10000, delivery_pct=82)
    print(f"Orders/Day: {result['orders_per_day']:,}")
    print(f"Profit/Order: {format_inr(result['profit_per_order'])}")
    print(f"Profit %: {result['profit_pct']}%")
    print(f"Daily Profit: {format_inr(result['daily_profit'])}")
    print(f"Monthly Profit: {format_inr(result['monthly_profit'])}")
    print(f"Annual Profit: {format_inr(result['annual_profit'])}")
    print(f"Blended ROAS: {result['blended_roas']}x")
    print(f"Blended Tax: {result['blended_tax']}%")
    print()

    # Example: lever check
    print("=== LEVER CHECK at ROAS 2.75 / Tax 7% / Delivery 85% ===")
    levers = lever_check(roas=2.75, tax_pct=7, delivery_pct=85)
    print(levers['verification'])
    print(f"Profit %: {levers['profit_pct']}%")
    print()

    # Example: reverse-solve
    print("=== ORDERS NEEDED FOR ₹6 CR/MONTH PROFIT ===")
    orders = orders_needed_for_profit(target_profit=6e7, days=30)
    print(f"Required orders/day: {orders:,}")
