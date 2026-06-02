"""
setup.py — first-time setup for the D2C Idea Finder skill.

Called by Claude after collecting the user's answers via the SKILL.md
onboarding conversation. Takes a JSON config blob, validates it, sets up
the Python environment, and writes all user_data/ files.

Usage:
    python3 scripts/setup.py --config '<json>'

Uses only Python stdlib (no third-party deps) so it can bootstrap from any
Python 3.10+ install.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SKILL_ROOT = Path(__file__).resolve().parent.parent
USER_DATA = SKILL_ROOT / "user_data"
VENV_DIR = SKILL_ROOT / "venv"
REQUIREMENTS = SKILL_ROOT / "requirements.txt"

# ---------------------------------------------------------------------------
# Canonical option vocabularies — kept in lockstep with SKILL.md
# ---------------------------------------------------------------------------

CATEGORIES = {
    "beauty": ("Beauty", "makeup, skincare, haircare, fragrance"),
    "personal_care": ("Personal Care", "bath/body, oral, hygiene, shaving, deodorant"),
    "food_cpg": ("Food & CPG", "snacks, beverages, condiments (non-functional)"),
    "home": ("Home", "cleaning, laundry, kitchen tools, decor, candles"),
    "pet": ("Pet", "food, accessories, grooming"),
    "wellness": ("Wellness", "sleep, stress, recovery, topicals, ingestible wellness"),
    "sexual_wellness": ("Sexual Wellness", "intimacy products, hormonal, performance (premium)"),
    "innerwear_loungewear": ("Innerwear & Loungewear", "innerwear, loungewear, socks, tees"),
    "fashion_apparel": ("Fashion & Apparel", "women's, men's, kids' fashion-forward"),
    "footwear": ("Footwear", "sneakers, sandals, formal, athleisure"),
    "jewellery_watches": ("Jewellery & Watches", "fashion jewellery, fine, smartwatches"),
    "eyewear": ("Eyewear", "sunglasses, prescription frames"),
    "accessories": ("Accessories", "bags, travel gear, small leather, tech accessories"),
    "consumer_electronics": ("Consumer Electronics", "audio, wearables, smart home, tech gadgets"),
    "baby_kids": ("Baby & Kids", "clothing, feeding, toys, care"),
    "fitness_nutrition": ("Fitness & Nutrition", "gear, supplements, protein, functional foods, gummies"),
}

DISTRIBUTION_CHANNELS = {
    "paid_digital_ads",          # Meta, Google, YouTube — bundled, similar skill
    "organic_social",            # Organic IG, YouTube, Reels
    "influencer",                # Nano to celebrity — one bucket
    "marketplace_ppc",           # Amazon, Flipkart, Nykaa sponsored
    "direct_email_whatsapp_sms", # Owned direct channels (Klaviyo, WA Business, SMS)
    "seo_content",               # Blog, organic search, Reddit/Quora
    "referral_virality",         # Refer-a-friend, viral mechanics, WoM
    "quick_commerce",            # Blinkit, Zepto, Instamart, BBNow paid placement
    "affiliate_coupon",          # Cuelinks, GrabOn, performance partners
    "pr_press",                  # Earned media, press placements
    # NOTE: offline_retail removed. D2C means direct-to-consumer — online
    # first. Offline retail (kirana, modern trade) is a post-D2C-scale move,
    # not a day-1 channel for a bootstrap founder.
}

HARD_EXCLUDE_OPTIONS = {
    "cold_chain", "complex_formulation", "tight_regulation", "over_10kg", "ingestible",
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(config: dict) -> None:
    """Raise ValueError if config is malformed.

    Note: anthropic_api_key and exa_api_key are OPTIONAL here. If absent,
    setup writes an empty placeholder in .env and the user fills it in
    later (either via paste-and-set_key.py or by editing .env directly).
    """
    required = {
        "categories", "distribution_channels",
        "capital_ceiling_lakh",
    }
    missing = required - set(config.keys())
    if missing:
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    if not config["categories"]:
        raise ValueError("At least one category must be picked.")

    bad_cats = set(config["categories"]) - set(CATEGORIES.keys())
    if bad_cats:
        raise ValueError(f"Unknown category keys: {sorted(bad_cats)}. Valid: {sorted(CATEGORIES.keys())}")

    bad_channels = set(config["distribution_channels"]) - DISTRIBUTION_CHANNELS
    if bad_channels:
        raise ValueError(f"Unknown distribution channels: {sorted(bad_channels)}")

    bad_supers = set(config.get("distribution_superpowers", [])) - set(config["distribution_channels"])
    if bad_supers:
        raise ValueError(f"Distribution superpowers must be subset of picked channels. Offenders: {sorted(bad_supers)}")

    bad_excludes = set(config.get("hard_excludes", [])) - HARD_EXCLUDE_OPTIONS
    if bad_excludes:
        raise ValueError(f"Unknown hard exclude keys: {sorted(bad_excludes)}. Valid: {sorted(HARD_EXCLUDE_OPTIONS)}")

    if not isinstance(config["capital_ceiling_lakh"], (int, float)) or config["capital_ceiling_lakh"] <= 0:
        raise ValueError("capital_ceiling_lakh must be a positive number.")

    # Keys are optional. If provided, validate format. If absent, .env gets
    # written with empty placeholders and the user fills in later.
    exa_key = config.get("exa_api_key", "")
    if exa_key and not isinstance(exa_key, str):
        raise ValueError("exa_api_key must be a string (or omitted entirely).")

    anthropic_key = config.get("anthropic_api_key", "")
    if anthropic_key:
        if not isinstance(anthropic_key, str):
            raise ValueError("anthropic_api_key must be a string (or omitted entirely).")
        if not anthropic_key.startswith("sk-ant-"):
            raise ValueError(
                "anthropic_api_key looks malformed — Anthropic keys start with 'sk-ant-'. "
                "Get a key at https://console.anthropic.com."
            )


# ---------------------------------------------------------------------------
# Environment setup (venv + dependencies)
# ---------------------------------------------------------------------------

def ensure_venv() -> Path:
    """Create venv and install requirements. Returns path to venv python."""
    venv_python = VENV_DIR / "bin" / "python"
    if venv_python.exists():
        print("  - venv already exists, skipping creation")
    else:
        print("  - creating Python venv (using system python3)")
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True, capture_output=True,
        )

    print("  - installing dependencies (this takes 30-90 seconds)")
    # Prefer uv if available — much faster
    if shutil.which("uv"):
        subprocess.run(
            ["uv", "pip", "install", "--python", str(venv_python), "-r", str(REQUIREMENTS)],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)],
            check=True, capture_output=True,
        )
    return venv_python


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------

def write_env(exa_key: str, anthropic_key: str) -> None:
    """Write user_data/.env. Empty values are fine — user fills them in later
    via scripts/set_key.py or by editing the file directly."""
    env_path = USER_DATA / ".env"
    env_path.write_text(
        "# Auto-generated by setup.py — do not commit.\n"
        "# Fill in the values below if they are empty.\n"
        "# Either edit this file directly, or run:\n"
        "#   ./venv/bin/python scripts/set_key.py --exa <key> --anthropic <key>\n"
        f"EXA_API_KEY={exa_key}\n"
        f"ANTHROPIC_API_KEY={anthropic_key}\n"
    )
    os.chmod(env_path, 0o600)  # owner-readable only
    status = []
    if exa_key:
        status.append("Exa filled")
    else:
        status.append("Exa empty")
    if anthropic_key:
        status.append("Anthropic filled")
    else:
        status.append("Anthropic empty")
    print(f"  - wrote {env_path.relative_to(SKILL_ROOT)} (chmod 600, {', '.join(status)})")


def write_profile(config: dict) -> None:
    """Write user_data/profile.yaml from config."""
    # Build category map: picked = 1.0, unpicked = 0 (filtered).
    # (Boost mechanism was removed — categories are either in or out.)
    picked = set(config["categories"])
    cat_map = {}
    for key, (label, includes) in CATEGORIES.items():
        weight = 1.0 if key in picked else 0.0
        cat_map[key] = {"weight": weight, "includes": includes}

    # Build distribution score map: superpower = 3, picked = 2, unpicked = 0
    picked_channels = set(config["distribution_channels"])
    super_channels = set(config.get("distribution_superpowers", []))
    dist_scores = {}
    for ch in sorted(DISTRIBUTION_CHANNELS):
        if ch in super_channels:
            dist_scores[ch] = 3
        elif ch in picked_channels:
            dist_scores[ch] = 2
        else:
            dist_scores[ch] = 0

    profile = _profile_yaml(
        capital_ceiling_lakh=config["capital_ceiling_lakh"],
        categories=cat_map,
        distribution=dist_scores,
        hard_excludes=config.get("hard_excludes", []),
        hard_excludes_custom=config.get("hard_excludes_custom", []),
    )
    profile_path = USER_DATA / "profile.yaml"
    profile_path.write_text(profile)
    print(f"  - wrote {profile_path.relative_to(SKILL_ROOT)}")


# NOTE: setup.py does NOT initialize state.db.
# scripts/utils/db.init_db() is the single source of truth for the schema, and
# find_ideas.py calls it at startup. Putting a duplicate CREATE TABLE here would
# silently collide if the schemas drift apart (which is exactly what happened in
# an earlier version of this skill).


# ---------------------------------------------------------------------------
# profile.yaml template (raw string — kept simple, no jinja)
# ---------------------------------------------------------------------------

def _profile_yaml(*, capital_ceiling_lakh, categories, distribution, hard_excludes, hard_excludes_custom):
    # Manual YAML serialisation — avoids depending on PyYAML during setup
    # (PyYAML is one of the things setup itself installs into the venv).

    def emit_dict(d, indent=2):
        lines = []
        prefix = " " * indent
        for k, v in d.items():
            if isinstance(v, dict):
                lines.append(f"{prefix}{k}:")
                for ik, iv in v.items():
                    if isinstance(iv, str):
                        lines.append(f"{prefix}  {ik}: \"{iv}\"")
                    else:
                        lines.append(f"{prefix}  {ik}: {iv}")
            elif isinstance(v, list):
                if not v:
                    lines.append(f"{prefix}{k}: []")
                else:
                    lines.append(f"{prefix}{k}:")
                    for item in v:
                        lines.append(f"{prefix}  - \"{item}\"")
            elif isinstance(v, str):
                lines.append(f"{prefix}{k}: \"{v}\"")
            else:
                lines.append(f"{prefix}{k}: {v}")
        return "\n".join(lines)

    return f"""# D2C Idea Finder — profile.yaml (auto-generated by setup.py)
#
# This file controls how the pipeline scores and filters ideas for YOU.
# It contains:
#   1. User inputs captured during onboarding (capital, categories, channels,
#      excludes)
#   2. Scoring policy (which dimensions matter, what the hard floors are)
#   3. Claude model selection (power-user tuning)
#
# Everything in here is actively read by the pipeline. To change settings
# without re-onboarding, use scripts/update_profile.py — see SKILL.md.

# ---------------------------------------------------------------------------
# USER INPUTS (from onboarding) — the only things in this file
# ---------------------------------------------------------------------------
# Internal pipeline config (scoring weights, filter thresholds, AOV floor,
# model IDs, generation knobs) lives in scripts/utils/config.py and
# scripts/utils/models.py — NOT here. That keeps this file purely about
# YOUR preferences, so update_profile.py --show is the complete picture.

market:
  capital_ceiling_lakhs: {capital_ceiling_lakh}   # Your hard ceiling on first-SKU launch budget

categories:
{emit_dict(categories, indent=2)}

distribution:
{emit_dict(distribution, indent=2)}

hard_excludes:
  preset:
{chr(10).join(f"    - {e}" for e in hard_excludes) if hard_excludes else "    []"}
  custom:
{chr(10).join('    - "' + e + '"' for e in hard_excludes_custom) if hard_excludes_custom else "    []"}
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="D2C Idea Finder — first-time setup")
    parser.add_argument("--config", required=True, help="JSON config blob")
    args = parser.parse_args()

    try:
        config = json.loads(args.config)
    except json.JSONDecodeError as e:
        print(f"ERROR: --config must be valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        validate(config)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    USER_DATA.mkdir(exist_ok=True)

    print("D2C Idea Finder — setup\n")

    print("Step 1: Python environment")
    ensure_venv()

    print("\nStep 2: Writing user files")
    write_env(config.get("exa_api_key", ""), config.get("anthropic_api_key", ""))
    write_profile(config)
    # state.db gets created lazily by find_ideas.py via utils.db.init_db().
    # Don't duplicate the schema here — keeping CREATE TABLE in one place avoids
    # silent schema drift.

    print(f"""
Setup complete.

Picked {len(config['categories'])} categories.
Picked {len(config['distribution_channels'])} distribution channels ({len(config.get('distribution_superpowers', []))} superpowers).
Capital ceiling: ₹{config['capital_ceiling_lakh']} lakh.
Hard excludes: {len(config.get('hard_excludes', [])) + len(config.get('hard_excludes_custom', []))} total.
""")

    # Tell the user (or the calling Claude) what to do about missing keys
    missing_keys = []
    if not config.get("exa_api_key"):
        missing_keys.append("EXA_API_KEY")
    if not config.get("anthropic_api_key"):
        missing_keys.append("ANTHROPIC_API_KEY")

    if missing_keys:
        print(
            f"Next: add your API key(s) — {', '.join(missing_keys)} — to user_data/.env.\n"
            f"Either edit the file directly, or run:\n"
            f"  ./venv/bin/python scripts/set_key.py --exa <key> --anthropic <key>\n"
            f"Then: ./venv/bin/python scripts/find_ideas.py"
        )
    else:
        print("Next: run `./venv/bin/python scripts/find_ideas.py` to get your first batch of ideas.")


if __name__ == "__main__":
    main()
