"""
Semantic dedup — uses Claude Haiku to catch rephrased duplicate ideas
that slip past title/hash matching.
"""
import json
import os
import anthropic
from utils.signal_schema import IdeaDict
from utils.logger import get_logger
from utils.models import DEDUP_MODEL

logger = get_logger()


def semantic_dedup(new_ideas: list[IdeaDict], existing_concepts: list[str]) -> list[IdeaDict]:
    """Filter out new ideas that are semantically identical to existing ones.

    Uses a single Haiku call to check all new ideas against the existing concept list.
    Returns only genuinely novel ideas.
    """
    if not existing_concepts or not new_ideas:
        return new_ideas

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    new_list = [
        {"id": idea.idea_id, "concept": f"{idea.category}: {idea.title} — {idea.hero_product}"}
        for idea in new_ideas
    ]

    prompt = f"""You are a duplicate detector for D2C product ideas.

EXISTING IDEAS (already generated in previous runs):
{chr(10).join(f"- {c}" for c in existing_concepts[:80])}

NEW IDEAS (just generated):
{json.dumps(new_list, indent=2)}

For each new idea, determine if it is essentially the SAME core product concept as any existing idea,
just with a different brand name or slightly rephrased. Examples of duplicates:
- Two brand names for the same hero ingredient + format + price tier → SAME
- Same ingredient but different format (liquid vs bar, gummy vs powder) → DIFFERENT
- Same format but different target consumer (women's vs men's variant) → DIFFERENT
- Same idea repositioned for a different category use case → DIFFERENT

Return ONLY a JSON array of idea IDs that are DUPLICATES (should be removed):
["id1", "id2"]

If no duplicates, return an empty array: []"""

    try:
        response = client.messages.create(
            model=DEDUP_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        duplicate_ids = set(json.loads(text.strip()))
        if duplicate_ids:
            removed = [i.title for i in new_ideas if i.idea_id in duplicate_ids]
            logger.info(f"[semantic_dedup] removing {len(removed)} duplicates: {removed}")

        return [i for i in new_ideas if i.idea_id not in duplicate_ids]

    except Exception as e:
        logger.warning(f"[semantic_dedup] failed: {e} — keeping all ideas")
        return new_ideas
