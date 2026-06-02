"""
Data schema for D2C Idea Finder pipeline.
Three core dataclasses: RawSignal → NormalizedSignal → IdeaDict
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class RawSignal:
    """Raw signal from a collector (reviews, Reddit, YouTube, etc.)."""
    id: str                     # unique per source
    source: str                 # "amazon_us", "amazon_in", "nykaa", "reddit_us", "youtube", etc.
    title: str
    body: str                   # raw text (review text, post body, transcript excerpt)
    url: str
    created_at: str             # ISO string
    extra: dict = field(default_factory=dict)
    # extra keys by source:
    #   amazon_us: {rating, category, product_name, review_count, signal_type}
    #   rising_brands: {brand_name, brand_category, brand_country, signal_type: "rising_brand"}
    #   google_trends: {keyword, trend_us, trend_india, ratio, signal_type: "trend"}
    #   reddit_us: {subreddit, score, num_comments, has_comments}
    #   youtube: {channel, views, has_transcript}
    #   instagram: {hashtag, likes, is_indian}
    #   quick_commerce: {platform, category, in_stock}


@dataclass
class NormalizedSignal:
    """Signal after enrichment agent validates and normalizes."""
    id: str
    signal_type: str                    # "complaint_cluster", "explicit_demand", "geo_arbitrage",
                                        # "format_shift", "momentum", "white_space",
                                        # "unbranded_market", "rising_brand_gap"
    summary: str                        # one-line description
    category_tags: list[str]            # from 12 fixed categories
    consumer_tags: list[str]            # ["urban_women_25_35", "fitness_enthusiasts", etc.]
    evidence_strength: str              # "high", "medium", "low"
    recency: str                        # ISO date
    key_entities: list[str]             # brand names, ingredients, product names
    raw_source: str                     # original source name
    raw_url: str
    pain_acuity: Optional[int] = None   # 1-5 for complaint/demand signals, null for trends
    gap_dimensions: list[str] = field(default_factory=list)  # what's missing/weak
    enrichment_notes: str = ""          # structured insight
    geo_markets: list[str] = field(default_factory=list)     # ["US", "Japan", "Europe", "Korea"]


@dataclass
class IdeaDict:
    """Final idea card — written to Google Sheets + email."""
    # Meta
    idea_id: str                        # UUID
    run_date: str                       # YYYY-MM-DD

    # Core idea
    category: str                       # one of 12 fixed categories
    title: str                          # 3-5 words
    tagline: str                        # one-sentence pitch with price + persona
    problem: str                        # specific consumer pain
    target_consumer: str                # detailed persona (age, city tier, lifestyle)
    market_size_estimate: str           # e.g., "₹500Cr Indian haircare market, shampoo bars <1%"
    why_now: str                        # timing: ingredient trending, format shift, arbitrage

    # Product specifics
    hero_product: str                   # specific SKU: format, ingredients, price point
    hero_product_detail: str            # expanded: full product spec, variants, packaging, unit economics
    aov_estimate: str                   # e.g., "₹899"
    margin_estimate: str                # e.g., "65-70%"
    capital_required_estimate: str      # e.g., "₹8-12L"
    first_year_revenue_estimate: str    # e.g., "₹18-25L" with assumptions
    sourcing_approach: str              # contract mfg, white-label, import; MOQ, first-batch cost

    # Go-to-market — the IDEAL launch playbook for the product, regardless of
    # which channels the founder currently has. Channel-match is scored
    # separately as distribution_fit so we can keep the GTM unbiased.
    # (Previously had a second field `gtm_amplifiers` to call out gaps; dropped
    # as redundant — distribution_fit already captures the same information.)
    gtm_tactics: str                    # ideal launch tactics, unbiased
    brand_angle: str                    # positioning: honest/premium/mass/niche/fun
    distribution_channels: str          # which channels fit (from profile.yaml scores)
    ai_angle: str                       # "AI-formulated skincare" or "none"
    word_of_mouth_potential: str        # 1-10, how instagrammable/shareable

    # Analysis
    idea_rationale: str                 # 3-4 sentences citing signals
    competitors_india: str              # "BrandA (weakness), BrandB (weakness)"
    reference_brands_global: str        # US/Japan/EU brands this is inspired by
    wedge: str                          # specific differentiator
    lenses_fired: str                   # comma-separated lens names

    # Source tracking
    contributing_signals: str           # comma-separated signal IDs
    source_signal: str                  # collector name (e.g., "amazon_us")
    source_url: str                     # newline-separated URLs
    opportunity_type: str               # "geo_arbitrage", "complaint_cluster", "format_shift",
                                        # "unbranded_market", "rising_brand_gap", "wildcard"
    comparable_product_images: str = "" # newline-separated image URLs (global reference products)
    time_sensitive: bool = False

    # Scores (1-10)
    score_demand: int = 0
    score_launchability: int = 0
    score_capital_fit: int = 0
    score_arbitrage: int = 0            # 0 if not geo-arbitrage
    score_competition: int = 0          # higher = LESS competition (inverted)
    score_distribution: int = 0
    score_composite: float = 0.0

    # User feedback (filled in Google Sheets by user)
    status: str = "new"
    rating: str = ""
    notes: str = ""

    # Evaluation
    eval_flags: str = ""
    idea_hash: str = ""

    # Pass/fail status against hard floors. We DON'T drop failing ideas
    # anymore (the user wants to see all 5 generated and decide for themselves).
    # Instead we tag each idea with its status and the dimensions it failed on,
    # so render_markdown can show a badge + reason per idea.
    eval_status: str = "passed"          # "passed" | "rejected"
    eval_reasons: str = ""               # comma-sep: "capital_fit", "launchability", "competition", "aov"

    def to_dict(self) -> dict:
        return asdict(self)
