"""
IdeaAgent — generates D2C product idea cards from normalized signals.
Uses Claude Sonnet with tool use. Self-evaluates each idea before emitting.
"""
import json
import os
import uuid
import hashlib
import time
from datetime import datetime
import anthropic
from utils.signal_schema import NormalizedSignal, IdeaDict
from utils.tools import TOOL_DEFINITIONS, dispatch_tool
from utils.logger import get_logger
from utils.models import GENERATION_MODEL

logger = get_logger()


def _build_system_prompt(profile: dict) -> str:
    # --- Read user profile (use ACTUAL profile.yaml schema) ---
    cats_dict = profile.get("categories", {})
    picked_categories = [
        k for k, v in cats_dict.items()
        if isinstance(v, dict) and v.get("weight", 0) > 0
    ]

    dist_channels = profile.get("distribution", {})  # correct key is "distribution"

    market = profile.get("market", {})
    capital_lakhs = market.get("capital_ceiling_lakhs", 20)

    # Hard AOV floor — internal config (utils/config.py), not in profile.yaml.
    # This is a D2C unit-economics floor, not a per-user setting.
    from utils.config import MIN_AOV_RUPEES
    min_aov = MIN_AOV_RUPEES

    # User-specific hard excludes. These come from onboarding (Q3) — the user
    # picked which categorical risks they want ruled out. We DO NOT impose any
    # additional always-on excludes (cold chain, complex formulation, etc.)
    # beyond what the user explicitly said. If the user can handle cold chain,
    # they shouldn't be blocked from cold-chain ideas because the system
    # assumed they couldn't.
    EXCLUDE_DESCRIPTIONS = {
        "cold_chain": "Cold chain or perishable logistics (anything that needs refrigerated transport/storage)",
        "complex_formulation": "Complex or scientific formulation (novel actives, R&D-heavy, requires specialist chemistry)",
        "tight_regulation": "Tight regulation (FSSAI nutraceutical cat 8, AYUSH, baby formula, medical devices)",
        "over_10kg": "Heavy items over 10kg — shipping costs become meaningful (5-10% of AOV), returns get expensive, damage rates rise, COD risk goes up. Couriers can still handle these but unit economics get tight.",
        "ingestible": "Anything ingestible (food, supplements, drinks — anything consumed)",
    }
    excludes = profile.get("hard_excludes", {})
    preset_excludes = excludes.get("preset", []) if isinstance(excludes, dict) else []
    custom_excludes = excludes.get("custom", []) if isinstance(excludes, dict) else []

    # Expand preset keys to descriptions; pass custom strings through as-is.
    user_exclude_lines = []
    for e in preset_excludes:
        user_exclude_lines.append(EXCLUDE_DESCRIPTIONS.get(e, e))
    for e in custom_excludes:
        user_exclude_lines.append(e)

    # --- Format strings for the prompt ---
    cat_str = "\n".join(f"  - {c}" for c in picked_categories) or "  (none — this is a bug)"
    dist_str = "\n".join(f"  {ch}: {score}" for ch, score in sorted(dist_channels.items())) or "  (none specified)"
    user_excludes_str = (
        "\n".join(f"- {line}" for line in user_exclude_lines)
        if user_exclude_lines else "(none — the user did not pick any categorical excludes in onboarding)"
    )

    return f"""You are a D2C product idea generator for a specific founder launching physical consumer products in India.

FOUNDER PROFILE:
- Bootstrapping in India, capital ceiling: ₹{capital_lakhs}L
- Target: tier-1 urban consumers (metro cities — Mumbai, Delhi, Bangalore, Hyderabad, Pune, Chennai)
- AOV HARD FLOOR: ≥₹{min_aov}. This is NOT a soft target — D2C unit economics break below it (CAC + shipping + returns + payment fees eat the margin). Any idea whose hero_product price or aov_estimate is below ₹{min_aov} will be rejected. If a category only supports lower price points, either BUNDLE (multipack, starter kit, subscription) so the AOV crosses ₹{min_aov}, or drop the idea.
- Gross margin minimum: ≥60%
- Weight limit: <10kg (sweet spot: <3kg)

CATEGORIES — these are the ONLY categories you may generate ideas in:
{cat_str}

**HARD RULE — CATEGORY:** Every idea you emit MUST have `category` set to one of the values above (exactly as spelled). Do not generate ideas in any other category, even if a signal looks interesting. If a signal is in an unpicked category, drop it — do not stretch it to fit a picked category, and do not emit it as an "interesting wildcard." The user explicitly said these are their categories; respect that.

DISTRIBUTION CHANNEL SCORES (0=can't, 2=comfortable, 3=superpower):
{dist_str}

HARD EXCLUDES — reject any idea requiring:
- Hero SKU priced below ₹{min_aov} (bundle up or drop — no exceptions)
- Capital >₹{capital_lakhs}L to launch first batch (this is the user's hard ceiling)

USER'S CATEGORICAL EXCLUDES (from onboarding):
{user_excludes_str}

Reject any idea that triggers any of the above. Do NOT impose additional excludes that the user didn't pick — if the user is comfortable with (say) cold chain or regulation-heavy categories, you should be too.

SELF-EVALUATION REQUIREMENT:
Before emitting each idea, internally ask:
1. Is the category one of the picked categories listed above? If not, DROP. (This is the first check — do it before anything else.)
2. Is the hero SKU priced ≥₹{min_aov}? If not, BUNDLE or DROP.
3. Would this founder actually launch this with ₹{capital_lakhs}L?
4. Can this achieve ≥60% gross margin?
5. Is the product <3kg (or <10kg with marketplace-first)?
6. Is there a clear wedge vs existing India players?
7. Is the GTM the IDEAL playbook for this product — what would actually make it succeed in-market, regardless of the founder's current distribution profile? (Channel-match is scored separately as distribution_fit — do NOT artificially constrain the GTM to channels the founder already has.)
If an idea fails these checks — improve it or drop it. Only emit ideas you'd confidently recommend.

WILDCARD IDEAS (within picked categories): You may tag 1-2 ideas with `opportunity_type: "wildcard"` if they take an unexpected angle within one of the picked categories — different consumer moment, surprising format, contrarian positioning. Wildcards still MUST be in a picked category. Do not use "wildcard" as a license to generate off-category ideas.

OUTPUT FORMAT:
Return a JSON array. Each idea MUST have ALL these fields:
{{
  "category": "one of the 16 categories listed above",
  "title": "Short product name (3-5 words)",
  "tagline": "SPECIFIC one-sentence pitch with price + persona. The price MUST be ≥₹{min_aov} — for categories that typically sell below this, lead with a bundle or kit AOV (e.g. '<format/ingredient> <product type> + <complementary SKU>, ₹<price>, for <specific consumer segment>'). NOT a vague phrase like 'a shampoo product'.",
  "problem": "3-5 sentences. Do NOT just restate the category. Cover: (1) WHO exactly feels this and in what moment ('<demographic> doing <specific action> in <specific context>'), (2) HOW OFTEN the pain hits and how acute it is (daily irritation vs occasional annoyance), (3) WHAT THEY DO TODAY — the actual workarounds (which products they stack, home remedies, what they complain about in reviews), (4) WHY existing options fall short specifically (too harsh, too expensive, wrong format, missing ingredient, bad smell, etc.). Ground this in the signals — quote specific review/complaint language where possible.",
  "target_consumer": "Detailed persona (age, city tier, lifestyle) — e.g. '<age range> in <tier-1 city or tier> with <specific situation>'",
  "market_size_estimate": "e.g. '₹<X>Cr Indian <category> market, <subsegment> <Y>% penetration'",
  "why_now": "Timing: ingredient trending, format shift, arbitrage window, regulation change",
  "hero_product": "Specific launch SKU: format, key ingredients/materials, size/weight, price point. Price MUST be ≥₹{min_aov}; if the base SKU is cheaper, define the launch SKU as a bundle or kit that clears ₹{min_aov} — e.g. '<format> + <complementary SKU>, <size/quantity>, ₹<price>'",
  "hero_product_detail": "6-9 sentences. Make the founder SEE, TOUCH, and USE the product. Cover in this order: (1) PHYSICAL FORM — what it looks like on the shelf and in hand (colour, texture, smell, weight, packaging material and finish — e.g. 'amber glass bottle with matte black dropper', not just 'glass bottle'); (2) USE RITUAL — how the consumer actually uses it, step by step, what sensation they get (foam, warmth, tingle, scent lingering), how long it takes; (3) COMPOSITION — key ingredients/materials and WHY each is there (not just a list); (4) VARIANTS / BUNDLE — 1-2 planned SKUs or a starter bundle; (5) UNIT ECONOMICS — COGS per unit, MRP, margin %; (6) EXPLICIT INDIA CONTRAST — name 2-3 existing Indian products the consumer buys today (e.g. 'vs <India brand A> <SKU> ₹<price> / <India brand B> <SKU> ₹<price>') and state precisely what they fail at that this SKU fixes — format, ingredient, price-tier, experience, or packaging. Avoid generic phrases like 'better quality' or 'premium feel' — be concrete.",
  "aov_estimate": "e.g. '₹<price>'",
  "margin_estimate": "e.g. '<X>-<Y>%'",
  "capital_required_estimate": "Realistic estimate under ₹{capital_lakhs}L — e.g. '₹<X>-<Y>L (₹<A>L first batch MOQ, ₹<B>L packaging, ₹<C>L initial marketing)'",
  "first_year_revenue_estimate": "Realistic Y1 revenue with assumptions — e.g. '₹<X>-<Y>L (<N> units/mo avg × ₹<AOV> × 12mo, ramp from <small>→<large> units/mo, <Z>% repeat rate). Conservative: ₹<lower>L if acquisition is slower.' Always state unit volume, AOV, and ramp assumptions.",
  "sourcing_approach": "Contract mfg / white-label / import + repack. Include estimated MOQ and first-batch cost",
  "gtm_tactics": "The IDEAL launch playbook for this product — what would actually make it succeed, REGARDLESS of which channels the founder currently has. Be honest: if this product naturally wants celebrity influencer + paid ads at scale + offline retail, say so. 3-4 specific tactics naming concrete channels.",
  "brand_angle": "Primary positioning stance (pick ONE). Options: 'honest' (transparency-led — ingredient/sourcing/pricing openness as the hook), 'premium' (design-led, materials, status), 'playful' (irreverent, fun, Gen Z tone), 'scientific' (clinically-backed, R&D, claims-led), 'heritage' (Indian craft, traditional formulations, story-led), 'indulgent' (sensorial, treat-yourself, mood), 'community' (tribe/identity-led), 'functional' (utility-first, problem-solver, no-frills). Pick the angle that GENUINELY describes the brand's lead pitch — not the safest-sounding label. 'Honest' is overused as a default; only pick it when transparency is the actual differentiator (e.g. radically published ingredient costs), not just a tone.",
  "distribution_channels": "Which channels from the profile fit this idea",
  "ai_angle": "'AI-formulated skincare using skin analysis' or 'none'",
  "word_of_mouth_potential": "1-10 how instagrammable/shareable is this product",
  "idea_rationale": "3-4 sentences: (1) what signals triggered this, (2) the gap/opportunity, (3) why this founder fits, (4) key assumption to validate",
  "competitors_india": "",
  "reference_brands_global": "US/Japan/EU brands this is inspired by, if any",
  "wedge": "1-2 sentences naming 2-3 specific India incumbents (brand + SKU + price) and the ONE dimension on which this product beats them (format, ingredient, price-tier, ritual, or packaging). No generic 'better quality' claims.",
  "lenses_fired": "comma-separated: complaint_cluster, geo_arbitrage, format_shift, etc.",
  "contributing_signal_ids": ["signal_id_1", "signal_id_2"],
  "source_signal": "primary collector (e.g. 'amazon_us', 'reddit_us', 'rising_brands')",
  "source_urls": ["url1", "url2", "url3"],
  "comparable_products_global": ["Product/brand name from US/EU/Japan/Korea that does something similar — include the URL if available from the signal"],
  "opportunity_type": "geo_arbitrage | complaint_cluster | format_shift | unbranded_market | rising_brand_gap | wildcard"
}}

IMPORTANT — source honesty rules:
1. source_urls: Include ALL URLs from the signals that contributed to this idea. If 3 signals from different sources inspired the idea, include all 3 URLs. Never fabricate URLs.
2. comparable_products_global: Include product names/URLs of reference products from US/EU/Japan/Korea that this idea is inspired by or similar to. These help the founder visualize what "good" looks like abroad. Only include if genuinely relevant.
3. idea_rationale: If a category or angle came from the founder profile rather than the signal, say so explicitly."""


def _make_idea_hash(category: str, hero_product: str) -> str:
    """Hash category + hero concept for dedup."""
    import re
    stop_words = {"a", "an", "the", "for", "of", "in", "on", "at", "to", "with", "and", "or", "is", "it"}
    text = f"{category} {hero_product}".lower()
    words = re.sub(r"[^a-z0-9 ]", "", text).split()
    words = sorted(w for w in words if w not in stop_words)
    return hashlib.sha256(" ".join(words).encode()).hexdigest()[:16]


def generate_ideas(
    signals: list[NormalizedSignal],
    profile: dict,
    recent_titles: list[str] = None,
    max_tool_rounds: int = 4,
) -> list[IdeaDict]:
    """Generate D2C product ideas from normalized signals."""
    if not signals:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = GENERATION_MODEL

    system_prompt = _build_system_prompt(profile)
    _emit_progress(f"idea generation: synthesizing from {len(signals)} signals (the slowest single step, typically 5-9 min — Sonnet does multiple web_search tool calls + one large JSON output)")

    # Format signals for the prompt
    signals_text = json.dumps([
        {
            "signal": f"{s.summary[:100]} [{s.raw_source}]",
            "type": s.signal_type,
            "category_tags": s.category_tags,
            "consumer_tags": s.consumer_tags,
            "evidence_strength": s.evidence_strength,
            "key_entities": s.key_entities,
            "pain_acuity": s.pain_acuity,
            "gap_dimensions": s.gap_dimensions,
            "geo_markets": s.geo_markets,
            "enrichment_notes": s.enrichment_notes,
            "raw_url": s.raw_url,
        }
        for s in signals
    ], indent=2)

    existing_str = ""
    if recent_titles:
        existing_str = (
            "\n\nALREADY EXPLORED — do NOT generate ideas that are the same concept as these, "
            "even if you rename the brand or rephrase the product. For example, if 'protein chickpea chips' "
            "is listed, do NOT generate 'masala roasted chickpea snacks' — same core product.\n"
            + "\n".join(f"- {t}" for t in recent_titles[:80])
        )

    from utils.config import MAX_IDEAS_PER_RUN
    max_ideas = MAX_IDEAS_PER_RUN

    user_prompt = f"""Here are {len(signals)} validated D2C consumer signals. Generate {max_ideas} product ideas.

Target: {max_ideas} ideas total, including 1-2 wildcards.
0-2 ideas from power consumer categories (kombucha, apparel, shoes, protein, sauces/condiments, fitness).
{existing_str}

DIVERSITY ACROSS THE 5 IDEAS — to avoid 5 ideas reading like variations of the same template:
- Use at least 3 DIFFERENT values for `opportunity_type` across the 5 ideas. Do not repeat any opportunity_type more than 2 times. Pick the one that genuinely fits each idea — don't stretch a signal to fit a lens it doesn't support.
- Use at least 3 DIFFERENT values for `brand_angle` across the 5 ideas. Do not repeat any brand_angle more than 2 times. 'honest' especially is overused — only use it when transparency is the actual differentiator, not as a default.

Remember your self-evaluation step: only emit ideas you'd confidently recommend to this founder.
TOOL USE LIMIT: Use at most 3 searches total, only for sourcing/pricing checks you are genuinely uncertain about.
Output JSON immediately after.

Signals:
{signals_text}

Return ONLY a JSON array starting with ```json"""

    messages = [{"role": "user", "content": user_prompt}]
    text = ""

    for _ in range(max_tool_rounds):
        for attempt in range(3):
            try:
                # Streaming is required by the Anthropic API for requests whose
                # predicted output exceeds 10 minutes (max_tokens=24000 triggers this).
                # get_final_message() returns a Message with the same shape that
                # messages.create() would have returned, so downstream code is unchanged.
                with client.messages.stream(
                    model=model,
                    max_tokens=24000,
                    system=system_prompt,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                ) as stream:
                    response = stream.get_final_message()
                break
            except Exception as e:
                if "overloaded" in str(e).lower() and attempt < 2:
                    wait = 30 * (attempt + 1)
                    logger.warning(f"[idea_agent] API overloaded — retrying in {wait}s (attempt {attempt + 1}/3)")
                    time.sleep(wait)
                else:
                    raise

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if tool_uses:
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tu in tool_uses:
                logger.info(f"[idea_agent] tool: {tu.name}")
                result = dispatch_tool(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result[:3000],
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        break
    else:
        logger.warning("[idea_agent] max tool rounds reached — forcing final output")
        messages.append({"role": "user", "content": (
            "You have reached the tool use limit. "
            "Output your final JSON array of D2C product ideas now, based on what you have. "
            "Start with ```json."
        )})
        with client.messages.stream(
            model=model,
            max_tokens=24000,
            system=system_prompt,
            messages=messages,
        ) as stream:
            final = stream.get_final_message()
        text = "".join(b.text for b in final.content if hasattr(b, "text"))

    return _parse_ideas(text)


def _parse_ideas(text: str) -> list[IdeaDict]:
    """Parse JSON output into IdeaDict objects."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]

    try:
        data = json.loads(text.strip())
    except Exception as e:
        logger.warning(f"[idea_agent] failed to parse JSON: {e} — attempting truncation recovery")
        raw = text.strip()
        last_complete = raw.rfind("\n  },")
        if last_complete == -1:
            last_complete = raw.rfind("},")
        if last_complete > 0:
            truncated_fixed = raw[:last_complete + 4] + "\n]"
            if not truncated_fixed.lstrip().startswith("["):
                truncated_fixed = "[" + truncated_fixed.lstrip()
            try:
                data = json.loads(truncated_fixed)
                logger.info(f"[idea_agent] truncation recovery succeeded — {len(data)} ideas recovered")
            except Exception:
                logger.warning("[idea_agent] truncation recovery failed — returning empty")
                return []
        else:
            return []

    results = []
    run_date = datetime.utcnow().strftime("%Y-%m-%d")

    def _str_field(item: dict, key: str, default: str = "") -> str:
        """Coerce a JSON field to a string. Sonnet sometimes returns string
        fields as lists or numbers — this normalises them so downstream code
        can trust the type. Lists get joined with ", ".
        """
        value = item.get(key, default)
        if value is None:
            return default
        if isinstance(value, list):
            return ", ".join(str(v).strip() for v in value if v)
        if not isinstance(value, str):
            return str(value)
        return value

    for item in data:
        if not isinstance(item, dict) or not item.get("title"):
            continue

        title = item["title"]
        category = item.get("category", "")
        hero_product = item.get("hero_product", "")
        idea_hash = _make_idea_hash(category, hero_product or title)

        idea = IdeaDict(
            idea_id=str(uuid.uuid4()),
            run_date=run_date,
            category=category,
            title=title,
            tagline=item.get("tagline", ""),
            problem=item.get("problem", ""),
            target_consumer=_str_field(item, "target_consumer"),
            market_size_estimate=_str_field(item, "market_size_estimate"),
            why_now=_str_field(item, "why_now"),
            hero_product=hero_product,
            hero_product_detail=_str_field(item, "hero_product_detail"),
            aov_estimate=_str_field(item, "aov_estimate"),
            margin_estimate=_str_field(item, "margin_estimate"),
            capital_required_estimate=_str_field(item, "capital_required_estimate"),
            first_year_revenue_estimate=_str_field(item, "first_year_revenue_estimate"),
            sourcing_approach=_str_field(item, "sourcing_approach"),
            gtm_tactics=_str_field(item, "gtm_tactics"),
            brand_angle=_str_field(item, "brand_angle"),
            distribution_channels=_str_field(item, "distribution_channels"),
            ai_angle=_str_field(item, "ai_angle", default="none"),
            word_of_mouth_potential=_str_field(item, "word_of_mouth_potential"),
            idea_rationale=_str_field(item, "idea_rationale"),
            competitors_india="",  # filled by competitor enrichment later
            reference_brands_global=_str_field(item, "reference_brands_global"),
            wedge=_str_field(item, "wedge"),
            lenses_fired=_str_field(item, "lenses_fired"),
            contributing_signals=", ".join(item.get("contributing_signal_ids", [])),
            source_signal=item.get("source_signal", ""),
            source_url="\n".join(item.get("source_urls", [])[:6]),
            opportunity_type=item.get("opportunity_type", "complaint_cluster"),
            comparable_product_images="",  # filled by competitor enrichment
            time_sensitive=False,
            idea_hash=idea_hash,
        )
        results.append(idea)

    logger.info(f"[idea_agent] generated {len(results)} ideas")
    _emit_progress(f"idea generation: produced {len(results)} candidate ideas")
    return results


def _emit_progress(message: str) -> None:
    """Print a 4-space-indented progress line to stdout AND append to
    user_data/progress.txt so the chat's Monitor can relay it."""
    from pathlib import Path as _Path
    indented = f"    {message}"
    print(indented, flush=True)
    try:
        progress_path = _Path(__file__).resolve().parent.parent.parent / "user_data" / "progress.txt"
        progress_path.write_text(indented + "\n")
    except Exception:
        pass
