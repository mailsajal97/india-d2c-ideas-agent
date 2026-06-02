"""
AmazonUSCollector — mines complaint clusters across D2C categories.

Uses Exa web_search to find:
1. Direct Amazon review pages (Exa can index some Amazon pages)
2. Cross-platform complaint content about Amazon products (Reddit, YouTube, blogs)

Queries are complaint-themed: "worst X", "1 star review X", "X problems".
Rotates through 3-4 categories per run based on run count.
"""
from __future__ import annotations
import hashlib
import time
from collectors.base_collector import BaseCollector
from utils.signal_schema import RawSignal
from utils.tools import web_search, fetch_page
from utils.db import is_seen, mark_seen, get_run_count
from utils.logger import get_logger

logger = get_logger()

# Category search queries — ONE consolidated query per category.
# Trimmed from 4 queries/category to 1 (the cross-platform Reddit+Amazon
# complaint search, which was the highest-signal of the original four). The
# saved Exa budget moves to india_marketplaces_collector, which is more
# valuable for an India-launching founder than US-side complaints.
CATEGORY_QUERIES: dict[str, list[str]] = {
    "beauty": [
        '"1 star review" shampoo OR moisturizer OR sunscreen OR face wash complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "personal_care": [
        '"1 star review" deodorant OR toothpaste OR body wash OR shaving complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "food_cpg": [
        '"1 star review" protein bar OR snacks OR kombucha OR condiments complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "home": [
        '"1 star review" cleaning OR laundry OR kitchen tools OR candles complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "pet": [
        '"1 star review" dog food OR cat litter OR pet grooming complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "wellness": [
        '"1 star review" sleep gummies OR magnesium OR ashwagandha OR massage gun complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "sexual_wellness": [
        '"1 star review" lube OR intimacy product OR libido supplement complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "innerwear_loungewear": [
        '"1 star review" innerwear OR loungewear OR socks OR basic tees complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "fashion_apparel": [
        '"1 star review" dress OR shirt OR ethnic wear sizing OR fabric complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "footwear": [
        '"1 star review" sneakers OR sandals OR formal shoes complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "jewellery_watches": [
        '"1 star review" fashion jewellery OR smartwatch complaints tarnish OR battery '
        'site:reddit.com OR site:amazon.com',
    ],
    "eyewear": [
        '"1 star review" sunglasses OR prescription glasses complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "accessories": [
        '"1 star review" travel bag OR laptop sleeve OR tech accessories complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "consumer_electronics": [
        '"1 star review" wireless earbuds OR bluetooth speaker OR smart home complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "baby_kids": [
        '"1 star review" baby care OR kids toys OR kids snacks complaints '
        'site:reddit.com OR site:amazon.com',
    ],
    "fitness_nutrition": [
        '"1 star review" resistance bands OR yoga mat OR whey protein OR creatine complaints '
        'site:reddit.com OR site:amazon.com',
    ],
}


class AmazonUSCollector(BaseCollector):
    source_name = "amazon_us"

    def __init__(self, profile: dict | None = None, **kwargs):
        self.profile = profile or {}
        self.config = self.profile.get("collection", {}).get("amazon_us", {})

    def fetch(self) -> list[RawSignal]:
        signals: list[RawSignal] = []
        run_count = get_run_count()
        all_categories = list(CATEGORY_QUERIES.keys())

        # Filter to user's PICKED categories only. The original tool used all
        # categories because the founder had picked all of them; for the skill,
        # rotating through unpicked categories produces wasted Exa calls and
        # off-topic signals that just get dropped downstream.
        profile_cats = self.profile.get("categories", {})
        picked = [
            c for c in all_categories
            if isinstance(profile_cats.get(c), dict)
            and profile_cats[c].get("weight", 0) > 0
        ]

        if not picked:
            # No picked categories in profile — fall back to all, so the
            # collector still does something useful for an unconfigured skill.
            picked = all_categories
            logger.warning(
                "[amazon_us] profile has no picked categories, falling back to all 16"
            )

        # Rotate: pick 3-4 categories per run. If user picked few categories,
        # use all of them (no rotation needed).
        cats_per_run = self.config.get("categories_per_run", 4)
        if len(picked) <= cats_per_run:
            selected = picked
        else:
            start = (run_count * cats_per_run) % len(picked)
            selected = (picked * 2)[start:start + cats_per_run]
        lookback = self.config.get("lookback_days", 30)

        logger.info(f"[amazon_us] run #{run_count}, categories: {selected} (filtered from {len(picked)} picked)")

        for cat in selected:
            queries = CATEGORY_QUERIES.get(cat, [])
            for q in queries:
                try:
                    results = web_search(
                        query=q,
                        num_results=5,
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
                                "category": cat,
                                "query": q,
                                "signal_type": "complaint",
                            },
                        ))
                        mark_seen(self.source_name, sig_id)

                except Exception as e:
                    logger.warning(f"[amazon_us] query failed for '{q}': {e}")

                time.sleep(0.3)  # Exa rate limit

        return signals
