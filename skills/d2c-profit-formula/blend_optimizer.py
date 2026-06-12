"""
Blend Optimizer — Find category mix that hits target EBITDA.

Given a target profit % and constraints (max stock clearance share, min Skin etc.),
find feasible category allocations.
"""

from profit_formula import (
    DEFAULT_CATEGORY_PROFIT,
    DEFAULT_CATEGORY_TAX,
    blended_profit_per_order,
    blended_tax,
)


# Standard pre-built blends used in planning
PRESETS = {
    'current_baseline': {
        # Estimated current Studd Muffyn mix (drags margin to ~6-10%)
        'skin': 0.25, 'hair': 0.10, 'perfume': 0.03,
        'jewellery': 0.30, 'crystals': 0.17, 'stock_clearance': 0.15,
    },
    'proposed_20_ebitda': {
        # Skin 35% / Crystals 35% / Hair 15% / Perfumes 10% / Neutra 5%
        # The presentation slide blend
        'skin': 0.35, 'hair': 0.15, 'perfume': 0.10,
        'jewellery': 0.0, 'crystals': 0.35, 'neutra': 0.05,
    },
    'quarterly_with_clearance': {
        # 35% (Skin+Hair+Perfume) + 30% Jewellery + 15% Crystals + 20% Stock Clearance
        'skin': 0.20, 'hair': 0.10, 'perfume': 0.05,
        'jewellery': 0.30, 'crystals': 0.15, 'stock_clearance': 0.20,
    },
    'tax_optimized_crystals_heavy': {
        # Heavy on Crystals + Neutra for low blended tax
        'skin': 0.08, 'hair': 0.07, 'perfume': 0.05,
        'jewellery': 0.0, 'crystals': 0.65, 'neutra': 0.15,
    },
    'festive_push': {
        # Q3 festive mix — Jewellery + Skin gifting heavy
        'skin': 0.30, 'hair': 0.05, 'perfume': 0.15,
        'jewellery': 0.35, 'crystals': 0.15,
    },
}


def evaluate_blend(mix: dict, aov: float = 850) -> dict:
    """Quick evaluation of a blend: profit/order, EBITDA %, blended tax."""
    profit_per_order = blended_profit_per_order(mix)
    return {
        'profit_per_order': round(profit_per_order, 2),
        'ebitda_pct': round(profit_per_order / aov * 100, 2),
        'blended_tax': round(blended_tax(mix), 2),
        'mix': mix,
    }


def compare_blends(blend_names: list = None, aov: float = 850):
    """Print a comparison table of preset blends."""
    blend_names = blend_names or list(PRESETS.keys())
    print(f"{'Blend':<32} {'Profit/Order':>14} {'EBITDA %':>10} {'Tax %':>8}")
    print("-" * 70)
    for name in blend_names:
        if name not in PRESETS:
            continue
        ev = evaluate_blend(PRESETS[name], aov)
        print(f"{name:<32} ₹{ev['profit_per_order']:>12.0f} "
              f"{ev['ebitda_pct']:>8.1f}% {ev['blended_tax']:>6.1f}%")


def search_blend_for_target(target_ebitda_pct: float,
                             max_stock_clearance: float = 0.20,
                             min_skin: float = 0.05,
                             min_jewellery: float = 0.10,
                             aov: float = 850,
                             max_iterations: int = 1000) -> dict:
    """
    Grid-search a feasible category mix that hits the target EBITDA %.

    Returns the closest-matching blend and its evaluation.
    """
    target_profit_per_order = target_ebitda_pct / 100 * aov

    best = None
    best_gap = float('inf')

    # Grid over reasonable share ranges
    for jewellery in [round(x * 0.05, 2) for x in range(int(min_jewellery * 20), 14)]:
        for crystals in [round(x * 0.05, 2) for x in range(2, 14)]:
            for stock in [round(x * 0.05, 2) for x in range(0, int(max_stock_clearance * 20) + 1)]:
                for skin in [round(x * 0.05, 2) for x in range(int(min_skin * 20), 10)]:
                    for hair in [round(x * 0.05, 2) for x in range(0, 6)]:
                        perfume = 1 - jewellery - crystals - stock - skin - hair
                        if perfume < 0 or perfume > 0.25:
                            continue
                        mix = {
                            'skin': skin, 'hair': hair, 'perfume': round(perfume, 2),
                            'jewellery': jewellery, 'crystals': crystals,
                            'stock_clearance': stock,
                        }
                        ppo = blended_profit_per_order(mix)
                        gap = abs(ppo - target_profit_per_order)
                        if gap < best_gap:
                            best_gap = gap
                            best = mix

    if best is None:
        return {'error': 'No feasible blend found'}

    return evaluate_blend(best, aov)


if __name__ == '__main__':
    print("=== ALL PRESETS ===")
    compare_blends()
    print()

    print("=== SEARCH: BLEND TARGETING 22% EBITDA ===")
    result = search_blend_for_target(target_ebitda_pct=22)
    print(f"Best mix found:")
    for cat, share in result['mix'].items():
        print(f"  {cat:<20} {share*100:>5.1f}%")
    print(f"Profit/Order: ₹{result['profit_per_order']}")
    print(f"EBITDA: {result['ebitda_pct']}%")
    print(f"Blended Tax: {result['blended_tax']}%")
