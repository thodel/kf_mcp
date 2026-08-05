"""db.py — SQLite helpers for Königsfelden MCP server."""
import re
import sqlite3
from contextlib import contextmanager

_DB_PATH = "kf.db"

MAX_LIMIT = 500            # ceiling for any caller-supplied limit
SPAN_LIMIT = 200           # spans attached to a single person/place record
# Rows in the kf://persons resource. Kept well under the ~150k-character result
# limit Claude.ai and Claude Desktop apply to a tool or resource payload: at 9999
# rows this resource ran to roughly a megabyte and was silently unusable there.
PERSON_INDEX_LIMIT = 1000

def set_db_path(path):
    global _DB_PATH
    _DB_PATH = path

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
