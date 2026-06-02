"""
System config — internal pipeline tuning, NOT user settings.

Why this file exists: these values used to live in user_data/profile.yaml,
which made them visible to Claude in chat. Some Claude sessions would then
"helpfully" surface them when the user asked for their settings — leaking
internal config the user can't tune and shouldn't see.

Moving them here means:
  1. profile.yaml contains ONLY user-editable fields (capital, categories,
     channels, excludes). When Claude reads profile.yaml, there's nothing
     internal to accidentally leak.
  2. To change one of these values, you edit this file directly — same as
     model IDs in utils/models.py. Power-user tuning, not user-facing config.
  3. update_profile.py --show output is now genuinely the complete picture
     of "your settings" — no risk of mismatch.

To change a value: edit the constant below, run a fresh pipeline. There is
no CLI surface for these — by design.
"""

# === Hard floor for D2C unit economics ===
#
# Below this AOV, the combination of CAC + shipping + returns + payment fees
# eats the margin. Any idea whose hero_product price falls below this gets
# rejected at evaluation. If a category typically sells below this, the
# idea_agent prompt instructs Sonnet to bundle (multipack / kit / subscription)
# until the AOV clears the floor.
MIN_AOV_RUPEES = 800


# === How many ideas Sonnet generates per run ===
#
# Was 10 in the original cron-job version. Reduced to 5 because:
#   - 10 ideas of JSON regularly truncated mid-output (max_tokens issues)
#   - User feedback: "5 well-developed ideas > 10 shallow ones"
#   - Per-idea token cost rises non-linearly with idea count
MAX_IDEAS_PER_RUN = 5


# === Composite score weights ===
#
# How much each sub-score contributes to the final composite. Must sum to 1.0.
# These were tuned by hand against test runs — don't change without
# re-validating against a known-good run.
SCORING_WEIGHTS = {
    "demand_strength":      0.25,
    "india_launchability":  0.20,
    "capital_fit":          0.20,
    "competition":          0.15,
    "distribution_fit":     0.10,
    "arbitrage_confidence": 0.10,
}


# === Hard thresholds — ideas below these get tagged as flagged ===
#
# "Flagged" means the idea still appears in the output (with a "Why flagged"
# callout), but sorts below passed ideas. These are NOT silent drops — the
# user sees flagged ideas with reasons.
#
# - min_launchability: 1-10 score; below this means supply chain / regulation
#   / India-fit gaps too severe to ship credibly
# - min_capital_fit: 1-10 score; only used as fallback. The primary check is
#   "actual capital estimate vs user's ceiling" (see evaluation_agent)
# - max_competition_density: 1-10 raw score; above this means the category
#   is too crowded with funded incumbents to break into without a massive
#   wedge. Inverted internally to a "min headroom" check.
# - min_composite_score: floor on the weighted total. 0 = no floor.
FILTERS = {
    "min_launchability":      6,
    "min_capital_fit":        7,
    "max_competition_density": 7,
    "min_composite_score":   0.0,
}


# === Thematic bonus multipliers ===
#
# Small composite-score nudges for ideas that have certain qualities.
# Each value is added to a base multiplier of 1.0 — so an ai_formulated idea
# with score 7 gets boosted to 7 * 1.10 = 7.7 composite (a 10% bump).
# Capped per-idea so a single idea can't stack all three for 30% boost.
THEMATIC_BONUSES = {
    "ai_formulated":       0.10,  # idea uses AI in the product (not just marketing)
    "honest_transparent":  0.08,  # brand_angle = honest/transparent
    "instagrammable_visual": 0.10,  # word_of_mouth_potential >= 8
}
