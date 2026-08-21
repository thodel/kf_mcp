"""db.py — SQLite helpers for Königsfelden MCP server."""
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any

_DB_PATH = "kf.db"

# Tables owned by embed_db.py. build_db.py owns the rest, so this is kept
# separate: rebuilding the corpus must not silently drop the vectors, and
# adding vectors must not touch the corpus.
EMBEDDING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT    PRIMARY KEY,   -- "<entry_id>#<chunk_index>"
    entry_id    TEXT    NOT NULL,
    chunk_index INTEGER NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    text        TEXT    NOT NULL,      -- the expanded reading, not the raw transcription
    UNIQUE (entry_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT    PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    model    TEXT    NOT NULL,
    dims     INTEGER NOT NULL,
    vector   BLOB    NOT NULL          -- float32, little-endian, L2-normalised
);
CREATE TABLE IF NOT EXISTS embedding_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    model       TEXT NOT NULL,
    dims        INTEGER,
    base_url    TEXT,
    chunk_chars INTEGER,
    chunk_overlap INTEGER,
    n_articles  INTEGER,
    n_chunks    INTEGER,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS ix_chunks_entry ON chunks(entry_id);
CREATE INDEX IF NOT EXISTS ix_embeddings_model ON embeddings(model);
"""

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


# ── Semantic search ───────────────────────────────────────────────────────────
#
# Vectors are stored L2-normalised, so cosine similarity is a dot product and
# the search is one matrix multiply. The corpus is small — a few thousand
# passages — so results are exact and there is no index to tune.
#
# Passages hold the *expanded* reading of the transcription, not the raw one:
# see embeddings.clean_entry_text.

_VECTOR_CACHE = {}


def _load_matrix(model):
    """(chunk_ids, matrix) for a model, loaded once and cached."""
    key = (_DB_PATH, model)
    cached = _VECTOR_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "numpy is required for semantic search — pip install numpy") from exc
    with conn() as c:
        rows = c.execute(
            "SELECT chunk_id, dims, vector FROM embeddings WHERE model = ? "
            "ORDER BY chunk_id", (model,)).fetchall()
    if not rows:
        raise RuntimeError(
            f"no embeddings for model {model!r} in {_DB_PATH}. "
            "Run embed_db.py to build the semantic index.")
    dims = rows[0]["dims"]
    chunk_ids = [row["chunk_id"] for row in rows]
    # One contiguous buffer: the difference between a matrix multiply and
    # thousands of small ones.
    buffer = b"".join(row["vector"] for row in rows)
    matrix = np.frombuffer(buffer, dtype="<f4").reshape(len(rows), dims)
    _VECTOR_CACHE[key] = (chunk_ids, matrix)
    return chunk_ids, matrix


def warm_semantic_index(model):
    """Load the vectors now and report what was loaded, or why it could not be."""
    try:
        chunk_ids, matrix = _load_matrix(model)
    except RuntimeError as exc:
        return {"ready": False, "model": model, "reason": str(exc)}
    return {"ready": True, "model": model, "n_chunks": len(chunk_ids),
            "dims": int(matrix.shape[1]),
            "megabytes": round(matrix.nbytes / 1_048_576, 1)}


def semantic_stats(model=None):
    """Coverage of the semantic index, and the runs that produced it."""
    with conn() as c:
        if not c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name='embeddings'").fetchone():
            return {"indexed": False,
                    "reason": "no embeddings table; run embed_db.py"}
        by_model = c.execute(
            "SELECT model, COUNT(*) n, MAX(dims) d FROM embeddings GROUP BY model"
        ).fetchall()
        n_chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_total = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        n_indexed = c.execute(
            "SELECT COUNT(DISTINCT entry_id) FROM chunks").fetchone()[0]
        runs = c.execute(
            "SELECT run_id, started_at, finished_at, model, dims, n_chunks, notes "
            "FROM embedding_runs ORDER BY started_at DESC LIMIT 5").fetchall()
    return {
        "indexed": bool(by_model),
        "n_chunks": n_chunks,
        "n_entries_indexed": n_indexed,
        "n_entries_total": n_total,
        "coverage": round(n_indexed / n_total, 4) if n_total else 0.0,
        "models": [{"model": m["model"], "n_vectors": m["n"], "dims": m["d"]}
                   for m in by_model],
        "recent_runs": r(runs),
    }


_SEMANTIC_SQL = (
    "SELECT c.chunk_id,c.entry_id,c.chunk_index,c.char_start,c.char_end,c.text,"
    "e.title,e.short_id,e.year,e.source "
    "FROM chunks c JOIN entries e ON e.id=c.entry_id "
    "WHERE c.chunk_id IN ({placeholders})"
)


def search_semantic(query_vector, limit=20, model=None, year_from=None,
                    year_to=None, per_entry=2):
    """Passages closest in meaning to an already-embedded query.

    ``per_entry`` caps how many passages one charter may contribute, so a long
    document cannot fill the result set and crowd out the other entries that
    answer the question. ``year_from``/``year_to`` restrict to a period, which
    for this corpus is often the point of the question.
    """
    import numpy as np

    limit = clamp(limit, 20)
    model = model or os.environ.get("KF_EMBED_MODEL", "qwen3-embedding-0.6b")
    chunk_ids, matrix = _load_matrix(model)

    query = np.asarray(query_vector, dtype="float32")
    if query.shape[0] != matrix.shape[1]:
        raise ValueError(
            f"query has {query.shape[0]} dimensions, index has {matrix.shape[1]}")
    norm = float(np.linalg.norm(query)) or 1.0
    scores = matrix @ (query / norm)

    # Take a generous slice before filtering: the year filter and the per-entry
    # cap both discard candidates.
    fetch = min(len(chunk_ids), max(limit * 8, limit + 50))
    candidates = np.argpartition(-scores, fetch - 1)[:fetch]
    candidates = candidates[np.argsort(-scores[candidates])]
    picked = [(chunk_ids[i], float(scores[i])) for i in candidates]

    by_id = {}
    with conn() as c:
        for start in range(0, len(picked), 400):
            window = picked[start:start + 400]
            sql = _SEMANTIC_SQL.format(placeholders=",".join("?" * len(window)))
            for row in c.execute(sql, [cid for cid, _ in window]).fetchall():
                by_id[row["chunk_id"]] = row

    out, seen = [], {}
    for chunk_id, score in picked:
        row = by_id.get(chunk_id)
        if row is None:
            continue                      # vector outlived its chunk
        year = row["year"]
        if year_from is not None and (year is None or year < year_from):
            continue
        if year_to is not None and (year is None or year > year_to):
            continue
        if seen.get(row["entry_id"], 0) >= per_entry:
            continue
        seen[row["entry_id"]] = seen.get(row["entry_id"], 0) + 1
        out.append({
            "id": row["entry_id"],
            "chunk_id": chunk_id,
            "title": row["title"],
            "short_id": row["short_id"],
            "year": year,
            "source": row["source"],
            "snippet": row["text"],
            "score": round(score, 4),
            "chunk_index": row["chunk_index"],
            "char_start": row["char_start"],
            "char_end": row["char_end"],
        })
        if len(out) >= limit:
            break
    return out
