"""
EvaluationAgent — scores D2C ideas on 6 dimensions and applies hard filters.
Uses Claude Haiku for single-pass structured JSON scoring.
"""
import json
import os
import re
import anthropic
from utils.signal_schema import IdeaDict
from utils.logger import get_logger
from utils.models import EVALUATION_MODEL

logger = get_logger()


def _build_founder_context(profile: dict) -> str:
    """Build the FOUNDER CONTEXT block injected into the evaluation user_prompt.

    Pulls live values from profile.yaml so every user gets scored against
    THEIR own capital ceiling, distribution profile, picked categories, and
    hard excludes — not against the original tool author's defaults.
    """
    market = profile.get("market", {})
    capital = market.get("capital_ceiling_lakhs", 20)

    # Categories: only the picked ones (weight > 0)
    cats = profile.get("categories", {})
    picked_cats = [k for k, v in cats.items() if isinstance(v, dict) and v.get("weight", 0) > 0]

    # Distribution: binary available/unavailable. (The earlier 3-tier
    # 'superpower' bucket was dropped — added complexity without enough
    # signal value.)
    dist = profile.get("distribution", {})
    available = sorted(k for k, v in dist.items() if v > 0)
    cannot_use = sorted(k for k, v in dist.items() if v == 0)

    # Hard excludes
    excludes = profile.get("hard_excludes", {})
    preset_excludes = excludes.get("preset", []) if isinstance(excludes, dict) else []
    custom_excludes = excludes.get("custom", []) if isinstance(excludes, dict) else []

    lines = ["=== FOUNDER CONTEXT ===\n"]

    lines.append(f"Capital ceiling: ₹{capital} lakh (HARD LIMIT — reject ideas needing more).\n")

    lines.append("Categories of interest:")
    lines.append(f"  Picked: {', '.join(picked_cats) if picked_cats else 'none'}")
    lines.append("  Reject ideas in categories NOT in the picked list.\n")

    lines.append("Distribution profile (use this for score_distribution):")
    if available:
        lines.append(f"  Channels the founder can use: {', '.join(available)}")
    if cannot_use:
        lines.append(f"  Channels NOT available: {', '.join(cannot_use)}")
    lines.append("  An idea whose ideal GTM relies on unavailable channels should get a low distribution_fit.\n")

    if preset_excludes or custom_excludes:
        lines.append("Hard excludes (reject ideas matching these):")
        for e in preset_excludes:
            lines.append(f"  - {e}")
        for e in custom_excludes:
            lines.append(f"  - {e}")
        lines.append("")

    lines.append("=== END FOUNDER CONTEXT ===")
    return "\n".join(lines)


def _extract_capital_high_lakhs(estimate_text: str) -> float | None:
    """Parse the high end of a capital_required_estimate string.

    Examples:
      "₹20-26L (₹10L first batch...)"  → 26.0
      "₹15-19L"                        → 19.0
      "₹15L"                           → 15.0
      "₹20–26 lakh"                    → 26.0  (note: en-dash variant)
      "Rs 22 lakh"                     → 22.0

    Returns None if no recognizable lakh value found.
    """
    if not estimate_text:
        return None
    import re
    # Strip ₹/Rs/INR prefix, normalize en-dash to hyphen
    text = estimate_text.replace("–", "-").replace("—", "-")
    # Match patterns like "20-26L", "20-26 lakh", "20 lakh", "15L"
    # Captures: optional low end, the high end, unit (L/lakh)
    matches = re.findall(r"(?:(\d+(?:\.\d+)?)\s*[-]\s*)?(\d+(?:\.\d+)?)\s*(?:L\b|lakh)", text, re.IGNORECASE)
    if not matches:
        return None
    # Take the FIRST match — that's the headline capital figure (the parenthetical
    # breakdowns after are sub-components like "first batch", "packaging", etc.)
    low_str, high_str = matches[0]
    try:
        return float(high_str)
    except ValueError:
        return None


def _extract_min_price_inr(*texts: str) -> int | None:
    """Pull the smallest rupee integer from free-text price fields.

    Handles formats like '₹899', 'Rs. 799', '799-999', '₹450 (single) / ₹1499 (bundle)'.
    Returns None if no numeric price is found — we treat that as 'unknown, do not
    reject' so a parsing failure never silently drops a good idea.

    Uses the MINIMUM price found because AOV is what customers actually pay most
    often — if a bundle costs ₹1499 but the single SKU is ₹450, the effective AOV
    is closer to ₹450 and the idea should be rejected.
    """
    prices: list[int] = []
    for t in texts:
        if not t:
            continue
        # Match 3-5 digit numbers (typical rupee prices ₹100 – ₹99999),
        # optionally preceded by ₹ / Rs / INR to avoid matching
        # weights, SKU counts, ingredient percentages, etc.
        for m in re.finditer(
            r"(?:₹|\bRs\.?\s*|\bINR\s*)\s*([\d,]{3,7})(?:\s*[-–]\s*([\d,]{3,7}))?",
            t,
            re.IGNORECASE,
        ):
            try:
                prices.append(int(m.group(1).replace(",", "")))
                if m.group(2):
                    prices.append(int(m.group(2).replace(",", "")))
            except ValueError:
                continue
    return min(prices) if prices else None

SYSTEM_PROMPT = """You are a D2C product idea evaluator for an Indian founder.
Score each idea on 6 dimensions. Return structured JSON only.

The user_prompt below contains FOUNDER CONTEXT specific to this founder
(their capital ceiling, their distribution profile, their hard excludes).
Use that context — do not assume defaults.

GENERAL CONSTRAINTS (apply to all founders):
- Target: tier-1 urban India (Mumbai, Delhi, Bangalore, Hyderabad, Pune, Chennai)
- AOV sweet spot: ≥₹800 for D2C, ≥₹300 for marketplace-first
- Gross margin floor: ≥60%
- Weight: <3kg ideal, <10kg acceptable (marketplace-first), >10kg = reject
- Must be launchable with contract manufacturing / white-label (no own factory)
- Must be launchable with 1-3 hero SKUs (no deep SKU matrices)

SCORING RUBRIC:

score_demand (1-10): Signal loudness — how much evidence exists for this pain/demand?
  10 = hundreds of reviews/posts mentioning this exact pain, multiple sources corroborate
  7-9 = 50-100 mentions across Amazon reviews + Reddit + YouTube
  5-6 = 20-50 mentions, mostly from one source
  3-4 = scattered mentions, weak evidence
  1-2 = single mention or founder speculation

score_launchability (1-10): Supply chain + regulation + taste fit for India
  10 = abundant contract manufacturers in India, no special regulation, obvious India fit
  7-9 = contract mfg available, light regulation (basic FSSAI, cosmetics license), clear demand
  5-6 = some supply chain complexity, moderate regulation, India taste fit uncertain
  3-4 = limited manufacturers, significant regulation, questionable India fit
  1-2 = needs own factory, heavy regulation (AYUSH, pharma-grade), no India precedent

score_capital_fit (1-10): Can launch under the founder's capital ceiling (see FOUNDER CONTEXT)?
The bands below calibrate the 1-10 scale to typical D2C launch costs in India.
Use the founder's actual ceiling (from FOUNDER CONTEXT) to anchor where they sit:
  10 = ₹3-5L to start (white-label, tiny MOQ 100-500 units, basic packaging)
  8-9 = ₹5-10L (contract mfg with 1000-unit MOQ, custom packaging, initial marketing)
  6-7 = ₹10-15L (higher MOQ, custom formulation, brand development)
  4-5 = ₹15-25L (pushes most bootstrap ceilings, needs significant inventory)
  1-3 = ₹25L+ (machinery, deep inventory, multiple SKUs required from day 1)

score_arbitrage (0-10): Geographic arbitrage confidence (0 if not a geo-arbitrage idea)
  10 = proven US/JP/EU brand with strong growth, zero India presence, no regulatory barrier
  7-9 = growing abroad, weak or no India equivalent, some adaptation needed
  4-6 = exists abroad but India adaptation is non-trivial (taste, format, regulation)
  1-3 = tenuous arbitrage read — similar products exist in India or gap is small
  0 = not a geo-arbitrage idea (Lens A only)

score_competition (1-10): INVERTED — higher = LESS competition (good)
  10 = no real competitor in India, true white space
  7-9 = 1-2 small players, no well-funded incumbent, weak existing options
  5-6 = established players but fragmented, room for differentiated entry
  3-4 = multiple strong players (well-funded D2C incumbents in this category), need strong wedge
  1-2 = dominated by well-funded incumbents (HUL, P&G, Dabur) or saturated D2C space

score_distribution (1-10): How much of the IDEAL GTM can THIS founder execute today?

  The idea's `gtm_tactics` field is the UNBIASED ideal playbook for this product —
  what would actually make it succeed regardless of who's launching it.

  Read it and ask: "what fraction of these tactics can this founder run with
  their current distribution channels (see FOUNDER CONTEXT above)?"

  10 = every channel in the ideal playbook is available to the founder
  7-9 = most channels are available; one or two secondary channels missing
  5-6 = half the ideal needs channels the founder doesn't have
  3-4 = most of the ideal needs channels the founder doesn't have
  1-2 = ideal requires channels the founder has explicitly scored 0 on

  Important: do NOT score based on whether gtm_tactics merely mentions channels
  from the founder's profile. That alone is meaningless — the idea agent could
  always do that. Score based on the REAL OVERLAP between what the product
  needs and what the founder can deliver.

Also assess:
- eval_flags: array of strings — specific issues like "cold chain risk", "FSSAI nutraceutical complex",
  "high competition from funded incumbents", "capital tight for founder's ceiling", "no clear wedge", "AOV too low for D2C",
  "product >5kg shipping cost concern", "taste/format may not translate to India"

Return ONLY a JSON array:
[{
  "idea_id": "<id>",
  "score_demand": <1-10>,
  "score_launchability": <1-10>,
  "score_capital_fit": <1-10>,
  "score_arbitrage": <0-10>,
  "score_competition": <1-10>,
  "score_distribution": <1-10>,
  "eval_flags": ["flag1", "flag2"],
}]"""


def evaluate_ideas(ideas: list[IdeaDict], profile: dict) -> list[IdeaDict]:
    """Score, filter, and return ideas that pass the evaluation threshold."""
    if not ideas:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = EVALUATION_MODEL

    # Internal pipeline tuning lives in utils/config.py — NOT profile.yaml.
    # profile.yaml is for user-editable preferences only (capital, categories,
    # channels, excludes). See utils/config.py for the rationale.
    from utils.config import (
        SCORING_WEIGHTS as weights,
        FILTERS,
        MIN_AOV_RUPEES as min_aov_rupees,
        THEMATIC_BONUSES as bonuses,
    )
    min_composite = FILTERS["min_composite_score"]
    min_launchability = FILTERS["min_launchability"]
    min_capital_fit = FILTERS["min_capital_fit"]
    max_competition_density = FILTERS["max_competition_density"]

    # Capital ceiling stays in profile.yaml — it's a USER setting.
    capital_ceiling = profile.get("market", {}).get("capital_ceiling_lakhs")

    # score_competition is inverted (10 = no competition = good)
    # max_competition_density = 7 means reject if RAW density > 7,
    # which in inverted terms means reject if score_competition < 3
    min_competition_score = 10 - max_competition_density  # = 3
    # score_competition is inverted (10 = no competition = good)
    # max_competition_density = 7 means reject if RAW density > 7,
    # which in inverted terms means reject if score_competition < 3
    min_competition_score = 10 - max_competition_density  # = 3

    ideas_text = json.dumps([
        {
            "idea_id": idea.idea_id,
            "title": idea.title,
            "tagline": idea.tagline,
            "category": idea.category,
            "problem": idea.problem,
            "target_consumer": idea.target_consumer,
            "hero_product": idea.hero_product,
            "aov_estimate": idea.aov_estimate,
            "margin_estimate": idea.margin_estimate,
            "capital_required_estimate": idea.capital_required_estimate,
            "sourcing_approach": idea.sourcing_approach,
            "gtm_tactics": idea.gtm_tactics,
            "distribution_channels": idea.distribution_channels,
            "ai_angle": idea.ai_angle,
            "word_of_mouth_potential": idea.word_of_mouth_potential,
            "wedge": idea.wedge,
            "competitors_india": idea.competitors_india,
            "opportunity_type": idea.opportunity_type,
        }
        for idea in ideas
    ], indent=2)

    founder_context = _build_founder_context(profile)

    user_prompt = f"""{founder_context}

Score these {len(ideas)} D2C product ideas for THIS founder (use the FOUNDER CONTEXT above).

Return ONLY a JSON array (one object per idea) with the scoring fields specified in the rubric.

Ideas to score:
{ideas_text}"""

    # Single-pass Haiku call — no tool use
    response = client.messages.create(
        model=model,
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    scores = _parse_scores(text)

    # Apply scores to IdeaDict objects and filter
    scored_map = {s["idea_id"]: s for s in scores}
    results = []

    # Hard category gate — even if idea_agent generates an off-category idea,
    # eval rejects it definitively. Belt-and-suspenders for the category bug
    # that surfaced in May 2026 (idea_agent prompt was silently missing the
    # category restriction due to a stale schema lookup).
    _cats = profile.get("categories", {})
    picked_cats_set = {
        k for k, v in _cats.items()
        if isinstance(v, dict) and v.get("weight", 0) > 0
    }

    for idea in ideas:
        # Hard category check — runs before any scoring
        if idea.category not in picked_cats_set:
            idea.eval_status = "rejected"
            idea.eval_reasons = (
                f"Category '{idea.category}' is not in your picked categories "
                f"({', '.join(sorted(picked_cats_set))}). This idea was generated "
                f"off-spec — it should not have been produced. Skipping."
            )
            # Zero out scores so downstream sort doesn't surface it
            idea.score_demand = 0
            idea.score_launchability = 0
            idea.score_capital_fit = 0
            idea.score_arbitrage = 0
            idea.score_competition = 0
            idea.score_distribution = 0
            idea.composite_score = 0.0
            results.append(idea)
            continue

        s = scored_map.get(idea.idea_id, {})
        if not s:
            continue

        idea.score_demand = s.get("score_demand", 0)
        idea.score_launchability = s.get("score_launchability", 0)
        idea.score_capital_fit = s.get("score_capital_fit", 0)
        idea.score_arbitrage = s.get("score_arbitrage", 0)
        idea.score_competition = s.get("score_competition", 0)
        idea.score_distribution = s.get("score_distribution", 0)
        idea.eval_flags = ", ".join(s.get("eval_flags", []))

        # Composite score calculation
        raw_composite = (
            idea.score_demand * weights.get("demand_strength", 0.25) +
            idea.score_launchability * weights.get("india_launchability", 0.20) +
            idea.score_capital_fit * weights.get("capital_fit", 0.20) +
            idea.score_competition * weights.get("competition", 0.15) +
            idea.score_distribution * weights.get("distribution_fit", 0.10) +
            idea.score_arbitrage * weights.get("arbitrage_confidence", 0.10)
        )

        # Apply thematic bonus multiplier
        bonus_multiplier = 1.0
        ai = idea.ai_angle.lower() if idea.ai_angle else ""
        if ai and ai != "none":
            bonus_multiplier += bonuses.get("ai_formulated", 0.10)

        brand = idea.brand_angle.lower() if idea.brand_angle else ""
        if "honest" in brand or "transparent" in brand:
            bonus_multiplier += bonuses.get("honest_transparent", 0.08)

        wom = idea.word_of_mouth_potential
        try:
            wom_score = int(wom) if wom else 0
        except (ValueError, TypeError):
            wom_score = 0
        if wom_score >= 7:
            bonus_multiplier += bonuses.get("instagrammable_visual", 0.10)

        raw_composite *= bonus_multiplier
        idea.score_composite = round(raw_composite, 2)

        # Tag each idea with eval_status + eval_reasons against the hard
        # floors. We DON'T drop failing ideas anymore — the user wants to see
        # all generated ideas regardless, and render_markdown shows badges +
        # reasons so they can judge for themselves whether a "rejected" idea
        # is still worth pursuing with adjusted constraints.
        reasons: list[str] = []

        # Reasons are plain-language, not raw score thresholds. Score is shown
        # parenthetically for those who want it, but the headline is "what's
        # actually wrong" in human terms.
        min_price = _extract_min_price_inr(idea.aov_estimate, idea.hero_product)
        if min_price is not None and min_price < min_aov_rupees:
            reasons.append(
                f"Price point too low (₹{min_price}) — below ₹{min_aov_rupees} D2C economics floor"
            )

        if idea.score_launchability < min_launchability:
            reasons.append(
                f"Launchability concerns — supply chain, regulation, or India fit "
                f"(fit score {idea.score_launchability}/10)"
            )
        # Capital flagging: prefer the ACTUAL parsed capital estimate over the
        # subjective Haiku score. Only flag if the estimate genuinely exceeds
        # the user's ceiling. Subjective "tightness" (high end close to ceiling
        # but under it) doesn't justify a hard flag — it's already reflected in
        # the composite score via score_capital_fit.
        capital_high = _extract_capital_high_lakhs(idea.capital_required_estimate)
        if capital_high is not None and capital_ceiling and capital_high > capital_ceiling:
            reasons.append(
                f"Needs more capital than your ceiling allows "
                f"(estimate ₹{capital_high:g}L vs your ₹{capital_ceiling}L ceiling)"
            )
        elif capital_high is None and idea.score_capital_fit < min_capital_fit:
            # Fallback: if we couldn't parse the estimate (unusual format),
            # rely on Haiku's subjective score as a last-resort signal.
            reasons.append(
                f"Capital fit unclear — estimate format unparseable "
                f"(Haiku score {idea.score_capital_fit}/10 < {min_capital_fit}/10 threshold)"
            )
        if idea.score_competition < min_competition_score:
            reasons.append(
                f"Category too crowded — well-funded incumbents already dominate "
                f"(score {idea.score_competition}/10)"
            )
        if idea.score_composite < min_composite:
            reasons.append(
                f"Overall score below minimum (composite {idea.score_composite}/10)"
            )

        if reasons:
            idea.eval_status = "rejected"
            idea.eval_reasons = "; ".join(reasons)
            logger.info(f"[evaluation] REJECT '{idea.title}' — {idea.eval_reasons}")
        else:
            idea.eval_status = "passed"
            idea.eval_reasons = ""

        results.append(idea)  # ALWAYS append — let renderer decide presentation

    passed = sum(1 for i in results if i.eval_status == "passed")
    logger.info(f"[evaluation] {passed}/{len(results)} passed filters; {len(results) - passed} flagged as rejected")
    return results


def _parse_scores(text: str) -> list[dict]:
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    try:
        return json.loads(text.strip())
    except Exception as e:
        logger.warning(f"[evaluation] failed to parse scores: {e}")
        return []
