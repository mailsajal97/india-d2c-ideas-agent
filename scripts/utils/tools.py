"""Shared tool definitions for all Claude agents in D2C Idea Finder."""
from __future__ import annotations
import os
import requests
from exa_py import Exa

_exa_client = None


def _get_exa() -> Exa:
    global _exa_client
    if _exa_client is None:
        _exa_client = Exa(api_key=os.environ["EXA_API_KEY"])
    return _exa_client


def web_search(query: str, num_results: int = 8, days_back: int = 7,
               search_type: str | None = None) -> list[dict]:
    """Neural web search via Exa. Returns list of {title, url, snippet, published_date}.

    Use for validating D2C signals, finding Indian competitors, checking pricing,
    exploring category trends, or verifying geo-arbitrage opportunities.

    Args:
        query: Search query string.
        num_results: Number of results to return.
        days_back: How many days back to search.
        search_type: Exa search type — 'auto' (default), 'neural', 'keyword'.
    """
    from datetime import datetime, timedelta
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        exa = _get_exa()
        kwargs = dict(
            num_results=num_results,
            start_published_date=start_date,
            text={"max_characters": 800},
        )
        if search_type:
            kwargs["type"] = search_type
        results = exa.search_and_contents(query, **kwargs)
        return [
            {
                "title": r.title or "",
                "url": r.url,
                "snippet": (r.text or "")[:800],
                "published_date": r.published_date or "",
                "image": r.image or "",
            }
            for r in results.results
        ]
    except Exception as e:
        return [{"error": str(e)}]


def fetch_page(url: str, max_chars: int = 2000) -> str:
    """Fetch and return plain text content of a URL.

    Tries Exa first for clean extraction, falls back to raw requests.
    """
    try:
        exa = _get_exa()
        results = exa.get_contents([url], text={"max_characters": max_chars})
        if results.results:
            return results.results[0].text or ""
        return ""
    except Exception:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            return resp.text[:max_chars]
        except Exception as e:
            return f"[fetch failed: {e}]"


# Tool definitions for Anthropic tool_use API
TOOL_DEFINITIONS = [
    {
        "name": "web_search",
        "description": (
            "Search the web using neural search. Returns recent articles, posts, "
            "and pages matching the query. Use for validating D2C signals, finding "
            "Indian competitors, checking pricing, or exploring category trends."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num_results": {"type": "integer", "default": 8, "description": "Number of results (1-20)"},
                "days_back": {"type": "integer", "default": 7, "description": "How many days back to search"},
                "search_type": {
                    "type": "string",
                    "description": "Exa search type: 'auto' (default), 'neural', 'keyword'",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_page",
        "description": (
            "Fetch the text content of a specific URL. Use to read pricing pages, "
            "product listings, reviews, forum posts, or competitor sites."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "max_chars": {"type": "integer", "default": 2000, "description": "Max characters to return"},
            },
            "required": ["url"],
        },
    },
]


def dispatch_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return result as string."""
    if tool_name == "web_search":
        results = web_search(**tool_input)
        if not results:
            return "No results found."
        lines = []
        for r in results:
            if "error" in r:
                lines.append(f"Error: {r['error']}")
            else:
                lines.append(f"**{r['title']}** ({r['published_date']})\n{r['url']}\n{r['snippet']}\n")
        return "\n".join(lines)
    elif tool_name == "fetch_page":
        return fetch_page(**tool_input)
    return f"Unknown tool: {tool_name}"
