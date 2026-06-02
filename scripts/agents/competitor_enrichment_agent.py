"""
CompetitorEnrichmentAgent — 8-search D2C competitor discovery for India.

Three-phase approach:
  Phase 1: Haiku extracts category-level search queries from each idea
  Phase 2: 8 searches per idea:
    1. Amazon.in — top sellers in category
    2. Flipkart — top sellers
    3. Specialist marketplaces (Nykaa/Myntra/Tira/Purplle, Meesho, 1mg — category-dependent)
    4. Quick commerce (Blinkit, Zepto, BigBasket)
    5. Google India real-user queries
    6. Instagram brand discovery
    7a. Gemini grounded search (Google-native India index)
    7b. ChatGPT web search (Bing-backed, different index)
  Phase 3: Haiku synthesizes all findings into competitors_india + reference_brands_global
"""
import json
import os
import time
import anthropic

from utils.signal_schema import IdeaDict
from utils.tools import web_search
from utils.logger import get_logger
from utils.models import COMPETITOR_QUERY_MODEL, COMPETITOR_SYNTHESIS_MODEL

logger = get_logger()

# Category → specialist marketplace mapping. Keys MUST match canonical
# categories in scripts/setup.py CATEGORIES.
SPECIALIST_MARKETPLACES = {
    "beauty": ["nykaa.com", "tira.com", "purplle.com"],
    "personal_care": ["nykaa.com", "purplle.com"],
    "food_cpg": ["bigbasket.com", "swiggy.com/instamart"],
    "home": ["pepperfry.com", "urbanladder.com"],
    "pet": ["supertails.com", "headsupfortails.com"],
    "wellness": ["nykaa.com", "1mg.com", "healthkart.com"],
    "sexual_wellness": ["mymuse.in", "bold.care", "thatsapersonal.com"],
    "innerwear_loungewear": ["myntra.com", "ajio.com", "nykaafashion.com"],
    "fashion_apparel": ["myntra.com", "ajio.com", "nykaafashion.com"],
    "footwear": ["myntra.com", "ajio.com", "amazon.in"],
    "jewellery_watches": ["myntra.com", "tata-cliq.com", "amazon.in"],
    "eyewear": ["lenskart.com", "amazon.in", "myntra.com"],
    "accessories": ["myntra.com", "ajio.com"],
    "consumer_electronics": ["amazon.in", "flipkart.com", "croma.com"],
    "baby_kids": ["firstcry.com", "hopscotch.com"],
    "fitness_nutrition": ["decathlon.in", "healthkart.com", "amazon.in"],
}


def _generate_search_queries(ideas: list[IdeaDict], profile: dict) -> dict[str, dict]:
    """Use Haiku to extract category-level search terms from each idea.

    Returns {idea_id: {"category": str, "hero_format": str, "sub_category": str,
                        "india_keyword_variants": list[str]}}
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = COMPETITOR_QUERY_MODEL

    payload = [
        {
            "idea_id": idea.idea_id,
            "title": idea.title,
            "category": idea.category,
            "hero_product": idea.hero_product[:200],
            "problem": idea.problem[:200],
            "competitors_india": idea.competitors_india,
        }
        for idea in ideas
    ]

    prompt = f"""For each D2C product idea, extract search terms for finding competitors in India.

For each idea return:
- "category": the broad product category (e.g. "shampoo bars", "protein snacks", "mosquito traps")
- "hero_format": the specific product format (e.g. "<ingredient/feature> <product type>")
- "sub_category": narrower niche (e.g. "sulfate-free haircare", "clean-label snacks")
- "india_keyword_variants": 2-3 search terms an Indian consumer would type on Amazon/Google
  (e.g. ["<ingredient> <product type> India", "<sub-category> <format> India", "<arbitrage source> <product type> India"])

Think: what would an Indian shopper search on Amazon.in or Google to find this type of product?

Return ONLY a JSON array:
[{{"idea_id": "...", "category": "...", "hero_format": "...", "sub_category": "...", "india_keyword_variants": [...]}}]

Ideas:
{json.dumps(payload, indent=2)}"""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        results = json.loads(text.strip())
        return {r["idea_id"]: r for r in results}

    except Exception as e:
        logger.warning(f"[competitor_enrichment] query generation failed: {e} — using fallback keywords")
        return {}


def _fallback_keywords(idea: IdeaDict) -> str:
    """Simple keyword extraction from title as fallback."""
    stop = {"ai", "the", "a", "an", "for", "of", "in", "on", "to", "and",
            "or", "with", "your", "my", "our", "how", "pro", "plus", "india"}
    words = [w.lower().strip(".,:-") for w in idea.title.split()]
    return " ".join(w for w in words if w not in stop)[:50]


# NOTE: The skill version dropped the optional Gemini grounded search and
# ChatGPT web search that lived here originally. Reasons:
#   1. Gemini grounded search was failing in production with the current
#      google-genai SDK version (Unknown field for FunctionDeclaration:
#      google_search). Removing it eliminates a class of API errors.
#   2. ChatGPT web search added marginal value over the 6 Exa-based searches
#      below, and pulled in the openai SDK as a transitive dep for the skill.
# The 6 Exa-based searches (Amazon.in, Flipkart, specialists, quick commerce,
# Google India, Instagram) cover the same ground and stay within the user's
# Exa free tier.


# Concurrency cap for Exa searches.
# Tuning notes:
#   - Exa free tier behaves OK up to ~5 concurrent. Higher risks 429 errors.
#   - Pay tier could go higher, but 5 already gives ~5x speedup vs sequential.
#   - This is enforced per-idea, since enrich_competitors iterates ideas
#     sequentially. Across all ideas, sustained rate stays under ~5 req/sec.
MAX_CONCURRENT_SEARCHES = 5


def _search_for_idea(idea: IdeaDict, queries: dict) -> dict:
    """Run the per-idea competitor searches in parallel and return raw findings.

    All 6 search categories (amazon_in, flipkart, specialist marketplaces,
    quick commerce, google india, instagram) plus an optional reference-brand
    image search fan out concurrently via a ThreadPoolExecutor capped at
    MAX_CONCURRENT_SEARCHES. Sequential time was ~30s per idea; parallel time
    is ~6-8s per idea.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    query_info = queries.get(idea.idea_id, {})
    category = query_info.get("category", "") or idea.category
    hero_format = query_info.get("hero_format", "") or _fallback_keywords(idea)
    sub_category = query_info.get("sub_category", "") or category
    india_keywords = query_info.get("india_keyword_variants", [])
    primary_keyword = india_keywords[0] if india_keywords else f"{hero_format} India"

    # Build the full list of (tag, query, num_results, days_back) tuples up front.
    # Each tag is a unique key used to route results back to the right finding bucket.
    tasks: list[tuple[str, str, int, int]] = []

    # 1. Amazon.in
    tasks.append(("amazon_in", f"site:amazon.in {primary_keyword}", 8, 365))

    # 2. Flipkart
    tasks.append(("flipkart", f"site:flipkart.com {primary_keyword}", 8, 365))

    # 3. Specialist marketplaces (up to 2 sites for this category)
    specialist_sites = SPECIALIST_MARKETPLACES.get(idea.category, ["amazon.in"])[:2]
    for site in specialist_sites:
        tasks.append((f"specialist::{site}", f"site:{site} {sub_category}", 5, 730))

    # 4. Quick commerce
    qc_query = f"site:blinkit.com OR site:zeptonow.com OR site:bigbasket.com {category}"
    tasks.append(("quick_commerce", qc_query, 8, 730))

    # 5. Google India — up to 3 query variants
    search_variants = [primary_keyword]
    if india_keywords and len(india_keywords) > 1:
        search_variants.append(india_keywords[1])
    search_variants.append(f"best {category} under {idea.aov_estimate or '1000'}")
    for i, variant in enumerate(search_variants[:3]):
        tasks.append((f"google_india::{i}", variant, 5, 365))

    # 6. Instagram
    tasks.append(("instagram", f"site:instagram.com #{category.replace('_', '')}", 8, 365))

    # 7. Reference brand image search — only if we have global references.
    # Defensive: coerce list → string in case the field came through as a list
    # (Sonnet occasionally returns it that way despite the schema asking for
    # comma-separated string; this is also normalised at idea_agent
    # construction time but belt-and-suspenders here).
    ref_brands = idea.reference_brands_global
    if isinstance(ref_brands, list):
        ref_brands = ", ".join(str(b).strip() for b in ref_brands if b)
    if ref_brands and isinstance(ref_brands, str):
        first_brand = ref_brands.split(',')[0].split('(')[0].strip()
        if first_brand:
            ref_query = f"{hero_format} product {first_brand}"
            tasks.append(("reference_global", ref_query, 5, 730))

    # Fan out: run all searches concurrently, capped at MAX_CONCURRENT_SEARCHES.
    raw_results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SEARCHES) as pool:
        future_to_tag = {
            pool.submit(web_search, query=q, num_results=n, days_back=d): tag
            for (tag, q, n, d) in tasks
        }
        for future in as_completed(future_to_tag):
            tag = future_to_tag[future]
            try:
                raw_results[tag] = future.result()
            except Exception as e:
                logger.warning(f"[competitor_enrichment] search '{tag}' failed: {e}")
                raw_results[tag] = []

    # Assemble findings dict from raw results
    findings: dict = {}
    product_images: list[dict] = []

    def _collect_images(results: list[dict], source: str):
        for r in results:
            img = r.get("image", "")
            if img and "error" not in r:
                product_images.append({
                    "url": r.get("url", ""), "image": img,
                    "title": r.get("title", ""), "source": source,
                })

    def _flatten(results: list[dict], snippet_len: int = 120) -> list[str]:
        return [
            f"{r.get('title', '')} — {r.get('snippet', '')[:snippet_len]}"
            for r in results if "error" not in r
        ]

    findings["amazon_in"] = _flatten(raw_results.get("amazon_in", []), 150)
    findings["flipkart"] = _flatten(raw_results.get("flipkart", []), 120)
    findings["quick_commerce"] = _flatten(raw_results.get("quick_commerce", []), 120)
    findings["instagram"] = _flatten(raw_results.get("instagram", []), 120)

    # Specialist: union across all specialist sites
    specialist_combined: list[str] = []
    for tag, results in raw_results.items():
        if tag.startswith("specialist::"):
            site = tag.split("::", 1)[1]
            specialist_combined.extend(
                f"[{site}] {r.get('title', '')} — {r.get('snippet', '')[:120]}"
                for r in results if "error" not in r
            )
    findings["specialist_marketplaces"] = specialist_combined

    # Google India: union across query variants
    google_combined: list[str] = []
    for tag, results in raw_results.items():
        if tag.startswith("google_india::"):
            google_combined.extend(_flatten(results, 150))
    findings["google_india"] = google_combined

    # Image collection — same coverage as sequential version
    _collect_images(raw_results.get("amazon_in", []), "amazon_in")
    _collect_images(raw_results.get("flipkart", []), "flipkart")
    _collect_images(raw_results.get("quick_commerce", []), "quick_commerce")
    _collect_images(raw_results.get("instagram", []), "instagram")
    _collect_images(raw_results.get("reference_global", []), "reference_global")

    # (Gemini grounded search and ChatGPT web search were removed in the skill
    # version — see comment near the top of this file.)

    findings["_product_images"] = product_images[:8]
    return findings


def enrich_competitors(ideas: list[IdeaDict], profile: dict) -> list[IdeaDict]:
    """Run 8-search competitor enrichment on each idea. Updates competitors_india
    and reference_brands_global fields in place."""
    if not ideas:
        return ideas

    logger.info(f"[competitor_enrichment] searching competitors for {len(ideas)} ideas...")
    _emit_progress(f"competitor mapping: setting up searches for {len(ideas)} ideas")

    # Phase 1: Generate category-level search queries via Haiku
    queries = _generate_search_queries(ideas, profile)
    if queries:
        logger.info(f"[competitor_enrichment] generated search queries for {len(queries)} ideas")

    # Phase 2: Run searches per idea
    all_findings: dict[str, dict] = {}
    for i, idea in enumerate(ideas, 1):
        logger.info(f"[competitor_enrichment] {idea.title}")
        _emit_progress(f"competitor mapping: idea {i}/{len(ideas)} — {idea.title}")
        all_findings[idea.idea_id] = _search_for_idea(idea, queries)

    # Phase 3: per-idea Haiku synthesis, run in parallel.
    # Previously this was a single batched call for all ideas — but Haiku's
    # output cap (4000-8000 tokens) gets truncated when synthesising 6+ ideas
    # at once, causing JSON parse failures and ALL ideas getting empty
    # competitor data. Per-idea calls eliminate the truncation risk and
    # ThreadPoolExecutor keeps total wall time low.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = COMPETITOR_SYNTHESIS_MODEL

    def _coerce_str(value, max_len: int | None = None) -> str:
        """Normalise Haiku's JSON values to a string. Haiku is inconsistent —
        sometimes returns these fields as JSON arrays, sometimes as
        comma-separated strings. Always return a string for downstream code."""
        if value is None:
            return ""
        if isinstance(value, list):
            value = ", ".join(str(v).strip() for v in value if v)
        elif not isinstance(value, str):
            value = str(value)
        value = value.strip()
        if max_len is not None and len(value) > max_len:
            value = value[:max_len]
        return value

    updated = 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_to_idea = {
            pool.submit(_synthesize_one_idea, idea, all_findings.get(idea.idea_id, {}), client, model): idea
            for idea in ideas
        }
        for future in as_completed(future_to_idea):
            idea = future_to_idea[future]
            try:
                result = future.result()
                # Truncation cap is a safety net, NOT the target. The prompt above
                # asks Haiku for ~400-600 chars; 1200 here gives headroom without
                # cutting mid-word like the old 800 limit did.
                competitors = _coerce_str(result.get("competitors_india"), max_len=1200)
                if competitors:
                    idea.competitors_india = competitors
                    updated += 1
                else:
                    # Haiku returned empty/falsy for competitors_india. Log
                    # the raw value type + content so we can debug whether
                    # Haiku consistently struggles with certain idea types.
                    raw_val = result.get("competitors_india")
                    logger.warning(
                        f"[competitor_enrichment] empty competitors for "
                        f"'{idea.title}' — Haiku returned: type={type(raw_val).__name__}, "
                        f"value={str(raw_val)[:200]!r}"
                    )
                    idea.competitors_india = (
                        "No clear direct competitors found in Indian marketplaces. "
                        "Treat as white-space opportunity — verify manually before launch."
                    )
                ref_brands = _coerce_str(result.get("reference_brands_global"), max_len=600)
                if ref_brands:
                    idea.reference_brands_global = ref_brands
                best_images = result.get("best_product_images", [])
                if not best_images:
                    raw_images = all_findings.get(idea.idea_id, {}).get("_product_images", [])
                    best_images = [img["image"] for img in raw_images[:3] if img.get("image")]
                idea.comparable_product_images = "\n".join(best_images[:3])
            except Exception as e:
                logger.warning(f"[competitor_enrichment] synthesis failed for '{idea.title}': {e}")

    logger.info(f"[competitor_enrichment] updated competitors on {updated}/{len(ideas)} ideas")
    _emit_progress(f"competitor mapping: complete ({updated}/{len(ideas)} ideas enriched)")
    return ideas


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


def _synthesize_one_idea(idea: IdeaDict, findings: dict, client, model: str) -> dict:
    """Synthesize competitor data for a single idea via Haiku.

    Per-idea calls (instead of one batched call for all ideas) avoid the
    JSON-truncation failure mode that was making competitors_india come back
    empty for every idea in big runs.
    """
    payload = {
        "idea_id": idea.idea_id,
        "title": idea.title,
        "category": idea.category,
        "hero_product": idea.hero_product,
        "problem": idea.problem,
        "existing_competitors": idea.competitors_india,
        "reference_brands_global": idea.reference_brands_global,
        "search_findings": findings,
    }

    prompt = f"""You are a D2C competitive intelligence analyst for the Indian market.

You're synthesizing competitor data for ONE idea. You have:
- existing_competitors: what the idea generator initially listed (often empty or incomplete)
- reference_brands_global: any US/Japan/EU reference brands
- search_findings: fresh results from Amazon.in, Flipkart, specialist marketplaces, quick commerce,
  Google India, and Instagram

Your job: produce a thorough, honest competitive landscape FOR INDIA. Founders need to know
what they're up against — don't downplay competition.

Rules:
- competitors_india: List **5-7 DIRECT competitors in India** — focused, not exhaustive. MINIMUM 1, MAX 8.
  Even if findings are thin, name AT LEAST one competitor (use general knowledge of the Indian D2C
  landscape if needed). Returning empty is failure.
  Format: "BrandA (concise weakness, 3-7 words), BrandB (concise weakness, 3-7 words), ..."
  - Each weakness MUST be 3-7 words. Examples of right length:
    "BrandX (overpriced at ₹1200, weak Instagram presence)"
    "BrandY (chemical-heavy formulation, no ingredient disclosure)"
    "BrandZ (Amazon-only, thin brand story)"
  - DO NOT write full sentences. DO NOT include price ranges with multiple values like "₹800-1,200".
    Pick ONE telling weakness per competitor.
  - Mix: 1-2 funded D2C brands, 1-2 mass-market incumbents, 1-2 unbranded/budget players, 1 specialty if relevant.
  - If only 1-2 competitors come to mind: list those plus "thin direct competition — possible
    white-space opportunity".
  - Do NOT list global brands unless they sell in India (use reference_brands_global for those).
  - Target output length: ~400-600 characters total. Hard ceiling: 1000 characters.
- reference_brands_global: 3-5 US/Japan/EU/Korea brands that inspired or set the benchmark.
  Format: "Brand (country, what they do — 3-6 words)". Example: "Maude (US, minimalist intimate essentials)".
  Target length: ~250-400 characters.
- best_product_images: 2-3 most relevant image URLs from search_findings._product_images
  (prefer global reference brands, then Indian competitors).

Return ONLY a JSON object (NOT an array):
{{"competitors_india": "...", "reference_brands_global": "...", "best_product_images": ["url1", "url2"]}}

Idea:
{json.dumps(payload, indent=2)}"""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        return json.loads(text.strip())
    except Exception as e:
        logger.warning(f"[competitor_enrichment] per-idea synthesis error for '{idea.title}': {e}")
        return {}
