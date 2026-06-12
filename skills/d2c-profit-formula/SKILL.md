---
name: d2c-profit-formula
description: D2C profit planning toolkit for Studd Muffyn. Computes EBITDA, optimizes category blend, projects daily/weekly/monthly/quarterly profit trajectories, and validates lever sensitivity (ROAS, Tax, Delivery, Order Volume) against per-order economics. Use when planning revenue targets, allocating Meta budget across categories, modeling stock clearance impact, or building multi-month profit roadmaps.
---

# D2C Profit Formula Skill

A unified math + planning framework for D2C profit modeling, built specifically for the Studd Muffyn portfolio (Meta-led D2C with skin, hair, jewellery, crystals, perfumes, neutra).

## When to Use

Invoke this skill for any of:
- "How many orders to hit ₹X profit?"
- "What's the right category blend for Y% EBITDA?"
- "Build a 3/10/12-month profit planner"
- "Verify lever combinations against target margin"
- "Model stock clearance impact on blended profit"
- "What ROAS / delivery / tax do I need to reach 20% EBITDA?"

## Core Formula

```
Profit % = 6 (current-mix base)
        + (ROAS - 2.5) × 10        # Each +0.1 ROAS = +1% profit
        + (8 - Tax %)              # Each -1% tax = +1% profit
        + (Delivery % - 80)        # Each +1% delivery = +1% profit
        + Mix Shift (varies)       # See blend_optimizer.py
```

**Important:** The "lever sensitivities" above are calibrated against Studd Muffyn's actual unit economics. The base 6% assumes the current portfolio (with stock clearance + loss-makers dragging margin). A mix shift to "winners only" adds another +13% margin on top.

## Files

| File | Purpose |
|---|---|
| `profit_formula.py` | Core math: per-order economics, lever math, profit calculations |
| `blend_optimizer.py` | Category mix optimization to hit target EBITDA |
| `quarterly_planner.py` | Day/week/month/quarter trajectory generator |
| `example_inputs.json` | Sample input scenarios (5K, 10K, 15K orders/day targets) |

## Quick Usage

```python
from skills.d2c_profit_formula.profit_formula import compute_profit

result = compute_profit(
    orders_per_day=10000,
    roas=2.75,
    tax_pct=7,
    delivery_pct=85,
    aov=850,
    mix={
        'skin': 0.20, 'hair': 0.10, 'perfume': 0.05,
        'jewellery': 0.30, 'crystals': 0.15, 'stock_clearance': 0.20
    }
)
# Returns: { 'profit_pct': 22.9, 'daily_profit': 1938000, 'monthly_profit': 58140000, ... }
```

## Category Per-Order Profit Reference

| Category | Avg Profit/Order ₹ | Tax % | Typical ROAS |
|---|---|---|---|
| Skin (winners only) | 185 | 18% | 3.0x |
| Hair (Xtreme + Phus Phus) | 165 | 18% | 3.35x |
| Perfumes (B1G1/B2G1) | 205 | 18% | 2.3x |
| Jewellery (China Pendants heavy) | 290 | 4% | 2.5x |
| Crystals (Peacock + Selenite) | 240 | 4% | 2.5x |
| Stock Clearance (bundle/B2B) | 40 | ~12% | 1.5x |

## Levers Cheat Sheet

| Lever Move | Profit Impact |
|---|---|
| ROAS +0.1 | +1% |
| Tax -1% | +1% |
| Delivery +1% | +1% |
| Mix to winners-only | +13% (vs base) |
| Mix to China Pendants (vs Velore) | +₹40/order in Jewellery slice |
| Stock clearance via bundle | +₹40/order vs standalone (₹0) |

## Critical Constraints

1. **Max orders/day: 15,000** (ops cap with current infra; needs 3PL beyond)
2. **Tax floor: ~7%** blended (requires Crystals+Neutra to dominate mix)
3. **ROAS ceiling: ~3.5x blended** sustainable (above this requires elite creative discipline)
4. **Delivery ceiling: ~85%** without major ops investment (premium courier tier)

## When NOT to Use

- General D2C strategy questions (use general knowledge)
- Meta API queries about live campaigns (use existing scripts in `scripts/`)
- Shopify-specific reporting (use `scripts/utm_sales_report.py`)

## Output Conventions

- All ₹ amounts in INR
- Lakhs (L) for daily/weekly figures
- Crores (cr) for monthly/quarterly/annual
- Margin % expressed as integer or 1 decimal (e.g., 22.9%)
- Orders expressed with comma thousands separator (15,000)

## Version

v1.0 — built from 60+ planning iterations across May-June 2026 sessions with Sonia (Founder).
