#!/usr/bin/env python3
"""
find_ideas.py — run the D2C Idea Finder pipeline once and write results.

Pipeline:
  collectors → pre-filter → enrichment → idea generation → dedup
  → competitor enrichment → evaluation → markdown output

Output goes to user_data/latest_ideas.md.
Reads profile from user_data/profile.yaml and EXA_API_KEY from user_data/.env.

Streams progress to stdout so the calling chat sees what's happening.

Usage:
    ./venv/bin/python scripts/find_ideas.py
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Make scripts/ importable so `from collectors...` works regardless of cwd
SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
USER_DATA = SKILL_ROOT / "user_data"
sys.path.insert(0, str(SCRIPTS_DIR))

import yaml
from dotenv import load_dotenv

# Load .env from user_data/ (the only place setup.py writes it)
load_dotenv(USER_DATA / ".env", override=True)


# ---------------------------------------------------------------------------
# Progress streaming
# ---------------------------------------------------------------------------

TOTAL_STAGES = 6
_current_stage = 0


def progress(message: str, *, done: bool = False) -> None:
    """Print a progress line, flush stdout, and update the status file.

    Two channels:
      - stdout (for logging and post-hoc inspection)
      - user_data/progress.txt (for the calling Claude to poll deterministically
        while find_ideas.py runs in background mode)
    """
    global _current_stage
    if done:
        prefix = "✓"
    else:
        _current_stage += 1
        prefix = f"[{_current_stage}/{TOTAL_STAGES}]"
    line = f"{prefix} {message}"
    print(line, flush=True)
    _write_status(line)


def substep(message: str) -> None:
    """Indented sub-progress line — for within-stage detail."""
    indented = f"    {message}"
    print(indented, flush=True)
    # Sub-steps also go to the status file so the user sees them
    _write_status(indented)


# ---------------------------------------------------------------------------
# Checkpointing — save enriched signals so a mid-pipeline crash doesn't waste
# the slow stages (collection + enrichment together take ~10-12 minutes).
# ---------------------------------------------------------------------------

CHECKPOINT_PATH = lambda: SKILL_ROOT / "user_data" / "checkpoint.json"
PROFILE_PATH_CHECK = lambda: SKILL_ROOT / "user_data" / "profile.yaml"
CHECKPOINT_MAX_AGE_HOURS = 24


def _profile_hash() -> str:
    """Return SHA256 of profile.yaml — used to invalidate checkpoints when
    the user changes categories, channels, capital, or excludes between runs.

    Without this check, a checkpoint from before a profile change would
    silently feed stale (wrong-category) signals into idea generation.
    """
    import hashlib
    try:
        return hashlib.sha256(PROFILE_PATH_CHECK().read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def _load_checkpoint() -> list | None:
    """Return enriched signals from a recent checkpoint, or None if absent/stale.

    'Stale' means any of:
      - Older than 24 hours
      - Profile has changed since the checkpoint was saved
      - Checkpoint file is corrupted
    """
    path = CHECKPOINT_PATH()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        saved_at = datetime.fromisoformat(payload["saved_at"])
        if datetime.now(timezone.utc) - saved_at > timedelta(hours=CHECKPOINT_MAX_AGE_HOURS):
            print(f"  - checkpoint older than {CHECKPOINT_MAX_AGE_HOURS}h, ignoring", flush=True)
            path.unlink()
            return None

        # Profile-change check: if profile.yaml has changed since the checkpoint
        # was saved, the enriched signals are for the wrong categories/excludes.
        saved_hash = payload.get("profile_hash", "")
        current_hash = _profile_hash()
        if saved_hash and current_hash and saved_hash != current_hash:
            print(
                "  - profile.yaml has changed since the checkpoint was saved — "
                "discarding checkpoint and re-collecting signals for the new profile",
                flush=True,
            )
            path.unlink()
            return None

        # Reconstruct NormalizedSignal objects from the serialized dicts
        from utils.signal_schema import NormalizedSignal
        signals = [NormalizedSignal(**item) for item in payload["signals"]]
        return signals
    except Exception as e:
        print(f"  - checkpoint corrupted ({e}), ignoring", flush=True)
        try:
            path.unlink()
        except Exception:
            pass
        return None


def _save_checkpoint(signals: list) -> None:
    """Persist enriched signals after stage 2 succeeds. Survives mid-pipeline crashes.

    Includes profile hash so a profile change between save and load invalidates
    the checkpoint (rather than silently resuming with stale signals).
    """
    try:
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "profile_hash": _profile_hash(),
            "signals": [dataclasses.asdict(s) for s in signals],
        }
        CHECKPOINT_PATH().write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    except Exception as e:
        # Checkpoint is best-effort — never let it break the pipeline
        print(f"  - warning: checkpoint write failed: {e}", flush=True)


def _clear_checkpoint() -> None:
    """Delete the checkpoint after a successful run so the next run starts fresh."""
    try:
        path = CHECKPOINT_PATH()
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _write_status(message: str) -> None:
    """Append the latest status line to user_data/progress.txt.

    The Claude that invoked find_ideas.py polls this file every ~30 seconds
    to surface live progress in the chat. Single source of truth — no need
    to tail the bash stdout stream, which is fragile.
    """
    from datetime import datetime, timezone
    try:
        path = USER_DATA / "progress.txt"
        path.write_text(
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}  {message}\n"
        )
    except Exception:
        # Status file is best-effort — never let it crash the pipeline
        pass


# ---------------------------------------------------------------------------
# Profile + env validation
# ---------------------------------------------------------------------------

def load_profile() -> dict:
    profile_path = USER_DATA / "profile.yaml"
    if not profile_path.exists():
        print(
            "ERROR: user_data/profile.yaml not found. Run scripts/setup.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(profile_path) as f:
        return yaml.safe_load(f)


def check_env() -> None:
    missing = []
    if not os.getenv("EXA_API_KEY"):
        missing.append("EXA_API_KEY")
    if not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        print(
            "ERROR: missing API key(s) in user_data/.env: " + ", ".join(missing) + "\n"
            "Fix it one of two ways:\n"
            "  1. Edit user_data/.env directly and fill in the empty values, or\n"
            "  2. Run: ./venv/bin/python scripts/set_key.py "
            + " ".join(f"--{k.lower().replace('_api_key', '')} <key>" for k in missing),
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Pre-filter (copied from original main.py, unchanged)
# ---------------------------------------------------------------------------

def prefilter_signals(signals: list) -> list:
    keep = []
    for s in signals:
        signal_type = s.extra.get("signal_type", "")
        if signal_type in ("rising_brand", "trend"):
            keep.append(s)
            continue
        if s.extra.get("has_comments") or s.extra.get("has_transcript"):
            keep.append(s)
            continue
        if len(s.body) < 40 and len(s.title) < 20:
            continue
        keep.append(s)
    return keep


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the D2C Idea Finder pipeline.")
    parser.add_argument(
        "--fresh", action="store_true",
        help="Ignore any existing checkpoint and re-run collection + enrichment from scratch.",
    )
    args = parser.parse_args()

    print("D2C Idea Finder — running pipeline\n", flush=True)
    start = time.time()

    # Reset progress file at the start of every run
    try:
        (USER_DATA / "progress.txt").write_text("Starting pipeline...\n")
    except Exception:
        pass

    check_env()
    profile = load_profile()

    from utils.db import (
        init_db, is_duplicate_idea, mark_idea_seen, record_run, record_idea,
        get_recent_idea_titles, get_recent_idea_concepts,
    )
    init_db()

    # ----- Checkpoint check — skip stages 1+2 if a recent run was interrupted -----
    checkpoint_signals = None
    if args.fresh:
        _clear_checkpoint()
        print("  - --fresh flag set, ignoring any existing checkpoint", flush=True)
    else:
        checkpoint_signals = _load_checkpoint()

    raw_signals: list = []
    collection_errors: list[str] = []

    if checkpoint_signals:
        normalized = checkpoint_signals
        progress(f"Resuming from checkpoint: {len(normalized)} enriched signals (skipping collection + enrichment)")
        # Bump _current_stage forward to keep the [N/6] counter accurate
        global _current_stage
        _current_stage = 2
        # Stub values for downstream logging
        raw_signals = [None] * len(normalized)
    else:
        # ----- Stage 1: Collection -----
        progress("Collecting signals from Indian marketplaces, adaptive discovery, US D2C publications, and US complaints...")

        from collectors.amazon_us_collector import AmazonUSCollector
        from collectors.rising_brands_collector import RisingBrandsCollector
        from collectors.google_trends_collector import GoogleTrendsCollector
        from collectors.exploration_collector import ExplorationCollector
        from collectors.india_marketplaces_collector import IndiaMarketplacesCollector

        # Order matters for user-facing streaming: most-important / most-direct
        # signal source first, so the user sees the India-first character of
        # the tool immediately rather than waiting for it after US sources.
        # Display labels are user-friendly versions of the internal source_name.
        for label, ctor in (
            ("Indian marketplaces", IndiaMarketplacesCollector),
            ("Adaptive discovery", ExplorationCollector),
            ("US D2C publications", RisingBrandsCollector),
            ("US Amazon + Reddit", AmazonUSCollector),
            ("Google Trends (US/IN)", GoogleTrendsCollector),
        ):
            try:
                collector = ctor(profile=profile)
                sigs = collector.fetch_safe() if hasattr(collector, "fetch_safe") else collector.fetch()
                raw_signals.extend(sigs)
                substep(f"{label}: {len(sigs)} signals")
            except Exception as e:
                substep(f"{label}: failed ({e})")
                collection_errors.append(label)

        if not raw_signals:
            print("\nNo signals collected. Check your Exa API key and network.", file=sys.stderr)
            return 1

        # ----- Stage 2: Pre-filter + enrichment -----
        filtered = prefilter_signals(raw_signals)
        progress(f"Enriching {len(filtered)} signals with Claude Sonnet...")

        try:
            from agents import signal_enrichment_agent
            normalized = signal_enrichment_agent.enrich_signals(filtered, profile)
        except Exception as e:
            substep(f"enrichment failed: {e} — falling back to raw signals")
            from utils.signal_schema import NormalizedSignal
            normalized = [
                NormalizedSignal(
                    id=s.id,
                    signal_type=s.extra.get("signal_type", "unknown"),
                    summary=s.title,
                    category_tags=[s.extra.get("category", "")],
                    consumer_tags=[],
                    evidence_strength="low",
                    recency=s.created_at,
                    key_entities=[],
                    raw_source=s.source,
                    raw_url=s.url,
                    enrichment_notes=s.body[:300] if s.body else "",
                )
                for s in filtered
            ]

        if not normalized:
            print("\nNo enriched signals. Pipeline cannot continue.", file=sys.stderr)
            return 1

        # Save checkpoint after successful enrichment. If anything fails downstream
        # (idea_agent JSON truncation, competitor synthesis hiccups), a fresh
        # invocation will skip back to here instead of re-doing 10 min of work.
        _save_checkpoint(normalized)
        substep(f"checkpoint saved ({len(normalized)} signals, will skip stages 1-2 on next run if it fails)")

    # ----- Stage 3: Idea generation -----
    progress(f"Generating ideas from {len(normalized)} signals...")

    recent_concepts = get_recent_idea_concepts(limit=100)
    recent_titles = get_recent_idea_titles(limit=100)
    dedup_titles = list(dict.fromkeys(recent_concepts + recent_titles))

    try:
        from agents import idea_agent
        raw_ideas = idea_agent.generate_ideas(
            signals=normalized,
            profile=profile,
            recent_titles=dedup_titles,
        )
    except Exception as e:
        print(f"\nIdea generation failed: {e}", file=sys.stderr)
        return 1

    if not raw_ideas:
        print("\nNo ideas generated.", file=sys.stderr)
        return 1

    # Dedup
    new_ideas = [i for i in raw_ideas if not is_duplicate_idea(i.idea_hash)]
    substep(f"{len(new_ideas)} new / {len(raw_ideas)} generated (after dedup)")

    if not new_ideas:
        print("\nAll generated ideas were duplicates. Try again or wait for new signals.", file=sys.stderr)
        return 0

    # ----- Stage 4: Competitor enrichment -----
    progress(f"Mapping Indian competitors for {len(new_ideas)} ideas...")
    try:
        from agents import competitor_enrichment_agent
        new_ideas = competitor_enrichment_agent.enrich_competitors(new_ideas, profile)
    except Exception as e:
        substep(f"competitor enrichment failed: {e} (continuing without)")

    # ----- Stage 5: Evaluation + scoring -----
    progress("Scoring and filtering ideas...")
    try:
        from agents import evaluation_agent
        scored = evaluation_agent.evaluate_ideas(ideas=new_ideas, profile=profile)
    except Exception as e:
        print(f"\nEvaluation failed: {e}", file=sys.stderr)
        return 1

    from utils.config import FILTERS
    min_composite = FILTERS["min_composite_score"]
    substep(f"{len(scored)} ideas passed filters (composite >= {min_composite})")

    if not scored:
        # Only get here if evaluation_agent returned NOTHING (very rare —
        # would mean Haiku failed to score at all). With the new "always
        # return all" behavior, even rejected ideas come back tagged.
        print("\nEvaluation returned no scored ideas — likely an API error. Check logs/run.log.", file=sys.stderr)
        return 1

    # Sort: passing ideas first (by composite desc), then rejected ideas.
    # Wildcards bubble below regular ideas within the passed group.
    # (time-sensitive tag removed — the heuristic was too coarse to be
    # meaningful; an arbitrage gap that's been open for years isn't
    # genuinely "time-sensitive" just because demand is high.)
    def _sort_key(i):
        passed = i.eval_status == "passed"
        rank_bucket = 0 if passed else 1
        wildcard_penalty = 1 if (passed and i.opportunity_type == "wildcard") else 0
        return (rank_bucket + wildcard_penalty, -i.score_composite)

    scored.sort(key=_sort_key)

    # ----- Stage 6: Render markdown + structured snapshot -----
    progress("Writing ideas to user_data/latest_ideas.md...")
    from render_markdown import render_ideas
    rendered_md = render_ideas(scored, run_started=start, profile=profile)
    out_path = USER_DATA / "latest_ideas.md"
    out_path.write_text(rendered_md)

    # Also write a structured JSON snapshot of the same ideas — useful for
    # any external tool that wants to consume the run output programmatically
    # (more parseable than the markdown).
    import json as _json
    snapshot = [
        {
            "index": i,
            "idea_id": getattr(idea, "idea_id", None),
            "title": idea.title,
            "category": idea.category,
            "tagline": idea.tagline,
            "score_composite": idea.score_composite,
            "opportunity_type": idea.opportunity_type,
            "hero_product": idea.hero_product,
        }
        for i, idea in enumerate(scored, 1)
    ]
    snapshot_json = _json.dumps(
        {"run_at": datetime.now(timezone.utc).isoformat(), "ideas": snapshot},
        indent=2,
    )
    (USER_DATA / "latest_ideas.json").write_text(snapshot_json)

    # Archive this run with a timestamp so past runs persist.
    # Format: user_data/runs/YYYY-MM-DD_HHMMSS_ideas.md (+ .json)
    # The user can browse these in their editor or via chat ("show me past runs").
    runs_dir = USER_DATA / "runs"
    runs_dir.mkdir(exist_ok=True)
    run_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    archive_md = runs_dir / f"{run_stamp}_ideas.md"
    archive_json = runs_dir / f"{run_stamp}_ideas.json"
    archive_md.write_text(rendered_md)
    archive_json.write_text(snapshot_json)
    # Archive write is silent — the user already saw the "Writing ideas" progress
    # line. The archive landing is implementation detail; surfacing it in chat
    # would just be noise.

    # Update DB
    for idea in scored:
        mark_idea_seen(idea.idea_hash, idea.title)
        record_idea(
            idea_id=idea.idea_id,
            run_at=idea.run_date,
            title=idea.title,
            score_composite=idea.score_composite,
            source=idea.source_signal,
            category_tags=idea.category,
            opportunity_type=idea.opportunity_type,
            hero_product=idea.hero_product,
        )

    record_run({
        "run_at": datetime.now(timezone.utc).isoformat(),
        "signals_collected": len(raw_signals),
        "ideas_generated": len(raw_ideas),
        "ideas_written": len(scored),
        "collectors_run": "all",
        "pipelines_run": "all",
        "duration_seconds": round(time.time() - start, 1),
        "errors": len(collection_errors),
        "notes": "",
    })

    # Pipeline completed successfully — drop the checkpoint so the next run is
    # fresh (collects new signals, doesn't reuse stale enriched ones).
    _clear_checkpoint()

    duration = round(time.time() - start, 1)
    done_msg = (
        f"Done in {duration}s. {len(raw_signals)} signals -> {len(raw_ideas)} ideas "
        f"-> {len(scored)} passed filters."
    )
    print(f"\n{done_msg}", flush=True)
    print(f"Read: {out_path}", flush=True)
    _write_status(f"DONE: {done_msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
