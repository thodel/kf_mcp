"""db.py — SQLite helpers for Königsfelden MCP server."""
import re
import sqlite3
from contextlib import contextmanager
from typing import Any

import corpus
import vectorstore

_DB_PATH = "kf.db"

# The chunk/embedding tables are vectorstore.SCHEMA_SQL, shared across the
# fleet; the parent column is `parent_id` (was `entry_id` until #11, see
# migrate_parent_id.py). build_db.py owns the corpus tables, embed_db.py these.

MAX_LIMIT = 500            # ceiling for any caller-supplied limit
SPAN_LIMIT = 200           # spans attached to a single person/place record
# Rows in the kf://persons resource. Kept well under the ~150k-character result
# limit Claude.ai and Claude Desktop apply to a tool or resource payload: at 9999
# rows this resource ran to roughly a megabyte and was silently unusable there.
PERSON_INDEX_LIMIT = 1000

def set_db_path(path):
    global _DB_PATH
    _DB_PATH = path
    vectorstore.clear_cache()

@contextmanager
def conn():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    try:
        yield con
    finally:
        con.close()

def r(rows):
    return [dict(row) for row in rows]

def clamp(limit, default, cap=MAX_LIMIT):
    """Constrain a caller-supplied limit. SQLite reads LIMIT -1 as unbounded, so an
    unchecked negative value would return the whole table; anything invalid or
    out of range falls back to the tool's own default."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return min(n, cap) if n >= 1 else default

def clamp_offset(offset):
    try:
        return max(int(offset), 0)
    except (TypeError, ValueError):
        return 0

def like_pattern(query):
    """Substring pattern for LIKE, with the wildcards escaped so a query of '%' or
    '_' matches those characters literally instead of the whole table. Pairs with
    ESCAPE '\\' in the SQL."""
    escaped = (query or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

def stats():
    with conn() as c:
        return {
            "n_entries":  c.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
            "n_spans":    c.execute("SELECT COUNT(*) FROM spans").fetchone()[0],
            "n_persons":  c.execute("SELECT COUNT(*) FROM persons").fetchone()[0],
            "n_places":   c.execute("SELECT COUNT(*) FROM places").fetchone()[0],
            "n_orgs":     c.execute("SELECT COUNT(*) FROM orgs").fetchone()[0],
            "year_min":   c.execute("SELECT MIN(year) FROM entries WHERE year IS NOT NULL").fetchone()[0],
            "year_max":   c.execute("SELECT MAX(year) FROM entries WHERE year IS NOT NULL").fetchone()[0],
        }

def list_entries(limit=50, offset=0):
    with conn() as c:
        return r(c.execute(
            "SELECT id,title,short_id,year,source FROM entries ORDER BY year,id LIMIT ? OFFSET ?",
            (clamp(limit, 50), clamp_offset(offset))
        ).fetchall())

def get_entry(entry_id):
    with conn() as c:
        doc = c.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not doc:
            return None
        # No ref filter: date and measure spans never carry one, and an unlinked
        # persName is still a mention. Callers can tell them apart by `ref`.
        spans = r(c.execute(
            "SELECT class,ref,text,norm FROM spans WHERE entry_id=? ORDER BY id",
            (entry_id,)
        ).fetchall())
        return dict(doc) | {"spans": spans}

_FTS_SQL = (
    "SELECT e.id,e.title,e.short_id,e.year,e.source,"
    "snippet(fts_entries,2,'<mark>','</mark>','…',32) AS snippet "
    "FROM fts_entries JOIN entries e ON fts_entries.id=e.id "
    "WHERE fts_entries MATCH ? ORDER BY rank LIMIT ?"
)

def quote_fts(query):
    """Rewrite a query as quoted FTS5 phrases, one per word (implicit AND).
    Strips the characters FTS5 treats as syntax so no input can be a syntax error."""
    tokens = [t for t in re.split(r'\s+', re.sub(r'["\*\(\):^-]', ' ', query)) if t]
    return ' '.join(f'"{t}"' for t in tokens)

def search_fulltext(query, limit=20):
    limit = clamp(limit, 20)
    if not query or not query.strip():
        return [{"error": "Empty query."}]
    with conn() as c:
        # Honour FTS5 operators (OR, NEAR, prefix*) when the query is well formed;
        # fall back to a literal word search rather than raising at the caller.
        for q in (query, quote_fts(query)):
            if not q:
                break
            try:
                return r(c.execute(_FTS_SQL, (q, limit)).fetchall())
            except sqlite3.OperationalError:
                continue
    return [{"error": f"Could not parse query: {query!r}"}]

def search_persons(query, limit=50):
    with conn() as c:
        return r(c.execute(
            "SELECT p.id,p.forename,p.surname,p.main_name,p.occupation,p.birth,p.death,p.hls_id "
            "FROM persons p "
            "WHERE p.main_name LIKE ?1 ESCAPE '\\' OR p.full_name LIKE ?1 ESCAPE '\\' "
            "OR p.forename LIKE ?1 ESCAPE '\\' OR p.surname LIKE ?1 ESCAPE '\\' "
            "ORDER BY p.surname,p.forename LIMIT ?2",
            (like_pattern(query), clamp(limit, 50))
        ).fetchall())

def search_places(query, limit=50):
    with conn() as c:
        return r(c.execute(
            "SELECT id,name_de,name_fr,country,region,hls_id,place_type FROM places "
            "WHERE name_de LIKE ?1 ESCAPE '\\' OR name_fr LIKE ?1 ESCAPE '\\' "
            "ORDER BY name_de LIMIT ?2",
            (like_pattern(query), clamp(limit, 50))
        ).fetchall())

def search_orgs(query, limit=50):
    with conn() as c:
        return r(c.execute(
            "SELECT id,name,desc_de FROM orgs "
            "WHERE name LIKE ?1 ESCAPE '\\' OR desc_de LIKE ?1 ESCAPE '\\' "
            "ORDER BY name LIMIT ?2",
            (like_pattern(query), clamp(limit, 50))
        ).fetchall())

def get_person(pid):
    with conn() as c:
        p = c.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone()
        if not p:
            return None
        # spans referencing this person
        spans = r(c.execute(
            "SELECT entry_id,text,norm FROM spans WHERE ref=? AND class='persName' LIMIT ?",
            (pid, SPAN_LIMIT)
        ).fetchall())
        return dict(p) | {"spans": spans}

def get_place(pid):
    with conn() as c:
        pl = c.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone()
        if not pl:
            return None
        spans = r(c.execute(
            "SELECT entry_id,text,norm FROM spans WHERE ref=? AND class='placeName' LIMIT ?",
            (pid, SPAN_LIMIT)
        ).fetchall())
        return dict(pl) | {"spans": spans}

def get_entries_for_person(pid, limit=50):
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT e.id,e.title,e.short_id,e.year,e.source "
            "FROM entries e JOIN spans s ON e.id=s.entry_id "
            "WHERE s.ref=? AND s.class='persName' ORDER BY e.year LIMIT ?",
            (pid, clamp(limit, 50))
        ).fetchall()
        return r(rows)

def get_entries_for_place(pid, limit=50):
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT e.id,e.title,e.short_id,e.year,e.source "
            "FROM entries e JOIN spans s ON e.id=s.entry_id "
            "WHERE s.ref=? AND s.class='placeName' ORDER BY e.year LIMIT ?",
            (pid, clamp(limit, 50))
        ).fetchall()
        return r(rows)

def get_entries_by_year(year_from, year_to, limit=100):
    with conn() as c:
        return r(c.execute(
            "SELECT id,title,short_id,year,source FROM entries "
            "WHERE year BETWEEN ? AND ? ORDER BY year,id LIMIT ?",
            (year_from, year_to, clamp(limit, 100))
        ).fetchall())

def person_index(limit=PERSON_INDEX_LIMIT):
    """Brief index of the person authority file. Says so when it is truncated, rather
    than silently returning a prefix of the register."""
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        rows = r(c.execute(
            "SELECT id,main_name,occupation,hls_id FROM persons ORDER BY id LIMIT ?",
            (limit,)
        ).fetchall())
    out = {"total": total, "returned": len(rows), "truncated": len(rows) < total,
           "persons": rows}
    if out["truncated"]:
        out["note"] = (f"Showing the first {len(rows)} of {total} persons. "
                       "Use search_persons(query) to reach the rest.")
    return out

def search_spans(query, cls, limit=50):
    with conn() as c:
        return r(c.execute(
            "SELECT entry_id,ref,text,norm FROM spans "
            "WHERE text LIKE ? ESCAPE '\\' AND class=? ORDER BY id LIMIT ?",
            (like_pattern(query), cls, clamp(limit, 50))
        ).fetchall())


# ── Semantic search ───────────────────────────────────────────────────────────
#
# The matrix, the search and the per-parent cap are vectorstore.py, shared
# across the fleet. What is this corpus's: the join to `entries`, the shape of
# a hit, the year filter — and the head line that comes off the snippet.
#
# Passages hold the *expanded* reading of the transcription, not the raw one:
# see corpus.clean.

_JOIN_SQL = (
    "SELECT c.chunk_id,c.parent_id,c.chunk_index,c.char_start,c.char_end,c.text,"
    "e.title,e.short_id,e.year,e.source "
    "FROM chunks c JOIN entries e ON e.id=c.parent_id "
    "WHERE c.chunk_id IN ({placeholders})"
)


def passage_body(text, title, year):
    """The passage without its head line (fleet rule, ch-h-bot#555).

    Every passage carries `title year` on its first line — for the vector,
    which otherwise has no idea which charter or century "the farm at Lind"
    belongs to. The reader does not need it there: the hit carries title and
    year as fields, the bot's citation line shows them, and a snippet that
    starts with them repeats that line, puts the head instead of the text into
    a text-fragment link and hides the half-word a window may start with from
    the bot's trimming. Only a text that starts with exactly this entry's head,
    built by the function that writes it, is cut; a passage without one (or
    one that merely begins with a line) is untouched. Offsets are not touched:
    char_start/char_end never counted the head.
    """
    head = corpus.head_line(title, year)
    if head and text.startswith(head + "\n"):
        return text[len(head) + 1:]
    return text


def _row_to_hit(row, chunk_id, score, year_from=None, year_to=None):
    """One joined row → one hit, or None when the year filter drops it.

    ``year_from``/``year_to`` restrict to a period, which for this corpus is
    often the point of the question; an entry without a year never passes a
    bound.
    """
    year = row["year"]
    if year_from is not None and (year is None or year < year_from):
        return None
    if year_to is not None and (year is None or year > year_to):
        return None
    return {
        "id": row["parent_id"],
        "chunk_id": chunk_id,
        "title": row["title"],
        "short_id": row["short_id"],
        "year": year,
        "source": row["source"],
        "snippet": passage_body(row["text"], row["title"], year),
        "score": score,
        "chunk_index": row["chunk_index"],
        "char_start": row["char_start"],
        "char_end": row["char_end"],
    }


def search_semantic(query_vector, limit=20, model=None, year_from=None,
                    year_to=None, per_parent=2):
    """Passages closest in meaning to an already-embedded query.

    ``per_parent`` caps how many passages one charter may contribute, so a long
    document cannot fill the result set and crowd out the other entries that
    answer the question.
    """
    return vectorstore.search(
        conn, _DB_PATH, query_vector,
        join_sql=_JOIN_SQL,
        row_to_hit=lambda r, cid, sc: _row_to_hit(r, cid, sc, year_from, year_to),
        limit=clamp(limit, 20), model=model, per_parent=per_parent)


def warm_semantic_index(model):
    """Load the vectors now and report what was loaded, or why it could not be."""
    return vectorstore.warm(conn, _DB_PATH, model)


def semantic_stats(model=None):
    """Coverage of the semantic index, and the runs that produced it."""
    return vectorstore.stats(conn, corpus.PARENT_TABLE)
