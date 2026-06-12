"""
Product catalogue — keyword → (category, product_name)
Built from STUDD MUFFYN STANDARD SHEET_NEW (MASTER_CATALOGUE)
Used by campaign_tracker_builder.py for campaign name parsing
"""

import re

# ── Category map (SKU_CATEGORY → our report category) ────────────────────────
CATEGORY_REMAP = {
    'SKIN CARE':        'Skin',
    'HAIR CARE':        'Hair',
    'CRYSTAL HOME DECOR': 'Crystal',
    'CRYSTAL':          'Crystal',
    'JEWELLERY':        'Jewellery',
    'NUTRACEUTICALS':   'Nutraceuticals',
    'PERFUME':          'Fragrance',
    'CLOTHING':         'Clothing',
    'DENTAL CARE':      'Wellness',
    'OTHER':            'Other',
}

# ── Product keyword rules ─────────────────────────────────────────────────────
# Order matters — first match wins
# Each entry: ([keywords in campaign name], product_display_name, category)
# Keywords are matched against lowercased campaign name with _ and - as spaces

PRODUCT_RULES = [

    # ── SKIN CARE ─────────────────────────────────────────────────────────────
    (['am_pm', 'ampm', 'am pm', 'am/pm'],               'AM/PM Booster Kit',            'Skin'),
    (['time_reversal', 'time reversal', 'trifecta'],     'Time Reversal Trifecta',       'Skin'),
    (['triderma', 'tri_derma'],                          'Triderma Bright',              'Skin'),
    (['24k', 'gold_serum', 'gold serum'],                '24K Gold Face Serum',          'Skin'),
    (['goat_milk', 'goatmilk', 'goat milk', 'under_eye', 'undereye'],
                                                         'Under Eye Goat Milk',          'Skin'),
    (['pitglow', 'pit_glow', 'pit glow', 'roll_on', 'rollon'],
                                                         'Pit Glow Roll On',             'Skin'),
    (['sunkissed', 'sun_kissed', 'sun kissed'],          'Sun Kissed Mousse',            'Skin'),
    (['hyaluronic', 'hyaluroni', 'nialuronic'],          'Hyaluronic Gel',               'Skin'),
    (['lipbright', 'lip_bright', 'lip bright',
      'lip_reel', 'lip.*reel', 'skin.*lip'],             'Lip Bright',                   'Skin'),
    (['berberine'],                                      'Berberine Capsules',           'Nutraceuticals'),
    (['charbigone', 'charbi_gone', 'charbi gone', 'charbi'],
                                                         'Charbi Gone Capsules',         'Nutraceuticals'),
    # — new rules added 06-10 (clearing up Unmapped backlog) —
    (['neck_bright', 'neckbright', 'neck bright'],       'Neck Bright',                  'Skin'),
    (['brow_grow', 'browgrow', 'brow grow'],             'Brow Grow',                    'Skin'),
    (['d-tan', 'd_tan', 'detan', 'dtan'],                'D-Tan Products',               'Skin'),
    (['botox'],                                          'Botox',                        'Skin'),
    (['kapikachu'],                                      'Kapikachu Capsules',           'Nutraceuticals'),
    (['harmone.*harmony', 'hormone.*harmony',
      'harmone.*balance', 'hormone.*balance'],           'Hormone Harmony',              'Nutraceuticals'),
    (['hair.*wand', 'magic.*wand'],                      'Magic Hair Wand',              'Hair'),
    (['karungali'],                                      'Karungali Malai',              'Crystal'),
    (['ks_spf', 'ks spf', 'spf.*mousse', 'mousse.*spf'], 'Sun Kissed Mousse',            'Skin'),
    (['summer.*sale', 'summersale'],                     'Summer Sale Combo',            'Mix'),
    (['saree', 'kurta'],                                 'Saree / Kurta Sale',           'Mix'),
    (['24/7', '24_7', '24by7'],                          '24/7 Skin Combo',              'Skin'),
    # —
    (['pigmentation_combo', 'pigmentation combo'],       'AM PM Pigmentation Combo',     'Skin'),
    (['pigmentation'],                                   'Pigmentation Products',        'Skin'),
    (['vitamin_c', 'vitamin c'],                         'Vitamin C Serum',              'Skin'),
    (['ashwabutin'],                                     'Ashwabutin Face Products',     'Skin'),
    (['glycothione'],                                    'Glycothione Products',         'Skin'),
    (['niacinamide'],                                    'Niacinamide Wash',             'Skin'),

    # ── HAIR CARE ─────────────────────────────────────────────────────────────
    (['xtremehair', 'xtreme_hair', 'xtreme hair', 'hair_booster', 'hair booster', 'xtreme.*booster'],
                                                         'Xtreme Hair Booster Kit',      'Hair'),
    (['phusphus', 'phus_phus', 'phus phus', 'allday.*mist', 'hair.*mist', 'rice.*mist'],
                                                         'Phus Phus Hair Mist',          'Hair'),
    (['xtreme.*oil', 'hair.*oil'],                       'Xtreme Hair Oil',              'Hair'),
    (['dandragone', 'dandruff'],                         'Dandragone Anti-Dandruff',     'Hair'),
    (['hair_serum', 'hair serum', 'rice.*serum'],        'Hair Serum',                   'Hair'),

    # ── CRYSTAL & HOME DECOR ──────────────────────────────────────────────────
    (['pyrite.*bracelet', 'bracelet.*pyrite'],           'Pyrite Bracelet',              'Crystal'),
    (['prem_sutra', 'prem sutra', 'prem.*sutra'],        'Prem Sutra Bracelet',          'Crystal'),
    (['nazar_sutra', 'nazar sutra', 'nazar.*sutra'],     'Nazar Sutra',                  'Jewellery'),
    (['selenite.*coaster', 'coaster.*selenite'],         'Selenite Coaster',             'Crystal'),
    (['sunflower.*selenite', 'selenite.*plate'],         'Sunflower Selenite Plate',     'Crystal'),
    (['hourglass'],                                      'Crystal Hourglass',            'Crystal'),
    (['7.*horse', 'horses.*frame', 'richie.*rich', 'richierich'],
                                                         '7 Horses / Richie Rich',       'Crystal'),
    (['peacock.*frame', 'frame.*peacock'],               'Peacock Frame',                'Crystal'),
    (['hanuman.*selenite', 'hanuman'],                   'Hanuman Crystal',              'Crystal'),
    (['ganesha.*selenite', 'ganesha'],                   'Ganesha Crystal',              'Crystal'),
    (['money.*bowl', 'geode'],                           'Money Bowl with Geode',        'Crystal'),
    (['miniature'],                                      'Crystal Miniature Series',     'Crystal'),
    (['pyrite.*half', 'half.*n.*half', 'half_n_half'],   'Pyrite Half & Half',           'Crystal'),
    (['bracelet'],                                       'Crystal Bracelet',             'Crystal'),
    (['selenite'],                                       'Selenite Products',            'Crystal'),
    (['pyrite'],                                         'Pyrite Products',              'Crystal'),
    (['crystal.*clock', 'clock'],                        'Crystal Clock',                'Crystal'),
    (['crystal.*frame', 'frame'],                        'Crystal Frame',                'Crystal'),

    # ── JEWELLERY ─────────────────────────────────────────────────────────────
    # 'wanda' removed (06-08): it's a creative team name, not a product
    # indicator — was mis-tagging Skin ads with 'wanda' in their name as
    # Jewellery (e.g. ntn_adv_wanda_retarget_conv_skin_offer_reel matched
    # 'wanda' before reaching Skin-catchall below). Compound names like
    # wanda_jewellery still match via 'jewellery' substring.
    (['24k.*chain', 'gold.*chain', 'gold.*necklace', '24k.*pendant',
      'jewellery', 'jewelry', 'earring', '_ring_', 'ring_'],
                                                         'Jewellery',                    'Jewellery'),

    # ── FRAGRANCE / PERFUME ───────────────────────────────────────────────────
    (['oxytocin', 'oxyto'],                              'Oxytocin EDP',                 'Fragrance'),
    (['serotonin'],                                      'Serotonin EDP',                'Fragrance'),
    (['endorphin'],                                      'Endorphins EDP',               'Fragrance'),
    (['dopamine'],                                       'Dopamine EDP',                 'Fragrance'),
    (['infinity', 'rich_brat', 'thirst_trap', 'xoxo', 'lapis', 'carnelian', 'tourmaline'],
                                                         'Fragrance EDP',                'Fragrance'),
    (['solid.*perfume', 'perfume.*solid'],               'Solid Perfume',                'Fragrance'),
    (['fragrance', 'perfume', 'edp', 'attar', 'roll.*on.*perf'],
                                                         'Fragrance',                    'Fragrance'),

    # ── NUTRACEUTICALS ────────────────────────────────────────────────────────
    (['multivitamin'],                                   'Multivitamin',                 'Nutraceuticals'),
    (['biotin'],                                         'Biotin Capsules',              'Nutraceuticals'),
    (['capsule', 'nutra'],                               'Nutraceuticals',               'Nutraceuticals'),

    # ── SKIN MISC (catch-all for known skin patterns) ─────────────────────────
    (['dirtoff', 'dirt_off'],                            'Dirt Off Facewash',            'Skin'),
    (['peptide', 'peptides'],                            'Peptide Products',             'Skin'),
    (['app.*offer', 'offer.*app'],                       'App Offer',                    'Skin'),
    (['sunmousse', 'sun.*mousse'],                       'Sun Kissed Mousse',            'Skin'),
    # 'Lip Products' merged into 'Lip Bright' above (06-12) — operator
    # treats them as one SKU group; no need for a separate catch-all row
    # in the budget allocation table.
    (['timereversal', 'time.*reversal'],                 'Time Reversal Trifecta',       'Skin'),
    (['seg12', 'seg13', 'routine.*upgrade', 'premium.*combo'],
                                                         'Skin Retarget Combo',          'Skin'),
    (['hair.*growth', 'growth.*combo'],                  'Hair Growth Combo',            'Hair'),
    (['astro.*re', '^mad2_astro'],                       'Astro Products',               'Mix'),

    # ── CRYSTAL MISC ──────────────────────────────────────────────────────────
    (['coaster'],                                        'Crystal Coaster',              'Crystal'),
    (['sleek.*crystal', 'crystal.*sleek'],               'Sleek Crystal',                'Crystal'),
    (['deer.*plate', 'plate.*deer'],                     'Crystal Deer Plate',           'Crystal'),
    (['owl'],                                            'Crystal Owl',                  'Crystal'),
    (['money.*magnet'],                                  'Money Magnet Crystal',         'Crystal'),
    (['rose.*quartz', 'quartz.*rose'],                   'Rose Quartz Crystal',          'Crystal'),
    (['sutra.*range', 'range.*sutra'],                   'Sutra Range Mix',              'Crystal'),
    (['crystal.*mix', 'mix.*crystal'],                   'Crystal Mix',                  'Crystal'),
    (['inc180dp', 'exc180dp', 'exc180imp', 'inc180imp'], 'Crystal Products',             'Crystal'),
    (['crystal'],                                        'Crystal Products',             'Crystal'),

    # ── ASTRO / BOT ───────────────────────────────────────────────────────────
    (['astro.*bot', 'astro_bot', 'chatbot'],             'Astro Bot',                    'Mix'),
    (['astro.*destiny', 'destiny.*report'],              'Astro Destiny Report',         'Mix'),
    (['astro.*interest', 'astro.*tof'],                  'Astro Products',               'Mix'),

    # ── RETARGET APP / NEW COASTERS ───────────────────────────────────────────
    (['retarget.*app', 'app.*retarget'],                 'App Retarget',                 'Mix'),
    (['new.*coaster', 'coaster.*new'],                   'New Coasters',                 'Crystal'),

    # ── DS (Dynamic/Structured — Skin/Mix) ────────────────────────────────────
    (['ds.*skin', 'skin.*ds'],                           'DS Skin Mix',                  'Skin'),
    (['ds.*hair', 'hair.*ds'],                           'DS Hair Mix',                  'Hair'),
    (['ds.*crystal', 'crystal.*ds'],                     'DS Crystal Mix',               'Crystal'),
    (['ds.*mix', 'ds.*cbo', 'ds.*conv', 'ds.*tof'],     'DS Mix',                       'Mix'),

    # ── OFFER / COMBO (multi-product — treated as Mix) ────────────────────────
    (['offer'],                                          'Offer/Combo',                  'Mix'),
]

# ── Segment keyword rules ─────────────────────────────────────────────────────
SEGMENT_RULES = [
    (['seg13', 'loyal', 'loyal_buyers'],                 'NTN | Loyal Buyers 3+'),
    (['seg12', 'repeat', 'repeat_buyer'],                'NTN | Repeat Buyers 2+'),
    (['seg11', 'recent.*0.30', 'recent_0'],              'NTN | Recent 0-30D'),
    (['seg6', 'sm_only', 'sm only'],                     'NTN | SM Only Buyers'),
    (['seg4', '365', 'lapsed'],                          'NTN | Crystal Buyers — 365+ Days (Lapsed)'),
    (['seg2', '30.*180', '30_to_180'],                   'NTN | Crystal Buyers — 30 to 180 Days'),
    (['seg16'],                                          'NTN | SM Only Buyers'),
    (['seg1_', '_seg1'],                                 'NTN | Recent 0-30D'),
    (['recent_31', '31.60'],                             'NTN | Recent 31-60D'),
    (['xtreme.*hair.*1', 'product.*xtreme'],             'NTN | Product: Xtreme Hair (1+)'),
    (['lip_bright.*buyer', 'pit_glow.*buyer'],           'NTN | Lip Bright + Pit Glow Buyers'),
    (['crystal_buyer'],                                  'NTN | Crystal Buyers — 30 to 180 Days'),
    (['sale_30', 'sale30'],                              'Sale 30'),
    (['visitor_180', '180_day', '180d'],                 'Visitor 180 Day'),
    (['visitor_30', '30_day'],                           'Visitor 30 Day'),
    (['180imp', 'impression_30', 'imp_30'],              'Impression 30 retarget'),
    (['impression_7', 'imp_7', '7_imp'],                 'Impression 7 retarget'),
    (['add_to_cart', 'atc_7', 'cart.*7'],                'ADD to cart 7 days'),
    (['add_to_cart', 'atc_30', 'cart.*30'],              '30 days add to cart'),
    (['atc'],                                            'ADD to cart 7 days'),
    (['lla', 'lookalike'],                               'Lookalike (IN, 1%) - prepaid purchase'),
    (['c1_c4', 'c9_', '_c9'],                            'C1_C4 _Meta'),
    (['lifetime'],                                       'Lifetime audience'),
    (['loose'],                                          'Loose'),
    (['ntn.*retarget', 'retarget.*ntn'],                 'NTN | Recent 0-30D'),
    (['impression_30', 'imp.*30'],                       'Impression 30 retarget'),
]


def _norm(s):
    """Normalize campaign name for matching."""
    return s.lower().replace('-', '_').replace(' ', '_')


def derive_product_and_category(campaign_name):
    """
    Returns (product, category) from campaign name.
    Uses PRODUCT_RULES — first match wins.
    Mix/Multiple campaigns (loose, multiple, mix with no specific product) → ('Mix/Multiple', 'Mix')
    """
    n = _norm(campaign_name)

    # Check for mix/multiple first ONLY if no specific product found below
    is_mix_candidate = any(kw in n for kw in ['_mix_', '_multiple_', 'loose_', '_loose', 'multi_cat', 'all_category', 'all_products', 'partnership', 'master.*loose'])

    for keywords, product, category in PRODUCT_RULES:
        for kw in keywords:
            if re.search(kw.replace(' ', '_'), n):
                return product, category

    # No specific product matched
    if is_mix_candidate:
        return 'Mix/Multiple', 'Mix'

    return 'Unmapped', 'Unmapped'


def derive_category_only(campaign_name):
    _, cat = derive_product_and_category(campaign_name)
    return cat


def derive_segment(campaign_name):
    n = _norm(campaign_name)
    for keywords, segment in SEGMENT_RULES:
        for kw in keywords:
            if re.search(kw.replace(' ', '_'), n):
                return segment

    # Fallbacks based on type
    if any(k in n for k in ['retarget', '_rtg_', '_rgt_', 'bof_']):
        return 'Impression 30 retarget'
    if any(k in n for k in ['_ds_', 'ds_', '_ds/']):
        return 'DS'
    return 'Lifetime audience'


def derive_type(campaign_name):
    n = _norm(campaign_name)
    if any(kw in n for kw in ['retarget', '_rtg_', '_rgt_', 'retargeting', 'bof_web', 'bof_bof', '_bof_']):
        return 'Retarget'
    return 'Sales'


# ── Category-head taxonomy (used by scripts/category_reports.py) ─────────────
# The ops team has internal heads each looking after one category. Their preferred
# names differ slightly from derive_product_and_category()'s output:
#   - Crystal is split into 'Crystal Home Decor' (frames, plates, hourglasses,
#     coasters, idols, clocks) vs 'Crystal Accessory' (bracelets, sutras,
#     keyrings, pendants, rudraksha)
#   - Jewellery surfaces as '24K Jewellery'
#   - Fragrance surfaces as 'Perfumes'
#   - 'Aibot' is a separate category
def derive_category_v2(campaign_or_ad_name):
    """Returns one of:
      Skin / Hair / Crystal Home Decor / Crystal Accessory / 24K Jewellery /
      Perfumes / Aibot / Nutraceuticals / DS / Other
    """
    n = _norm(campaign_or_ad_name)

    # DS team campaigns — separate territory (Madhav Ji's), not NTN. Keep
    # them visible in the dashboard so spend isn't lost, but bucket them
    # under 'DS' so they don't pollute NTN category roll-ups.
    if n.startswith('ds_') or '_ds_bof_' in n or '_ds_tof_' in n:
        return 'DS'

    # Aibot — distinct sub-brand. Includes the 'astro_destiny' chatbot/report
    # product family and mad2_astro_* interest-audience variants. This block
    # must run before the Crystal Home Decor block (which previously claimed
    # astro_destiny incorrectly, mis-routing ~250 SM ad-day rows).
    if any(kw in n for kw in ['aibot', 'ai_bot', 'aiblot',
                              'astro_destiny', 'destiny_report', 'destiny_reel',
                              'astro_chatbot', 'astro_ab_', 'astro_int_',
                              'astro_interest', 'astro_interested',
                              '_astro_', 'mad2_astro']):
        return 'Aibot'

    # 24K Jewellery — must check before Skin (24k_gold_serum is skincare,
    # not jewellery). Bare 'jewellery' included because SM Fragrance 01 runs
    # ₹14L/wk in jewellery campaigns that don't carry an explicit 24k_ prefix
    # — operator confirmed they're all 24K stock.
    if any(kw in n for kw in ['gold_jewellery', '24k_jewellery', '24k_chain', '24k_pendant',
                              'gold_chain', 'gold_necklace', 'wanda_jewellery', 'wanda_24k',
                              'jewellery', 'jewelry']):
        return '24K Jewellery'

    # Perfumes / Fragrance
    if any(kw in n for kw in ['perfume', 'fragrance', 'edp', 'edt', 'cologne', 'attar',
                              'rich_brat', 'inde_sleek', 'eau_de']):
        return 'Perfumes'

    # Skin (covers most skincare keywords)
    # Added 9 Jun 2026: skin products that were previously falling into
    # Other / Crystal Home Decor / DS because their root keyword wasn't here:
    #   neck_bright / 24k_gold (bare) / sun_mousse / glass_skin / d_tan /
    #   bb_spf / anti_acne / peptide / face_serum / cleanser / sunscreen
    # NOTE: 'booster_kit' is intentionally NOT added — it'd mis-route
    # `xtremehairboosterkit_*` to Skin (Hair is checked after Skin). The
    # existing 'am_pm' / 'ampm' keywords already catch AM/PM Booster Kit ads.
    if any(kw in n for kw in [
            'skin', 'am_pm', 'ampm', 'sunkissed', 'sun_kissed', 'goat_milk',
            'goatmilk', 'triderma', 'tri_derma', '24k_gold_serum',
            'pitglow', 'pit_glow', 'lipbright', 'lip_bright', 'hyaluronic',
            'pigmentation', 'vitamin_c', 'ashwabutin', 'glycothione',
            'niacinamide', 'time_reversal', 'trifecta', 'undereye',
            'under_eye', 'roll_on', 'rollon', 'glutabright', 'botox',
            'eyebrow', 'brow_grow', 'eye_care',
            # additions (Skin-only fix, 9 Jun 2026):
            'neck_bright', 'neckbright',
            # Only 24K Gold SERUM is Skin. Bare '24k_gold' was too broad —
            # a future 24K gold jewellery ad (e.g. `paras_24k_gold_reel`
            # without _serum) would have wrongly landed in Skin. Now only
            # explicit serum variants match; bare 24k_gold ads fall to Other,
            # which is the safe default.
            'gold_serum', 'gold_face_serum', 'goldserum', '24kgold_serum',
            'sun_mousse', 'sunmousse',
            'glass_skin', 'glassskin', 'cleanser',
            'd_tan', 'dtan',
            'bb_spf', 'bb_cream',
            'anti_acne', 'antiacne',
            'peptide',
            'face_serum', 'face_wash', 'facewash',
            'moisturizer', 'moisturiser',
            'sunscreen', 'sun_screen']):
        return 'Skin'

    # Hair
    if any(kw in n for kw in ['hair', 'phusphus', 'phus_phus', 'xtremehair', 'xtreme_hair',
                              'hairmist', 'hair_mist', 'dandragone', 'dandruff', 'rice_mist',
                              'hair_oil', 'hair_serum', 'hair_growth', 'hair_booster']):
        return 'Hair'

    # Nutraceuticals (capsules, supplements)
    if any(kw in n for kw in ['berberine', 'charbigone', 'charbi_gone', 'charbi', 'capsule',
                              'capsules', 'supplement', 'vitamin', 'ashwagandha']):
        return 'Nutraceuticals'

    # Crystal — Accessory (worn on body)
    if any(kw in n for kw in ['bracelet', 'sutra', 'keyring', 'pendant', 'necklace',
                              'mala', 'rudraksha', 'half_n_half', 'wrist',
                              # Brand/product names that are accessories
                              'richie_rich', 'richierich', 'riche_rich',
                              'anxiety_free', 'anxiety free',
                              'evil_eye', 'evil eye',
                              'tiger_eye_bracelet']):
        return 'Crystal Accessory'

    # Crystal — Home Decor (frames, idols, plates, clocks, etc.)
    if any(kw in n for kw in ['frame', 'coaster', 'plate', 'clock', 'hourglass', 'wall_art',
                              'idol', 'statue', 'horseclock', 'horse_clock', 'horseframe',
                              'horse_frame', 'peacock_frame', 'money_bowl', 'geode',
                              'tortoise', 'pyramid', 'butterfly', 'tree', 'sphere', 'wand',
                              'hanuman', 'ganesha', 'shiva', 'durga', 'lakshmi',
                              'leaf', 'mountain', 'cluster', 'tower',
                              # Additions from operator name patterns
                              'lapis_peacock', 'lapiz_peacock', 'peacock_plate',
                              'trishul', 'mahadev', 'kamdhenu',
                              'koi_fish', 'koifish', 'horses_of_harmony',
                              'pyrite_owl', 'baby_buddha', 'buddha',
                              'nazar_protection', 'wealth_success', 'fortune_flow',
                              # 'astro_destiny' removed — it's an Aibot product, see Aibot block above
                              'tree_of_life',
                              'horses', 'horse']):
        return 'Crystal Home Decor'

    # Generic crystal mention without specific item type → default to Home Decor
    if any(kw in n for kw in ['crystal', 'pyrite', 'selenite', 'amethyst', 'rose_quartz',
                              'citrine', 'obsidian', 'tigereye', 'opalite', 'hematite',
                              'hematarite']):
        return 'Crystal Home Decor'

    return 'Other'


if __name__ == '__main__':
    # Test against known campaigns
    tests = [
        'ntn_adv_web_conv_sales_skin_am_pm_booster_mix_200326_R',
        'ntn_adv_web_conv_rtg_crystal_pyrite_bracelet_mix_310326_R',
        'ds_bof_bof_web_conv_retarget_mix_catalog_040426',
        'ntn_adv_conv_web_loose_test_camp_2_multiple_mix_190426',
        'ds_adv1_web_conv_sales_partnership_mix_creative_200326',
        'ntn_adv_web_conv_sales_neutra_charbigone_reel_090326',
        'ntn_adv_web_conv_hair_phusphus_combo_mix_300326_ant',
        'ntn_adv_wanda_conv_jewellery_reel_clp_140426_R',
        'ds_tof_lla_web_conv_all_products_mix_creative_100426',
        'ntn_adv_web_conv_master_sale_crystal_richie_rich_reel_070426',
        'ds/ntn_adv_web_master_conv_crystal_peacock_frame_070426_R',
        'ntn_adv_web_conv_rgt_skin_time_reversal_reel_310326',
        'ntn_adv_web_conv_testing_multiple_products_mix_150426',
        'ntn_master_dv_web_rtg_conv_multiple_reel_060426',
    ]
    print(f"{'Campaign':<60} {'Product':<30} {'Category':<15} {'Type':<10} {'Segment'}")
    print('-'*140)
    for t in tests:
        prod, cat = derive_product_and_category(t)
        typ = derive_type(t)
        seg = derive_segment(t)
        print(f"{t[:58]:<60} {prod:<30} {cat:<15} {typ:<10} {seg}")
