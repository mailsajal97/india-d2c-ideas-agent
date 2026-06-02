#!/usr/bin/env python3
"""
update_profile.py — patch user_data/profile.yaml from CLI args.

Used by Claude in chat when the user asks to change settings without re-doing
the full onboarding. Examples of what Claude maps to this script:

- "bump my capital to 10L"          → --capital 10
- "remove tight regulation"          → --remove-exclude tight_regulation
- "add fashion to categories"        → --add-category fashion_apparel
- "add paid ads as a channel"        → --add-channel paid_digital_ads
- "drop pet from my categories"      → --remove-category pet
- "show me my settings"              → --show

Validates input against the canonical key vocabulary (same as setup.py), so
typos like "food" or "personalcare" get rejected with a helpful error instead
of silently corrupting profile.yaml.

Comments in profile.yaml are NOT preserved (PyYAML dump rewrites the file).
The structure stays correct; commentary is informational and can be regenerated.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
USER_DATA = SKILL_ROOT / "user_data"
PROFILE_PATH = USER_DATA / "profile.yaml"

# Make utils/ importable when invoked from anywhere
import sys as _sys
_sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from utils.labels import humanize, humanize_list  # noqa: E402

# Canonical key vocabularies — keep in sync with scripts/setup.py
CATEGORIES = {
    "beauty", "personal_care", "food_cpg", "home", "pet", "wellness",
    "sexual_wellness", "innerwear_loungewear", "fashion_apparel", "footwear",
    "jewellery_watches", "eyewear", "accessories", "consumer_electronics",
    "baby_kids", "fitness_nutrition",
}

DISTRIBUTION_CHANNELS = {
    "paid_digital_ads", "organic_social", "influencer", "marketplace_ppc",
    "direct_email_whatsapp_sms", "seo_content", "referral_virality",
    "quick_commerce", "affiliate_coupon", "pr_press",
}

HARD_EXCLUDES = {
    "cold_chain", "complex_formulation", "tight_regulation",
    "over_10kg", "ingestible",
}

# Channel availability uses a simple binary: 2 = picked/available, 0 = not picked.
# We previously had a 3-tier system (none/comfortable/superpower) but dropped
# "superpower" because it was never asked at onboarding and added complexity
# without enough scoring signal value to justify the UX cost.
CHANNEL_SCORE_AVAILABLE = 2
CHANNEL_SCORE_UNAVAILABLE = 0


# Category description fallback (matches setup.py CATEGORIES content)
CATEGORY_DESCRIPTIONS = {
    "beauty": "makeup, skincare, haircare, fragrance",
    "personal_care": "bath/body, oral, hygiene, shaving, deodorant",
    "food_cpg": "snacks, beverages, condiments (non-functional)",
    "home": "cleaning, laundry, kitchen tools, decor, candles",
    "pet": "food, accessories, grooming",
    "wellness": "sleep, stress, recovery, topicals, ingestible wellness",
    "sexual_wellness": "intimacy products, hormonal, performance (premium)",
    "innerwear_loungewear": "innerwear, loungewear, socks, tees",
    "fashion_apparel": "women's, men's, kids' fashion-forward",
    "footwear": "sneakers, sandals, formal, athleisure",
    "jewellery_watches": "fashion jewellery, fine, smartwatches",
    "eyewear": "sunglasses, prescription frames",
    "accessories": "bags, travel gear, small leather, tech accessories",
    "consumer_electronics": "audio, wearables, smart home, tech gadgets",
    "baby_kids": "clothing, feeding, toys, care",
    "fitness_nutrition": "gear, supplements, protein, functional foods, gummies",
}


def _load_profile():
    """Load profile.yaml. Errors out if missing — user has to onboard first."""
    if not PROFILE_PATH.exists():
        print(
            f"ERROR: {PROFILE_PATH.relative_to(SKILL_ROOT)} not found. "
            "Run onboarding first.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        import yaml
    except ImportError:
        print("ERROR: pyyaml not installed. Run setup.py first.", file=sys.stderr)
        sys.exit(1)
    return yaml.safe_load(PROFILE_PATH.read_text())


def _save_profile(profile: dict) -> None:
    """Write profile.yaml back. Loses comments but preserves structure."""
    import yaml
    PROFILE_PATH.write_text(
        yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, indent=2)
    )


def cmd_capital(profile: dict, value: float) -> str:
    if value <= 0:
        raise ValueError("capital ceiling must be positive")
    old = profile.get("market", {}).get("capital_ceiling_lakhs", "?")
    profile.setdefault("market", {})["capital_ceiling_lakhs"] = value
    return f"Capital ceiling: ₹{old} lakh → ₹{value} lakh"


def cmd_add_category(profile: dict, key: str) -> str:
    if key not in CATEGORIES:
        raise ValueError(
            f"Unknown category '{key}'. Valid options: {humanize_list(sorted(CATEGORIES))}"
        )
    cats = profile.setdefault("categories", {})
    cats[key] = {
        "weight": 1.0,
        "includes": CATEGORY_DESCRIPTIONS.get(key, ""),
    }
    return f"Added category: {humanize(key)}"


def cmd_remove_category(profile: dict, key: str) -> str:
    if key not in CATEGORIES:
        raise ValueError(
            f"Unknown category '{key}'. Valid options: {humanize_list(sorted(CATEGORIES))}"
        )
    cats = profile.setdefault("categories", {})
    # Setting weight to 0 = filtered out everywhere. We don't actually delete
    # the entry because downstream code expects all 16 keys to exist.
    if key in cats and isinstance(cats[key], dict):
        cats[key]["weight"] = 0.0
    else:
        cats[key] = {"weight": 0.0, "includes": CATEGORY_DESCRIPTIONS.get(key, "")}
    return f"Removed category: {humanize(key)}"


def cmd_add_exclude(profile: dict, key: str) -> str:
    if key not in HARD_EXCLUDES:
        raise ValueError(
            f"Unknown exclude '{key}'. Valid options: {humanize_list(sorted(HARD_EXCLUDES))}"
        )
    excl = profile.setdefault("hard_excludes", {})
    preset = excl.setdefault("preset", [])
    if key not in preset:
        preset.append(key)
    return f"Added hard exclude: {humanize(key)}"


def cmd_remove_exclude(profile: dict, key: str) -> str:
    if key not in HARD_EXCLUDES:
        raise ValueError(
            f"Unknown exclude '{key}'. Valid options: {humanize_list(sorted(HARD_EXCLUDES))}"
        )
    excl = profile.setdefault("hard_excludes", {})
    preset = excl.get("preset", []) or []
    if key in preset:
        preset.remove(key)
        return f"Removed hard exclude: {humanize(key)}"
    return f"{humanize(key)} was not in your excludes"


def cmd_add_channel(profile: dict, channel: str) -> str:
    if channel not in DISTRIBUTION_CHANNELS:
        raise ValueError(
            f"Unknown channel '{channel}'. Valid options: {humanize_list(sorted(DISTRIBUTION_CHANNELS))}"
        )
    dist = profile.setdefault("distribution", {})
    dist[channel] = CHANNEL_SCORE_AVAILABLE
    return f"Added channel: {humanize(channel)}"


def cmd_remove_channel(profile: dict, channel: str) -> str:
    if channel not in DISTRIBUTION_CHANNELS:
        raise ValueError(
            f"Unknown channel '{channel}'. Valid options: {humanize_list(sorted(DISTRIBUTION_CHANNELS))}"
        )
    dist = profile.setdefault("distribution", {})
    dist[channel] = CHANNEL_SCORE_UNAVAILABLE
    return f"Removed channel: {humanize(channel)}"


def cmd_show(profile: dict) -> str:
    """Show a human-readable summary of the current profile.

    Channel display is simplified — just a flat list of picked channels.
    The earlier 'Superpowers' bucket is gone (we dropped the 3-tier system
    in favor of binary picked / not-picked).
    """
    market = profile.get("market", {})
    cats = profile.get("categories", {})
    picked = [k for k, v in cats.items() if isinstance(v, dict) and v.get("weight", 0) > 0]
    dist = profile.get("distribution", {})
    available_channels = sorted(k for k, v in dist.items() if v > 0)
    excludes = profile.get("hard_excludes", {})
    preset = excludes.get("preset", []) if isinstance(excludes, dict) else []

    return f"""Current profile:

  Capital ceiling: ₹{market.get('capital_ceiling_lakhs', '?')} lakh

  Picked categories ({len(picked)}): {humanize_list(picked) if picked else 'none'}

  Channels you can use ({len(available_channels)}): {humanize_list(available_channels) if available_channels else 'none'}

  Hard excludes: {humanize_list(preset) if preset else 'none'}
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch user_data/profile.yaml from chat-driven user requests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  --capital 10                              Set capital ceiling to ₹10 lakh
  --add-category fashion_apparel            Add a category
  --remove-category pet                     Remove a category
  --add-exclude tight_regulation            Add hard exclude
  --remove-exclude over_10kg                Remove hard exclude
  --add-channel paid_digital_ads            Add a distribution channel
  --remove-channel pr_press                 Remove a distribution channel
  --show                                    Print current profile summary

Multiple actions can be combined in one invocation.
"""
    )
    parser.add_argument("--capital", type=float, help="Set capital ceiling in lakhs")
    parser.add_argument("--add-category", action="append", default=[], help="Add a category (can repeat)")
    parser.add_argument("--remove-category", action="append", default=[], help="Remove a category (can repeat)")
    parser.add_argument("--add-exclude", action="append", default=[], help="Add a hard exclude")
    parser.add_argument("--remove-exclude", action="append", default=[], help="Remove a hard exclude")
    parser.add_argument("--add-channel", action="append", default=[], help="Add a distribution channel")
    parser.add_argument("--remove-channel", action="append", default=[], help="Remove a distribution channel")
    parser.add_argument("--show", action="store_true", help="Show current profile and exit (no changes)")
    args = parser.parse_args()

    profile = _load_profile()

    if args.show and not any([
        args.capital, args.add_category, args.remove_category,
        args.add_exclude, args.remove_exclude,
        args.add_channel, args.remove_channel,
    ]):
        print(cmd_show(profile))
        return 0

    changes = []
    try:
        if args.capital is not None:
            changes.append(cmd_capital(profile, args.capital))
        for c in args.add_category:
            changes.append(cmd_add_category(profile, c))
        for c in args.remove_category:
            changes.append(cmd_remove_category(profile, c))
        for e in args.add_exclude:
            changes.append(cmd_add_exclude(profile, e))
        for e in args.remove_exclude:
            changes.append(cmd_remove_exclude(profile, e))
        for c in args.add_channel:
            changes.append(cmd_add_channel(profile, c))
        for c in args.remove_channel:
            changes.append(cmd_remove_channel(profile, c))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not changes:
        print("No changes specified. Run with --help for options or --show to inspect.", file=sys.stderr)
        return 1

    _save_profile(profile)

    print("Profile updated:")
    for c in changes:
        print(f"  - {c}")

    if args.show:
        print()
        print(cmd_show(profile))

    return 0


if __name__ == "__main__":
    sys.exit(main())
