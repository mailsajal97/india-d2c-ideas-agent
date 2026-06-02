"""SQLite helpers for run history, signal dedup, idea dedup, and source quality."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "user_data" / "state.db"

# Common English stop words to strip before hashing
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "its", "this", "that", "as",
    "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may", "might",
    "no", "not", "so", "if", "than", "too", "very", "just", "about",
})


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                signals_collected INTEGER DEFAULT 0,
                ideas_generated INTEGER DEFAULT 0,
                ideas_written INTEGER DEFAULT 0,
                duration_seconds REAL DEFAULT 0,
                errors INTEGER DEFAULT 0,
                notes TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS seen_ids (
                source TEXT NOT NULL,
                item_id TEXT NOT NULL,
                seen_at TEXT NOT NULL,
                PRIMARY KEY (source, item_id)
            );

            CREATE TABLE IF NOT EXISTS idea_hashes (
                hash TEXT PRIMARY KEY,
                title TEXT,
                added_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ideas (
                idea_id TEXT PRIMARY KEY,
                run_at TEXT,
                title TEXT,
                hero_product TEXT DEFAULT '',
                score_composite REAL,
                source TEXT,
                category_tags TEXT,
                opportunity_type TEXT
            );

            CREATE TABLE IF NOT EXISTS source_quality (
                source TEXT PRIMARY KEY,
                ideas_generated INTEGER DEFAULT 0,
                ratings_sum INTEGER DEFAULT 0,
                ratings_count INTEGER DEFAULT 0,
                last_4_ratings TEXT DEFAULT '[]',
                updated_at TEXT
            );
        """)
        # Migrations for existing DBs
        try:
            conn.execute("ALTER TABLE ideas ADD COLUMN hero_product TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists


# ---------------------------------------------------------------------------
# Signal dedup
# ---------------------------------------------------------------------------

def is_seen(source: str, item_id: str) -> bool:
    """Check if a signal has already been processed."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_ids WHERE source=? AND item_id=?", (source, str(item_id))
        ).fetchone()
        return row is not None


def mark_seen(source: str, item_id: str):
    """Mark a signal as processed so it won't be collected again."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_ids (source, item_id, seen_at) VALUES (?,?,?)",
            (source, str(item_id), datetime.utcnow().isoformat()),
        )


# ---------------------------------------------------------------------------
# Idea dedup
# ---------------------------------------------------------------------------

def compute_idea_hash(category: str, hero_concept: str) -> str:
    """Compute a stable hash for dedup. Normalize: lowercase, remove stop words, sort tokens, SHA256."""
    raw = f"{category} {hero_concept}".lower()
    # Strip punctuation and extra whitespace
    raw = re.sub(r"[^\w\s]", " ", raw)
    tokens = raw.split()
    # Remove stop words, sort remaining tokens for order-independence
    tokens = sorted(t for t in tokens if t not in _STOP_WORDS)
    normalized = " ".join(tokens)
    return hashlib.sha256(normalized.encode()).hexdigest()


def is_duplicate_idea(idea_hash: str) -> bool:
    """Check if an idea with this hash has already been generated."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM idea_hashes WHERE hash=?", (idea_hash,)
        ).fetchone() is not None


def mark_idea_seen(idea_hash: str, title: str):
    """Record an idea hash to prevent future duplicates."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO idea_hashes (hash, title, added_at) VALUES (?,?,?)",
            (idea_hash, title, datetime.utcnow().isoformat()),
        )


# ---------------------------------------------------------------------------
# Run tracking
# ---------------------------------------------------------------------------

def record_run(run_data: dict) -> int:
    """Insert a run record. Returns the new row ID."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO runs (run_at, signals_collected, ideas_generated,
               ideas_written, duration_seconds, errors, notes)
               VALUES (:run_at, :signals_collected, :ideas_generated,
               :ideas_written, :duration_seconds, :errors, :notes)""",
            run_data,
        )
        return cur.lastrowid


def get_run_count() -> int:
    """Return total number of completed runs (used for collector stagger logic)."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) as n FROM runs").fetchone()
        return row["n"] if row else 0


# ---------------------------------------------------------------------------
# Idea history (for learning + dedup context)
# ---------------------------------------------------------------------------

def record_idea(idea_id: str, run_at: str, title: str, score_composite: float,
                source: str, category_tags: str = "", opportunity_type: str = "",
                hero_product: str = ""):
    """Record idea metadata for learning and coverage analysis."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO ideas
               (idea_id, run_at, title, hero_product, score_composite, source, category_tags, opportunity_type)
               VALUES (?,?,?,?,?,?,?,?)""",
            (idea_id, run_at, title, hero_product, score_composite, source, category_tags, opportunity_type),
        )


def get_recent_idea_titles(limit: int = 100) -> list[str]:
    """Return titles of recently generated ideas for dedup in IdeaAgent prompt."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title FROM ideas ORDER BY run_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [r["title"] for r in rows if r["title"]]


def get_recent_idea_concepts(limit: int = 100) -> list[str]:
    """Return 'category: title (hero_product)' strings for semantic dedup.

    Gives Claude richer context than just titles to avoid rephrased duplicates.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category_tags, title, hero_product FROM ideas ORDER BY run_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [
            f"{r['category_tags']}: {r['title']}" + (f" ({r['hero_product']})" if r['hero_product'] else "")
            for r in rows if r["title"]
        ]


def get_recent_ideas(limit: int = 30) -> list[dict]:
    """Return recently generated ideas with scores for LearningAgent context."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT idea_id, run_at, title, score_composite,
               source, category_tags, opportunity_type
               FROM ideas ORDER BY run_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_category_coverage(last_n_runs: int = 5) -> dict:
    """Return per-category idea counts and avg scores over the last N runs.

    Used by LearningAgent to see which categories are over/under-explored.
    Returns dict of {category: {count, avg_score}}.
    """
    with get_conn() as conn:
        # Get the cutoff run_at timestamp (Nth most recent run)
        cutoff_row = conn.execute(
            "SELECT run_at FROM runs ORDER BY id DESC LIMIT 1 OFFSET ?",
            (last_n_runs - 1,)
        ).fetchone()

        if not cutoff_row:
            return {}

        cutoff = cutoff_row["run_at"]

        rows = conn.execute(
            """SELECT category_tags, COUNT(*) as count,
                      ROUND(AVG(score_composite), 1) as avg_score
               FROM ideas
               WHERE run_at >= ? AND category_tags IS NOT NULL AND category_tags != ''
               GROUP BY category_tags
               ORDER BY count DESC""",
            (cutoff,)
        ).fetchall()

        # Flatten — category_tags may be comma-separated
        cat_counts: dict[str, dict] = {}
        for row in rows:
            tags = row["category_tags"].split(", ")
            for tag in tags:
                tag = tag.strip()
                if not tag:
                    continue
                if tag not in cat_counts:
                    cat_counts[tag] = {"count": 0, "score_sum": 0.0, "score_n": 0}
                cat_counts[tag]["count"] += row["count"]
                if row["avg_score"]:
                    cat_counts[tag]["score_sum"] += row["avg_score"] * row["count"]
                    cat_counts[tag]["score_n"] += row["count"]

        result = {}
        for cat, d in cat_counts.items():
            avg = round(d["score_sum"] / d["score_n"], 1) if d["score_n"] > 0 else None
            result[cat] = {"count": d["count"], "avg_score": avg}

        return result


# ---------------------------------------------------------------------------
# Source quality tracking
# ---------------------------------------------------------------------------

def update_source_quality(source: str, rating: int):
    """Update rolling source quality after a user rates an idea from this source."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM source_quality WHERE source=?", (source,)
        ).fetchone()
        if row:
            last_4 = json.loads(row["last_4_ratings"])
            last_4.append(rating)
            last_4 = last_4[-4:]
            conn.execute(
                """UPDATE source_quality SET
                   ratings_sum=ratings_sum+?, ratings_count=ratings_count+1,
                   last_4_ratings=?, updated_at=?
                   WHERE source=?""",
                (rating, json.dumps(last_4), datetime.utcnow().isoformat(), source),
            )
        else:
            conn.execute(
                """INSERT INTO source_quality
                   (source, ideas_generated, ratings_sum, ratings_count, last_4_ratings, updated_at)
                   VALUES (?,0,?,1,?,?)""",
                (source, rating, json.dumps([rating]), datetime.utcnow().isoformat()),
            )


def get_source_quality() -> dict:
    """Return source quality stats keyed by source name.

    Returns dict of {source: {ideas_generated, ratings_sum, ratings_count,
    last_4_ratings, avg_rating, updated_at}}.
    """
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM source_quality ORDER BY ratings_count DESC").fetchall()
        result = {}
        for r in rows:
            d = dict(r)
            d["last_4_ratings"] = json.loads(d["last_4_ratings"])
            d["avg_rating"] = round(d["ratings_sum"] / d["ratings_count"], 1) if d["ratings_count"] > 0 else None
            source = d.pop("source")
            result[source] = d
        return result
