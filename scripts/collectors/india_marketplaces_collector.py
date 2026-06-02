"""
IndiaMarketplacesCollector — mines D2C signal directly from Indian marketplaces
and quick commerce platforms via Exa.

This is the India-direct counterpart to amazon_us_collector. Where amazon_us
infers India opportunities from US consumer pain, this collector queries
the marketplaces Indian consumers actually buy from — surfacing real demand,
real complaints, and real price points.

Category-aware: each category queries the marketplaces that specialise in
that category (Nykaa for beauty, Lenskart for eyewear, etc.). Quick commerce
queries fire only for FMCG-relevant categories — fashion and electronics
don't sell on Blinkit/Zepto in meaningful volume yet.

Signals from this collector get tagged with signal_type per query type
("complaint_cluster" for review/complaint queries, "explicit_demand" for
bestseller/category-trend queries) so the signal enrichment agent routes
them through Lens A (India demand).
"""
from __future__ import annotations

import hashlib
import random
import time
from datetime import date, datetime

from collectors.base_collector import BaseCollector
from utils.db import is_seen, mark_seen
from utils.signal_schema import RawSignal
from utils.tools import web_search
from utils.logger import get_logger

logger = get_logger()


# Category → specialist Indian marketplaces (in priority order).
# Each category gets 2-3 marketplaces. Amazon.in is included almost everywhere
# as a fallback because its catalog is broad and reviews are well-indexed.
INDIA_MARKETPLACES: dict[str, list[str]] = {
    "beauty": ["nykaa.com", "purplle.com", "amazon.in"],
    "personal_care": ["nykaa.com", "purplle.com", "amazon.in"],
    "food_cpg": ["bigbasket.com", "amazon.in"],
    "home": ["amazon.in", "flipkart.com", "pepperfry.com"],
    "pet": ["supertails.com", "headsupfortails.com", "amazon.in"],
    "wellness": ["healthkart.com", "1mg.com", "amazon.in"],
    "sexual_wellness": ["mymuse.in", "bold.care", "amazon.in"],
    "innerwear_loungewear": ["myntra.com", "amazon.in"],
    "fashion_apparel": ["myntra.com", "ajio.com", "nykaafashion.com"],
    "footwear": ["myntra.com", "ajio.com", "amazon.in"],
    "jewellery_watches": ["tata-cliq.com", "myntra.com", "amazon.in"],
    "eyewear": ["lenskart.com", "amazon.in"],
    "accessories": ["myntra.com", "amazon.in"],
    "consumer_electronics": ["amazon.in", "flipkart.com", "croma.com"],
    "baby_kids": ["firstcry.com", "hopscotch.com", "amazon.in"],
    "fitness_nutrition": ["healthkart.com", "decathlon.in", "amazon.in"],
}


# Categories where quick commerce (Blinkit / Zepto / Instamart / BBNow) is
# actually a relevant signal. Fashion, electronics, jewelry, etc. don't sell
# in meaningful volume on QC platforms today.
QC_RELEVANT_CATEGORIES = {
    "beauty",
    "personal_care",
    "food_cpg",
    "home",
    "pet",
    "wellness",
    "sexual_wellness",
    "baby_kids",
    "fitness_nutrition",
}


# Combined OR query across the major Indian quick commerce platforms.
# NOTE: BigBasket is intentionally NOT here even though it has a QC arm —
# bigbasket.com already appears in INDIA_MARKETPLACES["food_cpg"]. Including
# it here too would query the same site twice with overlapping keywords.
QC_DOMAINS = (
    "site:blinkit.com OR site:zeptonow.com OR site:swiggy.com/instamart"
)


# Category-specific keyword phrases — what to actually search for inside a
# marketplace. Keeps queries concrete (Nykaa "skincare reviews" is much better
# signal than Nykaa "products").
CATEGORY_KEYWORDS: dict[str, str] = {
    "beauty": "skincare OR haircare OR makeup",
    "personal_care": "deodorant OR shampoo OR body wash OR oral care",
    "food_cpg": "healthy snacks OR beverages OR condiments",
    "home": "cleaning OR candles OR home decor",
    "pet": "dog food OR cat litter OR pet care",
    "wellness": "sleep gummies OR magnesium OR ashwagandha",
    "sexual_wellness": "intimacy products OR sexual wellness",
    "innerwear_loungewear": "innerwear OR loungewear OR socks",
    "fashion_apparel": "women fashion OR men fashion OR ethnic wear",
    "footwear": "sneakers OR sandals OR formal shoes",
    "jewellery_watches": "fashion jewellery OR smartwatch",
    "eyewear": "sunglasses OR prescription glasses",
    "accessories": "travel bag OR backpack OR laptop sleeve",
    "consumer_electronics": "wireless earbuds OR bluetooth speaker OR smart home",
    "baby_kids": "baby care OR kids toys OR feeding",
    "fitness_nutrition": "whey protein OR resistance bands OR yoga mat",
}


# Cap total Exa queries per run regardless of how many categories the user
# picked. Each query returns 5-8 results, so 15 queries ≈ 75-120 raw signals
# from this collector alone — plenty for enrichment to chew on.
MAX_QUERIES_PER_RUN = 15

# Per-query result count (Exa default is 10, lower keeps cost predictable)
NUM_RESULTS = 5

# Rate-limit pause between Exa queries
RATE_LIMIT_SLEEP = 0.3

# Query templates — multiple variants per intent so we sample different
# phrasings across runs. Same Exa cost, broader signal coverage over time.
# Templates use {site} and {keyword} as placeholders.
COMPLAINT_TEMPLATES = [
    'site:{site} {keyword} review complaints OR "1 star" OR "poor quality"',
    'site:{site} {keyword} "did not work" OR "waste of money" OR "returned"',
    'site:{site} {keyword} disappointing OR "not as described" OR refund',
]

DEMAND_TEMPLATES = [
    'site:{site} bestseller OR "top rated" {keyword} 2026',
    'site:{site} {keyword} "most loved" OR "customer favorite" trending',
    'site:{site} {keyword} new launch OR "just dropped" 2026',
]


class IndiaMarketplacesCollector(BaseCollector):
    source_name = "india_marketplaces"

    def __init__(self, profile: dict | None = None, **kwargs):
        self.profile = profile or {}
        self.config = self.profile.get("collection", {}).get("india_marketplaces", {})

    def fetch(self) -> list[RawSignal]:
        signals: list[RawSignal] = []

        # Filter to user's picked categories
        profile_cats = self.profile.get("categories", {})
        picked = [
            c for c in INDIA_MARKETPLACES.keys()
            if isinstance(profile_cats.get(c), dict)
            and profile_cats[c].get("weight", 0) > 0
        ]
        if not picked:
            logger.warning(
                "[india_marketplaces] profile has no picked categories, skipping"
            )
            return []

        logger.info(
            f"[india_marketplaces] picked categories: {picked} "
            f"(query cap: {MAX_QUERIES_PER_RUN})"
        )

        # Build the full task list FIRST so we can cap at MAX_QUERIES_PER_RUN
        # across all categories rather than starving the last categories of queries.
        tasks: list[tuple[str, str, str, str]] = []  # (category, marketplace, query, kind)

        # Seed query template selection by today's date so same-day runs are
        # reproducible but different days hit different query phrasings.
        rng = random.Random(date.today().isoformat() + "india_marketplaces")

        for cat in picked:
            keyword = CATEGORY_KEYWORDS.get(cat, cat.replace("_", " "))
            marketplaces = INDIA_MARKETPLACES.get(cat, ["amazon.in"])

            # 1 query per marketplace for this category — alternates between
            # complaint-focused and demand-focused so we get both lenses.
            # Template is sampled from the pool per (category, marketplace) so
            # query phrasings vary across runs.
            for i, mp in enumerate(marketplaces[:3]):  # cap at 3 marketplaces per category
                if i % 2 == 0:
                    template = rng.choice(COMPLAINT_TEMPLATES)
                    kind = "complaint_cluster"
                else:
                    template = rng.choice(DEMAND_TEMPLATES)
                    kind = "explicit_demand"
                q = template.format(site=mp, keyword=keyword)
                tasks.append((cat, mp, q, kind))

            # Quick commerce query — only for FMCG-relevant categories
            if cat in QC_RELEVANT_CATEGORIES:
                q = f"{QC_DOMAINS} {keyword}"
                tasks.append((cat, "quick_commerce", q, "explicit_demand"))

        # Cap total queries per run. Round-robin across categories instead of
        # truncating last categories — ensures every picked category gets at
        # least one query even with many categories selected.
        if len(tasks) > MAX_QUERIES_PER_RUN:
            tasks = _round_robin_truncate(tasks, MAX_QUERIES_PER_RUN)

        logger.info(f"[india_marketplaces] running {len(tasks)} queries")

        # Run queries (sequential — could parallelize later, but Exa rate
        # limit makes sequential fine for this volume)
        for cat, mp, query, signal_kind in tasks:
            try:
                results = web_search(
                    query=query,
                    num_results=NUM_RESULTS,
                    days_back=365,
                )
                for r in results:
                    if "error" in r or not r.get("url"):
                        continue
                    sig_id = hashlib.sha256(
                        f"{mp}|{r.get('url', '')}".encode()
                    ).hexdigest()[:16]
                    if is_seen(self.source_name, sig_id):
                        continue
                    signals.append(RawSignal(
                        id=sig_id,
                        source=self.source_name,
                        title=r.get("title", "")[:200],
                        body=(r.get("snippet") or "")[:1500],
                        url=r.get("url", ""),
                        created_at=r.get("published_date", ""),
                        extra={
                            "category": cat,
                            "marketplace": mp,
                            "signal_type": signal_kind,
                            "query_kind": signal_kind,
                        },
                    ))
                    mark_seen(self.source_name, sig_id)
                time.sleep(RATE_LIMIT_SLEEP)
            except Exception as e:
                logger.warning(
                    f"[india_marketplaces] query failed ({cat}/{mp}): {e}"
                )

        return signals


def _round_robin_truncate(tasks: list, cap: int) -> list:
    """
    When the task list exceeds the cap, take queries round-robin across
    categories so every category gets at least one query. Within a category,
    earlier-indexed queries (complaint > demand > quick commerce) are kept
    first.
    """
    by_cat: dict[str, list] = {}
    for t in tasks:
        by_cat.setdefault(t[0], []).append(t)

    result = []
    while len(result) < cap and any(by_cat.values()):
        for cat in list(by_cat.keys()):
            if not by_cat[cat]:
                del by_cat[cat]
                continue
            result.append(by_cat[cat].pop(0))
            if len(result) >= cap:
                break
    return result
