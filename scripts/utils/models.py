"""
Centralized Claude model selection for the india-d2c pipeline.

Why this file exists: model IDs are infrastructure config, not user preferences.
Putting them in profile.yaml meant a model upgrade required editing the user's
own data file — easy to miss, easy to typo. Keeping them here means a future
model upgrade is one file edit, full stop.

To upgrade models (e.g. when Sonnet 4.7 ships):
  1. Update SONNET and/or HAIKU below.
  2. Run a test pipeline (./venv/bin/python scripts/find_ideas.py --fresh).
  3. If it works, ship it.

Per-stage assignments below let you target a different model to one stage
(e.g. "use Opus for evaluation") without touching the others.
"""

# --- Latest model IDs (as of May 2026) -------------------------------------

# Use bare aliases, NOT date-suffixed IDs. Date suffixes like
# "claude-sonnet-4-6-20251114" will 404 — Anthropic dropped them.
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"

# --- Per-stage model selection ---------------------------------------------

# Sonnet does the heavy reasoning (validating signals, generating ideas).
ENRICHMENT_MODEL = SONNET
GENERATION_MODEL = SONNET

# Haiku does the bulk classification + competitor lookups (cheaper, faster).
EVALUATION_MODEL = HAIKU
COMPETITOR_QUERY_MODEL = HAIKU
COMPETITOR_SYNTHESIS_MODEL = HAIKU
DEDUP_MODEL = HAIKU
