"""
Human-readable labels for internal canonical keys.

Single source of truth for converting snake_case internal identifiers
(used in profile.yaml, IdeaDict fields, evaluation reasons) into
user-facing display strings. Imported by render_markdown.py and
update_profile.py.

Add new entries here, not anywhere else.
"""
from __future__ import annotations


HUMAN_LABELS: dict[str, str] = {
    # Categories
    "beauty": "Beauty",
    "personal_care": "Personal Care",
    "food_cpg": "Food & CPG",
    "home": "Home",
    "pet": "Pet",
    "wellness": "Wellness",
    "sexual_wellness": "Sexual Wellness",
    "innerwear_loungewear": "Innerwear & Loungewear",
    "fashion_apparel": "Fashion & Apparel",
    "footwear": "Footwear",
    "jewellery_watches": "Jewellery & Watches",
    "eyewear": "Eyewear",
    "accessories": "Accessories",
    "consumer_electronics": "Consumer Electronics",
    "baby_kids": "Baby & Kids",
    "fitness_nutrition": "Fitness & Nutrition",

    # Hard excludes
    "cold_chain": "cold chain / perishables",
    "complex_formulation": "complex formulation",
    "tight_regulation": "heavy regulation (FSSAI nutra, AYUSH, etc.)",
    "over_10kg": "items over 10 kg",
    "ingestible": "ingestible products",

    # Distribution channels
    "paid_digital_ads": "Paid digital ads",
    "organic_social": "Organic social content",
    "influencer": "Influencer marketing",
    "marketplace_ppc": "Marketplace PPC",
    "direct_email_whatsapp_sms": "Email + WhatsApp + SMS",
    "seo_content": "SEO / content marketing",
    "referral_virality": "Referral / virality",
    "quick_commerce": "Quick commerce",
    "affiliate_coupon": "Affiliates / coupon sites",
    "pr_press": "PR / press",

    # Evaluation dimensions (used in rejection reasons)
    "capital_fit": "capital fit",
    "launchability": "launchability",
    "competition": "competition density",
    "competition_too_high": "competition density",
    "aov_below_floor": "price too low for D2C economics",
    "composite": "overall composite score",

    # Tiers
    "comfortable": "comfortable",
    "superpower": "superpower",
    "none": "not used",
}


def humanize(key: str) -> str:
    """Convert a snake_case canonical key to a user-facing label.

    Falls back to a generic "snake_case → Title Case" conversion for any
    key not explicitly mapped, so new keys we forget to add don't render
    as raw identifiers.
    """
    if not key:
        return ""
    key = key.strip()
    if key in HUMAN_LABELS:
        return HUMAN_LABELS[key]
    return key.replace("_", " ").title()


def humanize_list(keys: list[str], joiner: str = ", ") -> str:
    """Humanize a list of keys and join with the given separator."""
    return joiner.join(humanize(k) for k in keys)
