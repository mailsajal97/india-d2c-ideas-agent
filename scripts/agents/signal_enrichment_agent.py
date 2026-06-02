"""
SignalEnrichmentAgent — validates and enriches raw D2C signals using dual-lens approach.
Uses Claude Sonnet with tool access (web_search, fetch_page).

Lens A: India Demand Signal (complaints, demand, format shifts, momentum, white space, unbranded)
Lens B: Arbitrage / Rising Brand Signal (geo-arbitrage, rising brand gap)
"""
import json
import os
import time
import anthropic
from utils.signal_schema import RawSignal, NormalizedSignal
from utils.tools import TOOL_DEFINITIONS, dispatch_tool
from utils.logger import get_logger
from utils.models import ENRICHMENT_MODEL

logger = get_logger()

SYSTEM_PROMPT = """You are enriching consumer product signals for a D2C idea finder targeting India.
Your job: validate each signal's strength and enrich it with concrete evidence.

Focus on: Is this a real consumer pain? Would Indian tier-1 urban consumers pay for this?
Is there a geographic arbitrage opportunity?

Signals come in two types — detect from the signal's "extra.signal_type" field:

━━━ LENS A: INDIA DEMAND SIGNAL (default — signal_type NOT "rising_brand" or "trend") ━━━
Sources: Amazon reviews (US + India), Reddit, YouTube, Instagram, quick commerce

Use web_search and fetch_page to:
- Validate the complaint is widespread (not a one-off rant)
- Check if Indian consumers face this pain too
- Estimate willingness to pay and pain severity

Output fields:
- signal_type: "complaint_cluster" | "explicit_demand" | "format_shift" | "momentum" | "white_space" | "unbranded_market"
- pain_acuity: 1-5 (5 = hair-on-fire problem, 1 = mild annoyance)
- evidence_strength: "high" (100+ reviews/posts), "medium" (20-100), "low" (<20)
- enrichment_notes: Be SPECIFIC — not "<category> is bad" but:
  * What exact product failure? ("<specific issue> in <specific condition>, <N> Amazon/Reddit complaints mention this")
  * What specific consumer segment? ("<demographic> in <city tier> with <specific situation>")
  * Price sensitivity data? ("willing to pay ₹<range> for a solution, current options are ₹<higher> imports")
  * What format/ingredient is missing? ("<format/ingredient> exists in <market> at <price>, no Indian brand under ₹<floor>")

━━━ LENS B: ARBITRAGE / RISING BRAND SIGNAL (signal_type == "rising_brand" or "trend") ━━━
Sources: Rising US D2C brand lists, Google Trends US-vs-India

Do NOT apply the pain lens. These are market gap signals.

Use web_search to:
- Confirm the brand/format is actually growing (not just PR hype)
- Check if India has an equivalent product/brand

Output fields:
- signal_type: "geo_arbitrage" | "rising_brand_gap"
- pain_acuty: null
- gap_dimensions: list 2-3 specific things India is missing
- geo_markets: ["US", "Japan", "Europe", "Korea"] — which markets have this
- enrichment_notes: answer all four:
  (1) What the brand/product does and its hero SKU
  (2) Why it's winning (format innovation? ingredient? positioning? distribution?)
  (3) India gap analysis — is there truly no equivalent? Check Amazon.in, Nykaa, Flipkart
  (4) Which of the 16 categories: beauty, personal_care, food_cpg, home, pet,
      wellness, sexual_wellness, innerwear_loungewear, fashion_apparel, footwear,
      jewellery_watches, eyewear, accessories, consumer_electronics, baby_kids,
      fitness_nutrition

━━━ SHARED OUTPUT SCHEMA ━━━
Every signal (both lenses) must output:
{
  "id": "<original signal id>",
  "signal_type": "complaint_cluster" | "explicit_demand" | "format_shift" | "momentum" | "white_space" | "unbranded_market" | "geo_arbitrage" | "rising_brand_gap",
  "summary": "one line — specific, not generic",
  "category_tags": ["category1", "category2"],
  "consumer_tags": ["urban_women_25_35", "fitness_enthusiasts", etc.],
  "evidence_strength": "high" | "medium" | "low",
  "recency": "YYYY-MM-DD",
  "key_entities": ["brand names", "ingredients", "product names"],
  "raw_source": "<source name>",
  "raw_url": "<url>",
  "pain_acuity": 1-5 or null,
  "gap_dimensions": [],
  "enrichment_notes": "<see lens instructions above>",
  "geo_markets": []
}

Valid category_tags (use ONLY these):
beauty, personal_care, food_cpg, home, pet, wellness, sexual_wellness,
innerwear_loungewear, fashion_apparel, footwear, jewellery_watches, eyewear,
accessories, consumer_electronics, baby_kids, fitness_nutrition

Be selective: ignore venting without product need, problems already well-solved by popular Indian brands,
and signals with no path to a ₹800+ AOV D2C product."""


BATCH_SIZE = 20  # signals per enrichment call


def enrich_signals(raw_signals: list[RawSignal], profile: dict, max_tool_rounds: int = 6) -> list[NormalizedSignal]:
    """
    Run the signal enrichment agent on raw signals, in batches.
    Returns enriched, normalized signals ready for idea generation.
    """
    if not raw_signals:
        return []

    all_normalized: list[NormalizedSignal] = []
    batches = [raw_signals[i:i + BATCH_SIZE] for i in range(0, len(raw_signals), BATCH_SIZE)]
    logger.info(f"[enrichment] processing {len(raw_signals)} signals in {len(batches)} batches of {BATCH_SIZE}")

    for batch_idx, batch in enumerate(batches):
        if batch_idx > 0:
            time.sleep(15)  # pause between batches to stay within rate limits
        logger.info(f"[enrichment] batch {batch_idx + 1}/{len(batches)} ({len(batch)} signals)")
        # Also emit to stdout in the 4-space substep format so the running
        # chat sees per-batch progress (logger output goes to stderr, which
        # Monitor doesn't watch).
        _emit_progress(f"enrichment: batch {batch_idx + 1}/{len(batches)} ({len(batch)} signals)")
        normalized = _run_batch(batch, profile, max_tool_rounds)
        all_normalized.extend(normalized)
        _emit_progress(f"enrichment: batch {batch_idx + 1} produced {len(normalized)} normalized signals")

    _emit_progress(f"enrichment complete: {len(all_normalized)} normalized signals total")
    return all_normalized


def _emit_progress(message: str) -> None:
    """Print a 4-space-indented progress line to stdout AND append to
    user_data/progress.txt so the chat's Monitor can relay it. Logger
    output goes to stderr — Monitor doesn't watch stderr."""
    import sys
    from pathlib import Path
    indented = f"    {message}"
    print(indented, flush=True)
    try:
        progress_path = Path(__file__).resolve().parent.parent.parent / "user_data" / "progress.txt"
        progress_path.write_text(indented + "\n")
    except Exception:
        pass


def _run_batch(raw_signals: list[RawSignal], profile: dict, max_tool_rounds: int = 6) -> list[NormalizedSignal]:
    """Run enrichment on a single batch of signals."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = ENRICHMENT_MODEL

    # Format raw signals for the prompt
    signals_text = json.dumps([
        {
            "id": s.id,
            "source": s.source,
            "title": s.title,
            "body": s.body[:500],
            "url": s.url,
            "created_at": s.created_at,
            "extra": s.extra,
        }
        for s in raw_signals
    ], indent=2)

    user_prompt = f"""Here are {len(raw_signals)} raw D2C consumer signals from Amazon reviews, Reddit, YouTube, Instagram, and rising brand trackers.

Validate and enrich the most promising ones (aim for 8-15 high-quality normalized signals from this batch).

TOOL USE LIMIT: Use at most 4 searches total. Be selective — only search for the 3-4 signals
you are most uncertain about. For the rest, use your existing knowledge to assess them.
Do not search for every signal. After your searches, output the JSON immediately.

Focus on signals that represent:
- Real, recurring consumer pain with willingness to pay (Lens A)
- Geographic arbitrage: proven abroad, absent in India (Lens B)
- Format/ingredient shifts crossing the chasm into India
- Unbranded markets where no D2C brand has claimed the space

IMPORTANT: Rising brand / trend signals (Lens B) should have a LOWER bar for inclusion
than complaint signals — they represent validated market opportunities. Include them unless clearly
irrelevant to Indian consumers.

Raw signals:
{signals_text}

After your analysis (max 4 searches), output ONLY a JSON array of NormalizedSignal objects.
Start your final response with ```json and end with ```"""

    messages = [{"role": "user", "content": user_prompt}]

    for round_num in range(max_tool_rounds):
        # Retry up to 3 times on transient overload errors
        for attempt in range(3):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=8000,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )
                break
            except Exception as e:
                if "overloaded" in str(e).lower() and attempt < 2:
                    wait = 30 * (attempt + 1)
                    logger.warning(f"[enrichment] API overloaded — retrying in {wait}s (attempt {attempt + 1}/3)")
                    time.sleep(wait)
                else:
                    raise

        # Handle tool use
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if tool_uses:
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tool_use in tool_uses:
                logger.info(f"[enrichment] tool: {tool_use.name}({list(tool_use.input.keys())})")
                result = dispatch_tool(tool_use.name, tool_use.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result[:3000],
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Extract final JSON response
        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        break
    else:
        # Exhausted rounds — force a final output call with no tools
        logger.warning("[enrichment] max tool rounds reached — forcing final output")
        messages.append({"role": "user", "content": (
            "You have reached the tool use limit. "
            "Output your final JSON array of NormalizedSignal objects now, "
            "based on what you have already found. Start with ```json."
        )})
        final = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        text = "".join(b.text for b in final.content if hasattr(b, "text"))

    return _parse_normalized_signals(text, raw_signals)


def _parse_normalized_signals(text: str, raw_signals: list[RawSignal]) -> list[NormalizedSignal]:
    """Parse JSON output from the agent into NormalizedSignal objects."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]

    try:
        data = json.loads(text.strip())
    except Exception as e:
        logger.warning(f"[enrichment] failed to parse JSON: {e}")
        return []

    # Build lookup for raw signal URLs
    raw_by_id = {s.id: s for s in raw_signals}

    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        signal_id = str(item.get("id", ""))
        raw = raw_by_id.get(signal_id)
        results.append(NormalizedSignal(
            id=signal_id or item.get("raw_url", ""),
            signal_type=item.get("signal_type", "complaint_cluster"),
            summary=item.get("summary", ""),
            category_tags=item.get("category_tags", []),
            consumer_tags=item.get("consumer_tags", []),
            evidence_strength=item.get("evidence_strength", "medium"),
            recency=item.get("recency", ""),
            key_entities=item.get("key_entities", []),
            raw_source=item.get("raw_source", raw.source if raw else ""),
            raw_url=item.get("raw_url", raw.url if raw else ""),
            pain_acuity=item.get("pain_acuity"),
            gap_dimensions=item.get("gap_dimensions", []),
            enrichment_notes=item.get("enrichment_notes", ""),
            geo_markets=item.get("geo_markets", []),
        ))

    logger.info(f"[enrichment] produced {len(results)} normalized signals")
    return results
