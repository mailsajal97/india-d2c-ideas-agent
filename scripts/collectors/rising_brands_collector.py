"""
RisingBrandsCollector — finds rising US D2C brands via Exa searches.

Queries 4 US D2C trade publications (Modern Retail, Retail Brew, The Fascination,
Exploding Topics) with category-specific terms. Feeds the "Rising US D2C Brands
-> India Gap" lens — we look at what's launching/funding/trending in the US so
the founder can spot opportunities India hasn't filled yet.

Architecture: per-category query pools rather than generic templates with
category suffixes. This lets us use category-natural vocabulary
("audio wearable" not "consumer electronics brand") and target publications
that lean into specific categories.
"""
from __future__ import annotations
import hashlib
import random
import time
from datetime import date

from collectors.base_collector import BaseCollector
from utils.db import is_seen, mark_seen
from utils.signal_schema import RawSignal
from utils.tools import web_search, fetch_page
from utils.logger import get_logger

logger = get_logger()


# Per-category query pools. Each entry: (query, site_filter, description).
# Roughly 6 queries per category, spread across the 4 publications, using
# category-natural vocabulary.
CATEGORY_QUERY_POOLS: dict[str, list[tuple[str, str, str]]] = {
    "beauty": [
        ("new clean beauty brand launch 2026", "site:modernretail.co", "Modern Retail"),
        ("indie skincare brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising K-beauty trend US market", "site:explodingtopics.com", "Exploding Topics"),
        ("emerging skincare DTC brand to watch", "site:thefascination.com", "The Fascination"),
        ("new haircare D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("emerging makeup brand 2026 funding", "site:retailbrew.com", "Retail Brew"),
    ],
    "personal_care": [
        ("new personal care D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("natural deodorant brand venture funding 2026", "site:retailbrew.com", "Retail Brew"),
        ("oral care D2C brand trending", "site:thefascination.com", "The Fascination"),
        ("rising body care category US market", "site:explodingtopics.com", "Exploding Topics"),
        ("emerging shampoo body wash brand 2026", "site:modernretail.co", "Modern Retail"),
        ("indie hygiene grooming brand to watch", "site:retailbrew.com", "Retail Brew"),
    ],
    "food_cpg": [
        ("new healthy snack brand launch 2026", "site:modernretail.co", "Modern Retail"),
        ("functional beverage brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising better-for-you food category US", "site:explodingtopics.com", "Exploding Topics"),
        ("emerging condiment sauce brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("new CPG D2C brand launch direct to consumer", "site:modernretail.co", "Modern Retail"),
        ("indie snack beverage brand 2026 trending", "site:thefascination.com", "The Fascination"),
    ],
    "home": [
        ("new home goods D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("rising home cleaning brand category 2026", "site:explodingtopics.com", "Exploding Topics"),
        ("emerging candle decor brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("indie home care brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("new kitchen tools D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("trending home essentials brand 2026", "site:thefascination.com", "The Fascination"),
    ],
    "pet": [
        ("new pet food brand launch 2026", "site:modernretail.co", "Modern Retail"),
        ("rising pet care category trending", "site:explodingtopics.com", "Exploding Topics"),
        ("emerging dog cat brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("indie pet wellness brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("new pet accessories DTC brand launch", "site:modernretail.co", "Modern Retail"),
        ("trending pet supplement gummies brand", "site:thefascination.com", "The Fascination"),
    ],
    "wellness": [
        ("new wellness supplement brand launch 2026", "site:modernretail.co", "Modern Retail"),
        ("sleep stress recovery brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising adaptogen functional wellness category", "site:explodingtopics.com", "Exploding Topics"),
        ("emerging gummies vitamin brand to watch", "site:thefascination.com", "The Fascination"),
        ("new topical wellness D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("indie wellness brand 2026 trending", "site:retailbrew.com", "Retail Brew"),
    ],
    "sexual_wellness": [
        ("new sexual wellness brand launch 2026", "site:modernretail.co", "Modern Retail"),
        ("intimacy brand venture funding D2C", "site:retailbrew.com", "Retail Brew"),
        ("rising premium intimacy product category", "site:explodingtopics.com", "Exploding Topics"),
        ("emerging sexual wellness brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("indie intimacy hormonal performance brand", "site:thefascination.com", "The Fascination"),
        ("trending sexual wellness DTC brand 2026", "site:modernretail.co", "Modern Retail"),
    ],
    "innerwear_loungewear": [
        ("new innerwear DTC brand launch", "site:modernretail.co", "Modern Retail"),
        ("loungewear basics brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising sock underwear brand category", "site:explodingtopics.com", "Exploding Topics"),
        ("emerging comfort apparel brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("new sleepwear tee D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("indie loungewear brand 2026 trending", "site:thefascination.com", "The Fascination"),
    ],
    "fashion_apparel": [
        ("new fashion DTC brand launch 2026", "site:modernretail.co", "Modern Retail"),
        ("emerging clothing brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising apparel category US market trending", "site:explodingtopics.com", "Exploding Topics"),
        ("indie fashion brand to watch women's wear", "site:retailbrew.com", "Retail Brew"),
        ("new ethnic modest fashion D2C launch", "site:modernretail.co", "Modern Retail"),
        ("trending fashion brand 2026 funding", "site:thefascination.com", "The Fascination"),
    ],
    "footwear": [
        ("new sneaker DTC brand launch 2026", "site:modernretail.co", "Modern Retail"),
        ("emerging footwear brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising athleisure shoe category trending", "site:explodingtopics.com", "Exploding Topics"),
        ("indie sandal sneaker brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("new comfort footwear D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("trending footwear brand 2026 direct consumer", "site:thefascination.com", "The Fascination"),
    ],
    "jewellery_watches": [
        ("new fashion jewelry DTC brand launch", "site:modernretail.co", "Modern Retail"),
        ("emerging smartwatch wearable brand funding", "site:retailbrew.com", "Retail Brew"),
        ("rising fine jewelry category US trending", "site:explodingtopics.com", "Exploding Topics"),
        ("indie jewelry brand to watch direct consumer", "site:retailbrew.com", "Retail Brew"),
        ("new lab grown diamond D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("trending watch brand 2026 venture funded", "site:thefascination.com", "The Fascination"),
    ],
    "eyewear": [
        ("new eyewear DTC brand launch 2026", "site:modernretail.co", "Modern Retail"),
        ("emerging sunglasses brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising prescription glasses category trending", "site:explodingtopics.com", "Exploding Topics"),
        ("indie eyewear brand to watch direct consumer", "site:retailbrew.com", "Retail Brew"),
        ("new blue light glasses D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("trending eyewear brand 2026 funded", "site:thefascination.com", "The Fascination"),
    ],
    "accessories": [
        ("new bag travel accessory DTC brand launch", "site:modernretail.co", "Modern Retail"),
        ("emerging backpack brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising tech accessory category trending", "site:explodingtopics.com", "Exploding Topics"),
        ("indie leather goods brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("new small leather goods D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("trending laptop sleeve travel gear brand 2026", "site:thefascination.com", "The Fascination"),
    ],
    "consumer_electronics": [
        ("new audio wearable DTC brand launch", "site:modernretail.co", "Modern Retail"),
        ("emerging consumer electronics brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising smart home category trending US", "site:explodingtopics.com", "Exploding Topics"),
        ("indie consumer tech brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("new earbuds speaker D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("trending consumer gadget brand 2026 funded", "site:thefascination.com", "The Fascination"),
    ],
    "baby_kids": [
        ("new baby DTC brand launch 2026", "site:modernretail.co", "Modern Retail"),
        ("emerging baby care brand venture funding", "site:retailbrew.com", "Retail Brew"),
        ("rising kids learning toy category trending", "site:explodingtopics.com", "Exploding Topics"),
        ("indie kids feeding brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("new diapers wipes D2C brand launch", "site:modernretail.co", "Modern Retail"),
        ("trending baby clothing brand 2026 funded", "site:thefascination.com", "The Fascination"),
    ],
    "fitness_nutrition": [
        ("new protein supplement DTC brand launch", "site:modernretail.co", "Modern Retail"),
        ("emerging fitness brand venture funding 2026", "site:retailbrew.com", "Retail Brew"),
        ("rising functional food gummies category", "site:explodingtopics.com", "Exploding Topics"),
        ("indie fitness gear brand to watch", "site:retailbrew.com", "Retail Brew"),
        ("new resistance band yoga mat D2C launch", "site:modernretail.co", "Modern Retail"),
        ("trending pre-workout creatine brand 2026", "site:thefascination.com", "The Fascination"),
    ],
}

# How many queries to actually fire per run.
QUERIES_PER_RUN = 4


def _build_queries(picked: list[str], rng: random.Random) -> list[tuple[str, str, str, str]]:
    """Build per-category query list from user's picked categories.

    Returns list of (query, site_filter, desc, category) tuples.

    Logic:
    - 1 picked: sample 4 queries from that category's pool
    - 2-4 picked: 1 query per picked category
    - 5+ picked: sample 4 picked categories, 1 query each
    """
    out: list[tuple[str, str, str, str]] = []

    if len(picked) == 1:
        cat = picked[0]
        pool = CATEGORY_QUERY_POOLS.get(cat, [])
        if not pool:
            return []
        sample_n = min(QUERIES_PER_RUN, len(pool))
        for q, site, desc in rng.sample(pool, sample_n):
            out.append((q, site, desc, cat))
    elif len(picked) <= QUERIES_PER_RUN:
        # 1 query per picked category
        for cat in picked:
            pool = CATEGORY_QUERY_POOLS.get(cat, [])
            if not pool:
                continue
            q, site, desc = rng.choice(pool)
            out.append((q, site, desc, cat))
    else:
        # 5+ picked: sample QUERIES_PER_RUN categories, 1 query each
        sampled_cats = rng.sample(picked, QUERIES_PER_RUN)
        for cat in sampled_cats:
            pool = CATEGORY_QUERY_POOLS.get(cat, [])
            if not pool:
                continue
            q, site, desc = rng.choice(pool)
            out.append((q, site, desc, cat))

    return out


class RisingBrandsCollector(BaseCollector):
    source_name = "rising_brands"

    def __init__(self, profile: dict | None = None, **kwargs):
        self.profile = profile or {}
        self.config = self.profile.get("collection", {}).get("rising_brands", {})

    def fetch(self) -> list[RawSignal]:
        signals: list[RawSignal] = []
        max_results = self.config.get("max_results", 30)
        lookback = self.config.get("lookback_days", 14)

        # Filter to picked categories
        profile_cats = self.profile.get("categories", {})
        picked = [
            c for c, v in profile_cats.items()
            if isinstance(v, dict) and v.get("weight", 0) > 0
        ]
        if not picked:
            logger.info("[rising_brands] no picked categories — skipping")
            return signals

        # Seeded RNG so same-day runs are reproducible, different days vary.
        rng = random.Random(date.today().isoformat() + "rising_brands")
        category_tagged_sources = _build_queries(picked, rng)

        if not category_tagged_sources:
            logger.info("[rising_brands] no queries built — skipping")
            return signals

        per_query = max(2, max_results // len(category_tagged_sources))

        logger.info(
            f"[rising_brands] {len(category_tagged_sources)} queries across "
            f"{len(set(s[3] for s in category_tagged_sources))} categories: "
            f"{[s[2] + '/' + s[3] for s in category_tagged_sources]}"
        )

        for query, site_filter, desc, _cat in category_tagged_sources:
            full_query = f"{site_filter} {query}" if site_filter else query
            try:
                results = web_search(
                    query=full_query,
                    num_results=per_query,
                    days_back=lookback,
                )
                for r in results:
                    if "error" in r:
                        continue
                    url = r.get("url", "")
                    if not url:
                        continue

                    sig_id = hashlib.sha256(url.encode()).hexdigest()[:16]
                    if is_seen(self.source_name, sig_id):
                        continue

                    # Fetch page for richer content
                    page_text = ""
                    try:
                        page_text = fetch_page(url, max_chars=3000)
                    except Exception:
                        pass

                    body = page_text if page_text else r.get("snippet", "")

                    signals.append(RawSignal(
                        id=sig_id,
                        source=self.source_name,
                        title=r.get("title", ""),
                        body=body[:3000],
                        url=url,
                        created_at=r.get("published_date", ""),
                        extra={
                            "source_site": desc,
                            "signal_type": "rising_brand",
                            "for_category": _cat,
                        },
                    ))
                    mark_seen(self.source_name, sig_id)

            except Exception as e:
                logger.warning(f"[rising_brands] query failed for '{full_query}': {e}")

            time.sleep(0.3)  # Exa rate limit

        return signals
