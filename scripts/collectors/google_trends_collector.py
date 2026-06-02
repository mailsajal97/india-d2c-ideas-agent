"""
GoogleTrendsCollector — detects geographic arbitrage between India and US.

Uses pytrends to compare Google Trends interest for D2C product keywords
between India and US. Signals when US interest is notably higher (arbitrage
opportunity) or when India interest is rising fast.

Rotates through 3-4 categories per run. Rate-limited to ~1 req/sec for pytrends.
"""
from __future__ import annotations
import time
from collectors.base_collector import BaseCollector
from utils.signal_schema import RawSignal
from utils.db import is_seen, mark_seen, get_run_count
from utils.logger import get_logger

logger = get_logger()

# Product keywords grouped by category — curated for D2C relevance.
# Keys MUST match the canonical category keys in scripts/setup.py CATEGORIES.
TREND_KEYWORDS: dict[str, list[str]] = {
    "beauty": [
        "shampoo bar", "rice water shampoo", "niacinamide serum",
        "snail mucin", "hair oil serum", "retinol cream", "lip oil",
    ],
    "personal_care": [
        "bamboo toothbrush", "natural deodorant", "beard oil",
        "intimate wash", "tongue scraper", "charcoal toothpaste",
    ],
    "food_cpg": [
        "kombucha", "oat milk", "millet snacks",
        "cold brew coffee", "sparkling water", "sugar free chocolate",
    ],
    "home": [
        "scented candles", "fabric softener sheets", "reusable wraps",
        "drawer organizer", "cleaning tablets", "laundry strips",
    ],
    "pet": [
        "dog food", "cat litter", "dog treats",
        "pet grooming brush", "interactive cat toy",
    ],
    "wellness": [
        "sleep gummies", "magnesium spray", "acupressure mat",
        "weighted blanket", "probiotic powder", "ashwagandha gummies",
    ],
    "sexual_wellness": [
        "couples intimacy products", "water based lube", "performance gummies",
        "libido supplement", "premium intimate wash",
    ],
    "innerwear_loungewear": [
        "bamboo socks", "seamless underwear", "organic cotton tee",
        "loungewear set", "merino wool base layer",
    ],
    "fashion_apparel": [
        "linen shirt men", "co-ord set women", "cargo pants",
        "ethnic wear", "kurta set", "athleisure dress",
    ],
    "footwear": [
        "white sneakers", "running shoes", "sandals men",
        "formal shoes", "athleisure shoes",
    ],
    "jewellery_watches": [
        "fashion jewellery", "lab grown diamond", "smartwatch",
        "anklet design", "ear cuffs",
    ],
    "eyewear": [
        "blue light glasses", "sunglasses men", "prescription frames",
        "polarised sunglasses", "kids eyewear",
    ],
    "accessories": [
        "travel organizer", "laptop sleeve", "phone grip",
        "cable organizer", "passport holder",
    ],
    "consumer_electronics": [
        "wireless earbuds", "bluetooth speaker", "smart bulb",
        "robot vacuum", "phone tripod",
    ],
    "baby_kids": [
        "montessori toys", "silicone bibs", "baby food maker",
        "sensory toys", "kids activity kit",
    ],
    "fitness_nutrition": [
        "whey protein", "mass gainer", "creatine monohydrate",
        "pre workout", "massage gun", "resistance bands", "foam roller",
    ],
}


class GoogleTrendsCollector(BaseCollector):
    source_name = "google_trends"

    def __init__(self, profile: dict | None = None, **kwargs):
        self.profile = profile or {}
        self.config = self.profile.get("collection", {}).get("google_trends", {})

    def fetch(self) -> list[RawSignal]:
        # Import pytrends lazily — it's not always installed
        try:
            from pytrends.request import TrendReq
        except ImportError:
            logger.warning("[google_trends] pytrends not installed — skipping")
            return []

        signals: list[RawSignal] = []
        pytrends = TrendReq(hl="en-US", tz=330)  # IST offset

        run_count = get_run_count()
        all_categories = list(TREND_KEYWORDS.keys())

        # Filter to user's PICKED categories only (skill-version fix —
        # otherwise we'd burn Google Trends rate-limit quota on categories
        # the user doesn't care about).
        profile_cats = self.profile.get("categories", {})
        picked = [
            c for c in all_categories
            if isinstance(profile_cats.get(c), dict)
            and profile_cats[c].get("weight", 0) > 0
        ]
        if not picked:
            picked = all_categories
            logger.warning(
                "[google_trends] profile has no picked categories, falling back to all"
            )

        cats_per_run = self.config.get("categories_per_run", 4)
        if len(picked) <= cats_per_run:
            selected_cats = picked
        else:
            start = (run_count * cats_per_run) % len(picked)
            selected_cats = (picked * 2)[start:start + cats_per_run]
        timeframe = self.config.get("timeframe", "today 12-m")

        logger.info(f"[google_trends] run #{run_count}, categories: {selected_cats} (filtered from {len(picked)} picked)")

        for cat in selected_cats:
            keywords = TREND_KEYWORDS.get(cat, [])
            for kw in keywords:
                try:
                    # Compare India vs US interest
                    pytrends.build_payload([kw], timeframe=timeframe, geo="")
                    interest_by_region = pytrends.interest_by_region(
                        resolution="COUNTRY", inc_low_vol=True
                    )

                    us_interest = 0
                    india_interest = 0
                    if not interest_by_region.empty and kw in interest_by_region.columns:
                        if "United States" in interest_by_region.index:
                            us_interest = int(interest_by_region.loc["United States", kw])
                        if "India" in interest_by_region.index:
                            india_interest = int(interest_by_region.loc["India", kw])

                    # Signal if US interest is notably higher (arbitrage) or US interest is strong
                    ratio = us_interest / max(india_interest, 1)

                    if ratio <= 2 and us_interest <= 50:
                        time.sleep(4)  # pytrends rate limit even for skipped keywords
                        continue

                    sig_id = f"trends_{kw.replace(' ', '_')}_{run_count}"
                    if is_seen(self.source_name, sig_id):
                        time.sleep(4)
                        continue

                    # Get related rising queries for richer signal
                    related = ""
                    try:
                        pytrends.build_payload([kw], timeframe="today 12-m", geo="US")
                        related_queries = pytrends.related_queries()
                        if (
                            kw in related_queries
                            and related_queries[kw].get("rising") is not None
                            and not related_queries[kw]["rising"].empty
                        ):
                            top_rising = related_queries[kw]["rising"].head(5)
                            related = ", ".join(top_rising["query"].tolist())
                    except Exception:
                        pass

                    body = (
                        f"Google Trends comparison for '{kw}':\n"
                        f"US interest: {us_interest}/100\n"
                        f"India interest: {india_interest}/100\n"
                        f"US/India ratio: {ratio:.1f}x\n"
                        f"Category: {cat}\n"
                    )
                    if related:
                        body += f"Related rising queries (US): {related}\n"

                    signals.append(RawSignal(
                        id=sig_id,
                        source=self.source_name,
                        title=f"Trend gap: '{kw}' — US {us_interest} vs India {india_interest}",
                        body=body,
                        url=f"https://trends.google.com/trends/explore?q={kw.replace(' ', '+')}&geo=",
                        created_at="",
                        extra={
                            "keyword": kw,
                            "category": cat,
                            "trend_us": us_interest,
                            "trend_india": india_interest,
                            "ratio": round(ratio, 2),
                            "related_rising": related,
                            "signal_type": "trend",
                        },
                    ))
                    mark_seen(self.source_name, sig_id)

                    time.sleep(4)  # pytrends rate limit (important — Google throttles)

                except Exception as e:
                    # Demoted from WARNING to DEBUG: pytrends 429s are
                    # EXPECTED (Google rate-limits aggressively, 80%+ of
                    # queries get blocked in a typical run). Logging them at
                    # WARNING was spamming stdout/Monitor and confusing the
                    # chat's progress relay. They still get captured at DEBUG
                    # in scripts/logs/run.log for diagnostics.
                    logger.debug(f"[google_trends] error for '{kw}': {e}")
                    time.sleep(5)  # back off on errors
                    continue

        return signals
