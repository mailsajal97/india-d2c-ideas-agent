"""
render_markdown.py — turn scored ideas into chat-friendly markdown.

Called by find_ideas.py at the end of the pipeline.
Output lands in user_data/latest_ideas.md and is what the calling Claude
shows back to the user.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


def _format_score_bar(score: float, max_score: int = 10) -> str:
    """Render an int score as a small visual bar (■■■■□□□□□□ 4/10)."""
    filled = round(score)
    return "■" * filled + "□" * (max_score - filled) + f" {score}/{max_score}"


def _badge(opportunity_type: str) -> str:
    badges = []
    if opportunity_type == "wildcard":
        badges.append("🎲 Wildcard")
    elif opportunity_type == "geo_arbitrage":
        badges.append("🌍 Geo-arbitrage")
    elif opportunity_type == "rising_brand_gap":
        badges.append("🚀 Rising brand gap")
    elif opportunity_type == "format_shift":
        badges.append("🔄 Format shift")
    elif opportunity_type == "unbranded_market":
        badges.append("🏷️ Unbranded market")
    elif opportunity_type == "complaint_cluster":
        badges.append("📣 Complaint cluster")
    return " · ".join(badges)


def _safe(value, default: str = "—") -> str:
    """Render a value as string, gracefully handling None/empty."""
    if value is None or value == "":
        return default
    return str(value)


# Humanization lives in utils/labels.py — single source of truth.
from utils.labels import humanize as _humanize


def _humanize_eval_reasons(raw: str) -> str:
    """Eval reasons are now generated in plain-language form by
    evaluation_agent (e.g. 'Needs more capital than your ceiling allows
    (fit score 3/10)'), so no transformation is needed. This is a passthrough
    that exists for backwards compatibility with checkpointed runs that
    might still have old-format reasons."""
    if not raw:
        return ""
    return raw


def render_ideas(ideas: list, run_started: float | None = None, profile: dict | None = None) -> str:
    """Render a list of scored IdeaDicts as readable markdown.

    Shows ALL ideas (passed + rejected) with pass/fail badges. At the bottom,
    if any ideas were rejected, appends a diagnostic section with
    pattern-specific recommendations.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    duration = ""
    if run_started:
        duration = f" · ran in {round(time.time() - run_started, 1)}s"

    passed = [i for i in ideas if getattr(i, "eval_status", "passed") == "passed"]
    rejected = [i for i in ideas if getattr(i, "eval_status", "passed") == "rejected"]

    summary_bits = [f"{len(passed)} passed", f"{len(rejected)} flagged"] if rejected else [f"{len(passed)} ideas"]
    summary = " · ".join(summary_bits)

    out: list[str] = [
        f"# D2C Idea Finder · {now}{duration}\n",
        f"_{summary}. Passed ideas first (sorted by score), flagged ideas below with reasons._\n",
        "---\n",
    ]

    for i, idea in enumerate(ideas, 1):
        is_rejected = getattr(idea, "eval_status", "passed") == "rejected"
        status_badge = "⚠️ Flagged" if is_rejected else "✓ Passed"
        badge = _badge(idea.opportunity_type)
        header = f"## {i}. {idea.title}  ·  [{idea.score_composite:.1f}/10]  ·  {status_badge}"
        if badge:
            header += f"  ·  {badge}"
        out.append(header)
        out.append(f"_{idea.tagline}_  ·  **Category:** {_humanize(idea.category)}\n")

        # If flagged, surface why right at the top so the founder doesn't
        # read the whole card before realising it doesn't fit their constraints.
        if is_rejected:
            raw_reasons = _safe(getattr(idea, "eval_reasons", ""), "")
            human_reasons = _humanize_eval_reasons(raw_reasons)
            out.append(f"> **Why flagged:** {human_reasons}\n")

        # Core sections — bolded labels, paragraph values
        out.append(f"**Problem**\n{_safe(idea.problem)}\n")
        out.append(f"**Target consumer**\n{_safe(idea.target_consumer)}\n")
        out.append(f"**Why now**\n{_safe(idea.why_now)}\n")
        out.append(f"**Hero product**\n{_safe(idea.hero_product)}\n")

        # Unit economics line
        out.append(
            f"**Unit economics**  ·  "
            f"AOV {_safe(idea.aov_estimate)}  ·  "
            f"Margin {_safe(idea.margin_estimate)}  ·  "
            f"Capital {_safe(idea.capital_required_estimate)}\n"
        )

        out.append(f"**Sourcing**\n{_safe(idea.sourcing_approach)}\n")

        # GTM = the ideal playbook for the product, regardless of which
        # channels the founder has. Channel-match is captured separately in
        # the distribution_fit sub-score.
        out.append(f"**Go-to-market**\n{_safe(idea.gtm_tactics)}\n")

        # Brand positioning (e.g. "honest", "premium", "playful") is a single-word
        # output that doesn't earn a full section in chat — too thin to add value.
        # Kept in the JSON schema + idea_agent prompt diversity constraint so:
        #   (a) Sonnet still spreads ideas across positioning stances (forces idea variety)
        #   (b) we can re-add display later if we ever surface it usefully (color-coded
        #       chips, brand vibe summary, etc.)

        # Only show AI angle when the idea actually has one — most ideas don't,
        # and printing "none" everywhere just adds noise.
        ai_angle = _safe(idea.ai_angle, "").strip().lower()
        if ai_angle not in ("", "—", "none", "n/a", "null", "not applicable"):
            out.append(f"**AI angle**\n{idea.ai_angle}\n")

        out.append(f"**Wedge / why it wins**\n{_safe(idea.wedge)}\n")

        # Competition
        out.append(f"**Indian competitors**\n{_safe(idea.competitors_india)}\n")
        if _safe(getattr(idea, "reference_brands_global", ""), "") not in ("", "—"):
            out.append(f"**Reference brands (global)**\n{idea.reference_brands_global}\n")

        # Sub-scores as a compact block.
        # Geo-arbitrage is suppressed when 0 (means "not a geo-arbitrage idea"
        # rather than "scored poorly on geo-arbitrage" — showing 0/10 is
        # misleading visual noise).
        out.append("**Sub-scores**")
        out.append(f"- Demand:           `{_format_score_bar(idea.score_demand)}`")
        out.append(f"- Launchability:    `{_format_score_bar(idea.score_launchability)}`")
        out.append(f"- Capital fit:      `{_format_score_bar(idea.score_capital_fit)}`")
        # Competition is inverted in the raw score (high = more competition = worse for founder).
        # Display the inverted value so all sub-scores follow "higher = better for founder".
        # raw 8/10 (high competition) → display 2/10 (low headroom for founder)
        # raw 6/10 (medium competition) → display 4/10 (some headroom)
        competition_headroom = 10 - idea.score_competition
        out.append(f"- Competition headroom: `{_format_score_bar(competition_headroom)}`")
        out.append(f"- Distribution fit: `{_format_score_bar(idea.score_distribution)}`")
        arb = getattr(idea, "score_arbitrage", None)
        if arb is not None and arb > 0:
            out.append(f"- Geo-arbitrage:    `{_format_score_bar(arb)}`")
        out.append("")

        # Source URLs if present
        urls = getattr(idea, "source_urls", None) or []
        if isinstance(urls, str):
            # In case the schema sometimes returns a comma-string
            urls = [u.strip() for u in urls.split(",") if u.strip()]
        if urls:
            out.append("**Source signals**")
            for url in urls[:5]:
                out.append(f"- {url}")
            out.append("")

        out.append("---\n")

    # Diagnostic section: only appears if any ideas were flagged. Looks at the
    # PATTERN of failures (capital_fit-heavy vs launchability-heavy vs mixed)
    # and gives the founder specific actionable suggestions, not generic
    # "widen things" advice.
    if rejected:
        diagnostic = _build_diagnostic(rejected, passed, profile)
        out.append(diagnostic)

    # Overview table — quick recap of all ideas so the founder can scan the
    # full slate after reading the detailed cards. Placed at the end so it
    # serves as a TL;DR they land on after the full read-through.
    out.append(_build_overview_table(ideas))

    out.append(
        "---\n\n"
        "_Run saved. Ask me to **show past runs**, change your settings "
        "(*\"drop pet\"*, *\"bump capital to 15L\"*, *\"add fashion\"*), "
        "or **find new d2c ideas**. Full output: `~/.claude/skills/india-d2c/user_data/latest_ideas.md`_\n"
    )

    return "\n".join(out)


def _build_overview_table(ideas: list) -> str:
    """Render a compact recap table of all ideas (passed + flagged).

    Two-line description per idea: line 1 is the tagline (elevator pitch),
    line 2 is the unit-economics snapshot (category, AOV, capital). Uses
    <br> for the in-cell line break — works in GitHub-flavoured markdown
    and the renderers Claude Code uses.
    """
    if not ideas:
        return ""

    lines = [
        "## Overview",
        "",
        "| # | Idea | Score | Status | Snapshot |",
        "|---|------|-------|--------|----------|",
    ]
    for i, idea in enumerate(ideas, 1):
        is_rejected = getattr(idea, "eval_status", "passed") == "rejected"
        status = "⚠️ Flagged" if is_rejected else "✓ Passed"
        title = (idea.title or "").replace("|", "\\|")
        tagline = (_safe(idea.tagline, "") or "").replace("|", "\\|").replace("\n", " ").strip()
        category = _humanize(idea.category) if idea.category else "—"
        aov = _safe(getattr(idea, "aov_estimate", None), "—")
        capital = _safe(getattr(idea, "capital_required_estimate", None), "—")
        snapshot_line2 = f"_{category} · AOV {aov} · Capital {capital}_"
        snapshot = f"{tagline}<br>{snapshot_line2}"
        lines.append(f"| {i} | **{title}** | {idea.score_composite:.1f}/10 | {status} | {snapshot} |")

    lines.append("")
    return "\n".join(lines)


def _build_diagnostic(rejected: list, passed: list, profile: dict | None) -> str:
    """
    Analyse the failure pattern across rejected ideas and emit specific,
    actionable recommendations. Recommendations are written for the USER —
    no internal jargon (no AOV/SKU/apply_feedback references). Anything
    snake_cased gets humanised.
    """
    if not rejected:
        return ""

    # Tally failure reasons
    fail_counts = {
        "capital_fit": 0,
        "launchability": 0,
        "competition": 0,
        "aov": 0,
    }
    for idea in rejected:
        reasons = getattr(idea, "eval_reasons", "").lower()
        if "capital_fit" in reasons:
            fail_counts["capital_fit"] += 1
        if "launchability" in reasons:
            fail_counts["launchability"] += 1
        if "competition" in reasons:
            fail_counts["competition"] += 1
        if "aov" in reasons:
            fail_counts["aov"] += 1

    n_rejected = len(rejected)
    n_total = n_rejected + len(passed)

    lines = [
        "## Why some ideas were flagged",
        "",
        f"Of {n_total} ideas generated, {n_rejected} fell below one or more hard thresholds. "
        "Here's what the pattern suggests you could change:",
        "",
    ]

    capital_ceiling = None
    picked_cats_human: list[str] = []
    if profile:
        capital_ceiling = profile.get("market", {}).get("capital_ceiling_lakhs")
        picked_cats_human = [
            _humanize(k) for k, v in profile.get("categories", {}).items()
            if isinstance(v, dict) and v.get("weight", 0) > 0
        ]

    recommendations: list[str] = []

    # Capital-heavy failures — honest about floor reality
    if fail_counts["capital_fit"] >= max(2, n_rejected // 2):
        cap_str = f"₹{capital_ceiling} lakh" if capital_ceiling else "your set ceiling"
        rec_parts = [
            f"**{fail_counts['capital_fit']} ideas needed more capital than you have available** "
            f"(your ceiling is currently {cap_str})."
        ]
        if capital_ceiling and capital_ceiling < 10:
            rec_parts.append(
                f"₹{capital_ceiling} lakh is below the realistic floor for D2C launches in India. "
                "most product categories need ₹8-15 lakh minimum to cover first-batch manufacturing, "
                "packaging, brand assets, and ~2 months of initial marketing."
            )
            rec_parts.append(
                "Best move: just say **\"bump my capital to 12 lakh\"** (or 15 lakh) in chat, "
                "and ask to find new ideas after. That single change usually unlocks 3-4 of "
                "the previously flagged ideas."
            )
        else:
            cat_str = f" ({', '.join(picked_cats_human)})" if picked_cats_human else ""
            rec_parts.append(
                f"Your picked categories{cat_str} need ₹8-15 lakh typically to launch credibly. "
                "Either bump your ceiling further, or accept that the flagged ideas above need outside funding "
                "or a co-founder with capital."
            )
        recommendations.append(" ".join(rec_parts))

    # Launchability failures — use humanized excludes
    if fail_counts["launchability"] >= max(2, n_rejected // 2):
        rec = (
            f"**{fail_counts['launchability']} ideas hit launchability concerns** (supply chain, regulation, "
            "or India-fit gaps."
        )
        excludes = []
        if profile:
            excl_dict = profile.get("hard_excludes", {})
            if isinstance(excl_dict, dict):
                excludes = [_humanize(e) for e in excl_dict.get("preset", [])]
        if excludes:
            rec += (
                f" Your hard excludes ({', '.join(excludes)}) rule out a lot of formulation paths. "
                "Consider whether any of these are absolute. For example, if you'd actually be open "
                "to basic FSSAI / cosmetics-license categories rather than blanket-excluding all regulation."
            )
        else:
            rec += (
                " This usually means contract-manufacturing availability or regulatory friction. "
                "Re-running with a different category mix may help."
            )
        recommendations.append(rec)

    # Competition density and AOV are NOT user-controllable, so we
    # deliberately don't generate recommendations for them. The flagged ideas
    # already show the failure reason in their card header; that's enough.
    # The user's actionable knobs are: capital ceiling, hard excludes,
    # picked categories. Recommendations only mention things in that set.

    if not recommendations:
        # Fallback when the only failures are non-actionable (competition,
        # AOV). Be honest with the user instead of inventing fake actions.
        non_actionable_count = fail_counts["competition"] + fail_counts["aov"]
        if non_actionable_count >= n_rejected // 2:
            recommendations.append(
                "The flagged ideas failed on dimensions you don't directly control: either the "
                "categories are crowded with well-funded incumbents, or the natural price points "
                "are too low for D2C unit economics. There isn't a simple lever to pull. "
                "**Options:** (a) pursue these flagged ideas anyway if you can find a strong "
                "differentiation angle, (b) just say **\"add [category]\"** or **\"drop [category]\"** "
                "in chat to shift the mix, or (c) accept the system's judgment and look at only "
                "the passed ideas above."
            )
        else:
            recommendations.append(
                "The flagged ideas failed across multiple dimensions in roughly equal measure. "
                "The most impactful single change you can make is bumping the capital ceiling. "
                "Just say **\"bump my capital to [X] lakh\"** in chat and I'll apply it."
            )

    for i, rec in enumerate(recommendations, 1):
        lines.append(f"{i}. {rec}")
        lines.append("")

    lines.append("---")
    lines.append("")

    return "\n".join(lines)
