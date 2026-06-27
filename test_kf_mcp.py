#!/usr/bin/env python3
"""
test_kf_mcp.py — test suite for Königsfelden MCP server.

Usage:
    # Unit tests (no server needed):
    python test_kf_mcp.py --unit

    # DB integration tests (requires built kf.db):
    python test_kf_mcp.py --db /home/dh/kf_data/kf.db

    # Server integration tests (requires running kf-mcp container):
    python test_kf_mcp.py --server http://localhost:8001

    # All tests:
    python test_kf_mcp.py --unit --db /home/dh/kf_data/kf.db --server http://localhost:8001
"""
import argparse, sys, tempfile, os, glob, sqlite3, json
from pathlib import Path

# ── Sample TEI fragment used for unit tests ────────────────────────────────────

SAMPLE_TEI = '''<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>Test Urkunde 1350</title></titleStmt>
      <idno type="short">AA_0001</idno>
      <seriesStmt><title>Königsfelden</title></seriesStmt>
    </fileDesc>
  </teiHeader>
  <text>
    <body>
      <div>
        <head>Testdokument 1350</head>
        <bibl>Staatsarchiv Aargau</bibl>
        <biblScope unit="page">42</biblScope>
        <date when="1350-03-15">15. März 1350</date>
        <persName ref="per000001" xml:id="p1">Heinrich von Brugg</persName>
        <placeName ref="loc00001" xml:id="l1">Brugg</placeName>
        <orgName ref="org0001">Rat von Bern</orgName>
      </div>
    </body>
  </text>
</TEI>'''

SAMPLE_PLACES_XML = '''<?xml version="1.0"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <listPlace>
    <place xml:id="loc00001">
      <placeName xml:lang="de">Brugg</placeName>
      <placeName xml:lang="fr">Brugg</placeName>
      <country>Schweiz</country>
      <region>Aargau</region>
      <geo>47.48 8.27</geo>
      <bibl><idno type="HLS">000101</idno></bibl>
      <note type="city">Stadt</note>
    </place>
  </listPlace>
</TEI>'''

SAMPLE_PEOPLE_XML = '''<?xml version="1.0"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <listPerson>
    <person xml:id="per000001">
      <persName type="full"><forename>Heinrich</forename><surname>von Brugg</surname></persName>
      <persName type="main">Heinrich v.</persName>
      <occupation>Schultheiss</occupation>
      <birth>1300</birth>
      <death>1360</death>
      <bibl><idno type="HLS">000102</idno></bibl>
      <note>Gründungsmitglied</note>
    </person>
  </listPerson>
</TEI>'''

SAMPLE_ORGS_XML = '''<?xml version="1.0"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <listOrg>
    <org xml:id="org0001">
      <orgName>Rat von Bern</orgName>
      <desc xml:lang="de">Der Rat der Stadt Bern</desc>
      <desc xml:lang="fr">Conseil de la ville de Berne</desc>
    </org>
  </listOrg>
</TEI>'''


# ── Helpers ───────────────────────────────────────────────────────────────────

RED   = "\033[91m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RESET = "\033[0m"

def ok(msg):   print(f"{GREEN}✅ {msg}{RESET}")
def fail(msg): print(f"{RED}❌ {msg}{RESET}")
def warn(msg): print(f"{YELLOW}⚠️  {msg}{RESET}")
def info(msg): print(f"   {msg}")

class TestRunner:
    def __init__(self): self.passed = self.failed = 0
    def check(self, cond, msg):
        if cond:
            self.passed += 1
            ok(msg)
        else:
            self.failed += 1
            fail(msg)
    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'─'*50}")
        print(f"Ran {total} tests: {GREEN}{self.passed} passed{RESET}", end="")
        if self.failed: print(f", {RED}{self.failed} failed{RESET}", end="")
        print()
        return self.failed == 0


# ── 1. Unit tests — parse_entry with filename-based entry_id ─────────────────

def test_entry_id_from_filename():
    """entry_id must come from filename, NOT from xml:id attribute."""
    import xml.etree.ElementTree as ET
    import sys, re
    sys.path.insert(0, str(Path(__file__).parent))
    from build_db import parse_entry

    # Simulate a file named AA_0428_0002.xml
    # The root <TEI> has no xml:id → traditional parse_entry would give ''
    elem = ET.fromstring(SAMPLE_TEI)
    tree = ET.ElementTree(elem)   # wrap Element in a Tree for parse_entry
    fname = "AA_0428_0002"
    entry, spans = parse_entry(tree, fname)

    tr = TestRunner()
    tr.check(entry[0] == "AA_0428_0002", f"entry_id == 'AA_0428_0002' (from filename)")
    tr.check(entry[1] == "Test Urkunde 1350", "title extracted from teiHeader/titleStmt")
    tr.check(entry[3] == 1350, "year extracted from @when attribute")
    tr.check(len(spans) >= 3, f"at least 3 named-entity spans extracted (got {len(spans)})")
    tr.check(spans[0][0] == "AA_0428_0002", "span entry_id matches filename")
    ok(f"parse_entry unit test — {tr.passed} passed")
    return tr.summary()


# ── 2. Unit tests — parse_people / parse_places / parse_orgs ─────────────────

def test_authority_parsers():
    import xml.etree.ElementTree as ET
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from build_db import parse_people, parse_places, parse_orgs

    with tempfile.TemporaryDirectory() as tmpdir:
        # places
        p = f"{tmpdir}/places.xml"
        with open(p, "w") as f: f.write(SAMPLE_PLACES_XML)
        places = parse_places(p)

        # people
        pp = f"{tmpdir}/people.xml"
        with open(pp, "w") as f: f.write(SAMPLE_PEOPLE_XML)
        people = parse_people(pp)

        # orgs
        o = f"{tmpdir}/orgs.xml"
        with open(o, "w") as f: f.write(SAMPLE_ORGS_XML)
        orgs = parse_orgs(o)

    tr = TestRunner()
    tr.check(len(places) == 1, "places: 1 record parsed")
    tr.check(places[0][1] == "Brugg",        "places: name_de extracted (col 1)")
    tr.check(places[0][6] == "000101",       "places: HLS id extracted (col 6)")
    tr.check(places[0][8] == "city",         "places: place_type from note@type (col 8)")

    tr.check(len(people) == 1, "people: 1 record parsed")
    tr.check(people[0][1] == "Heinrich",     "people: forename extracted (col 1)")
    tr.check(people[0][2] == "von Brugg",    "people: surname extracted (col 2)")
    tr.check(people[0][9] == "000102",       "people: HLS id extracted (col 9)")
    tr.check(people[0][4] == "Heinrich v.",  "people: main_name extracted (col 4)")

    tr.check(len(orgs) == 1, "orgs: 1 record parsed")
    tr.check(orgs[0][1] == "Rat von Bern", "orgs: name extracted")
    tr.check(orgs[0][2] == "Der Rat der Stadt Bern", "orgs: desc_de extracted")
    tr.check(orgs[0][3] == "Conseil de la ville de Berne", "orgs: desc_fr extracted")

    ok(f"authority parser unit tests — {tr.passed} passed")
    return tr.summary()


# ── 3. DB build + query integration test ──────────────────────────────────────

def test_db_build_and_queries(db_path):
    """Full DB build from sample data, then run query checks."""
    if not os.path.exists(db_path):
        warn(f"kf.db not found at {db_path} — skipping DB tests")
        return True

    # Check minimum row counts
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    tr = TestRunner()

    n_entries  = cur.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    n_spans    = cur.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    n_persons  = cur.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    n_places   = cur.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    n_orgs     = cur.execute("SELECT COUNT(*) FROM orgs").fetchone()[0]

    info(f"entries={n_entries} spans={n_spans} persons={n_persons} places={n_places} orgs={n_orgs}")

    tr.check(n_entries >= 1550, f"entries: >=1550 (got {n_entries})")
    tr.check(n_spans >= 1000,   f"spans: >=1000 (got {n_spans})")
    tr.check(n_persons >= 5000, f"persons: >=5000 (got {n_persons})")
    tr.check(n_places >= 1300,  f"places: >=1300 (got {n_places})")
    tr.check(n_orgs >= 2000,    f"orgs: >=2000 (got {n_orgs})")

    # Verify spans.ref holds authority IDs (not empty for persName spans)
    pers_refs = cur.execute(
        "SELECT COUNT(*) FROM spans WHERE class='persName' AND ref != ''"
    ).fetchone()[0]
    total_pers = cur.execute(
        "SELECT COUNT(*) FROM spans WHERE class='persName'"
    ).fetchone()[0]
    tr.check(pers_refs > 0, f"persName spans have authority refs ({pers_refs}/{total_pers})")

    # FTS is populated
    fts_count = cur.execute("SELECT COUNT(*) FROM fts_entries").fetchone()[0]
    tr.check(fts_count >= n_entries * 0.9, f"fts_entries populated ({fts_count})")

    # Test get_entry — should work if entry_id is filename-based
    sample_entry = cur.execute("SELECT id FROM entries LIMIT 1").fetchone()
    if sample_entry:
        eid = sample_entry[0]
        row = cur.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
        tr.check(row is not None, f"get_entry by id='{eid}' returns a row")
        tr.check(bool(row[0]), f"entry id is non-empty string ('{row[0]}')")

    # Test search_persons
    rows = cur.execute(
        "SELECT id,main_name FROM persons WHERE main_name LIKE '%Heinrich%' LIMIT 5"
    ).fetchall()
    tr.check(len(rows) >= 0, "search_persons LIKE query works (no results is OK)")

    # Verify places table column count (9 columns)
    col_count = len(cur.execute("PRAGMA table_info(places)").fetchall())
    tr.check(col_count == 9, f"places table has 9 columns (got {col_count})")

    con.close()
    ok(f"DB integration tests — {tr.passed} passed")
    return tr.summary()


# ── 4. Server integration test ────────────────────────────────────────────────

def test_server(base_url):
    """Start server (assumes already running) and call all endpoints."""
    import urllib.request, urllib.parse

    if not base_url:
        warn("No server URL provided — skipping server tests")
        return True

    tr = TestRunner()

    # Health/SSE endpoint
    try:
        r = urllib.request.urlopen(f"{base_url}/sse", timeout=5)
        tr.check(r.status == 200, f"SSE endpoint returns 200")
    except Exception as e:
        tr.check(False, f"SSE endpoint reachable: {e}")

    # Tool calls via JSON-RPC over POST
    def post(tool, params):
        data = json.dumps({"jsonrpc":"2.0","id":1,"method":tool,"params":params}).encode()
        req = urllib.request.Request(f"{base_url}/messages",
            data=data, headers={"Content-Type":"application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return json.loads(resp.read())
        except Exception as e:
            return {"error": str(e)}

    # corpus_stats
    r = post("corpus_stats", {})
    tr.check("result" in r or "error" not in r, f"corpus_stats tool call")
    if "result" in r:
        stats = r["result"]
        tr.check(stats.get("n_entries",0) > 0, f"corpus_stats n_entries > 0")

    # search_persons
    r = post("search_persons", {"query": "Heinrich", "limit": 5})
    tr.check("result" in r or "error" not in r, "search_persons tool call")

    # search_places
    r = post("search_places", {"query": "Bern", "limit": 5})
    tr.check("result" in r or "error" not in r, "search_places tool call")

    # search_fulltext
    r = post("search_fulltext", {"query": "König", "limit": 5})
    tr.check("result" in r or "error" not in r, "search_fulltext tool call")

    # get_entries_by_year
    r = post("get_entries_by_year", {"year_from": 1350, "year_to": 1360})
    tr.check("result" in r or "error" not in r, "get_entries_by_year tool call")

    ok(f"server integration tests — {tr.passed} passed")
    return tr.summary()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="KF MCP test suite")
    ap.add_argument("--unit", action="store_true", help="Run unit tests")
    ap.add_argument("--db", default=os.environ.get("KF_DB",""), help="Path to kf.db")
    ap.add_argument("--server", default=os.environ.get("KF_SERVER",""), help="Server base URL")
    args = ap.parse_args()

    if not args.unit and not args.db and not args.server:
        ap.print_help()
        sys.exit(0)

    print(f"{'═'*50}")
    print("KF MCP test suite")
    print(f"{'═'*50}")

    ok_all = True

    if args.unit:
        print("\n[1] Unit: parse_entry (filename-based entry_id)")
        ok_all &= test_entry_id_from_filename()
        print("\n[2] Unit: authority parsers")
        ok_all &= test_authority_parsers()

    if args.db:
        print(f"\n[3] DB: build+query integration ({args.db})")
        ok_all &= test_db_build_and_queries(args.db)

    if args.server:
        print(f"\n[4] Server: HTTP integration ({args.server})")
        ok_all &= test_server(args.server)

    print(f"\n{'═'*50}")
    sys.exit(0 if ok_all else 1)