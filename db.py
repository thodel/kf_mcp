"""db.py — SQLite helpers for Königsfelden MCP server."""
import sqlite3
from contextmanager import contextmanager

_DB_PATH = "kf.db"

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
            (limit, offset)
        ).fetchall())

def get_entry(entry_id):
    with conn() as c:
        doc = c.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not doc:
            return None
        spans = r(c.execute(
            "SELECT class,ref,text,norm FROM spans WHERE entry_id=? AND ref!=''",
            (entry_id,)
        ).fetchall())
        return dict(doc) | {"spans": spans}

def search_fulltext(query, limit=20):
    with conn() as c:
        rows = c.execute(
            "SELECT e.id,e.title,e.short_id,e.year,e.source,"
            "snippet(fts_entries,2,'<mark>','</mark>','…',32) AS snippet "
            "FROM fts_entries JOIN entries e ON fts_entries.id=e.id "
            "WHERE fts_entries MATCH ? ORDER BY rank LIMIT ?",
            (query, limit)
        ).fetchall()
        return r(rows)

def search_persons(query, limit=50):
    with conn() as c:
        return r(c.execute(
            "SELECT p.id,p.forename,p.surname,p.main_name,p.occupation,p.birth,p.death,p.hls_id "
            "FROM persons p "
            "WHERE p.main_name LIKE ?1 OR p.full_name LIKE ?1 OR p.forename LIKE ?1 OR p.surname LIKE ?1 "
            "ORDER BY p.surname,p.forename LIMIT ?2",
            (f"%{query}%", limit)
        ).fetchall())

def search_places(query, limit=50):
    with conn() as c:
        return r(c.execute(
            "SELECT id,name_de,name_fr,country,region,hls_id,place_type FROM places "
            "WHERE name_de LIKE ?1 OR name_fr LIKE ?1 "
            "ORDER BY name_de LIMIT ?2",
            (f"%{query}%", limit)
        ).fetchall())

def search_orgs(query, limit=50):
    with conn() as c:
        return r(c.execute(
            "SELECT id,name,desc_de FROM orgs "
            "WHERE name LIKE ?1 OR desc_de LIKE ?1 ORDER BY name LIMIT ?2",
            (f"%{query}%", limit)
        ).fetchall())

def get_person(pid):
    with conn() as c:
        p = c.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone()
        if not p:
            return None
        # spans referencing this person
        spans = r(c.execute(
            "SELECT entry_id,text,norm FROM spans WHERE ref=? AND class='persName' LIMIT 200",
            (pid,)
        ).fetchall())
        return dict(p) | {"spans": spans}

def get_place(pid):
    with conn() as c:
        pl = c.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone()
        if not pl:
            return None
        spans = r(c.execute(
            "SELECT entry_id,text,norm FROM spans WHERE ref=? AND class='placeName' LIMIT 200",
            (pid,)
        ).fetchall())
        return dict(pl) | {"spans": spans}

def get_entries_for_person(pid, limit=50):
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT e.id,e.title,e.short_id,e.year,e.source "
            "FROM entries e JOIN spans s ON e.id=s.entry_id "
            "WHERE s.ref=? AND s.class='persName' ORDER BY e.year LIMIT ?",
            (pid, limit)
        ).fetchall()
        return r(rows)

def get_entries_for_place(pid, limit=50):
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT e.id,e.title,e.short_id,e.year,e.source "
            "FROM entries e JOIN spans s ON e.id=s.entry_id "
            "WHERE s.ref=? AND s.class='placeName' ORDER BY e.year LIMIT ?",
            (pid, limit)
        ).fetchall()
        return r(rows)

def get_entries_by_year(year_from, year_to, limit=100):
    with conn() as c:
        return r(c.execute(
            "SELECT id,title,short_id,year,source FROM entries "
            "WHERE year BETWEEN ? AND ? ORDER BY year,id LIMIT ?",
            (year_from, year_to, limit)
        ).fetchall())

def search_spans(query, cls, limit=50):
    with conn() as c:
        base = "SELECT entry_id,ref,text,norm FROM spans WHERE text LIKE ? AND class=? LIMIT ?"
        return r(c.execute(base, (f"%{query}%", cls)).fetchall())
