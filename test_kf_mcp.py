#!/usr/bin/env python3
"""
test_kf_mcp.py — test suite for Königsfelden MCP server.

Runs two ways. Under pytest, failures fail the run; the CLI keeps the grouped
output and sets the exit code.

    pytest test_kf_mcp.py                       # unit tests only
    KF_DB=/home/dh/kf_data/kf.db pytest test_kf_mcp.py      # + DB tests
    KF_SERVER=http://localhost:8001 pytest test_kf_mcp.py   # + server tests

    python test_kf_mcp.py --unit
    python test_kf_mcp.py --db /home/dh/kf_data/kf.db
    python test_kf_mcp.py --server http://localhost:8001
    python test_kf_mcp.py --unit --db /home/dh/kf_data/kf.db --server http://localhost:8001

Tests needing a DB or a live server skip when it isn't configured.
Requires pytest (see requirements-dev.txt).
"""
import argparse, sys, tempfile, os, glob, sqlite3, json
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Sample TEI fragments used for unit tests ──────────────────────────────────

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

# Same document, but the header carries the edition's publication date. The year
# must still come from the body — see the teiHeader-contamination bug.
SAMPLE_TEI_HEADER_DATE = '''<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>Test Urkunde 1350</title></titleStmt>
      <publicationStmt><date when="2021-06-01">2021</date></publicationStmt>
      <sourceDesc><p><date when="1349-01-01">1349</date></p></sourceDesc>
    </fileDesc>
    <revisionDesc><change when="2024-02-02">rev</change></revisionDesc>
  </teiHeader>
  <text><body><div>
    <date when="1350-03-15">15. März 1350</date>
    <persName ref="per000001">Heinrich von Brugg</persName>
  </div></body></text>
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

# Middle record is malformed (no <orgName>). It must not swallow the ones after it.
SAMPLE_ORGS_XML_MALFORMED = '''<?xml version="1.0"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <listOrg>
    <org xml:id="org0001"><orgName>Rat von Bern</orgName></org>
    <org xml:id="org0002"><desc xml:lang="de">kein orgName</desc></org>
    <org xml:id="org0003"><orgName>Rat von Zürich</orgName></org>
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

class Checks:
    """Collects several checks so one run reports them all, then fails as a unit.

    Not named Test* — pytest would try to collect it as a test class.
    """
    def __init__(self): self.passed = self.failed = 0; self.failures = []
    def check(self, cond, msg):
        if cond:
            self.passed += 1
            ok(msg)
        else:
            self.failed += 1
            self.failures.append(msg)
            fail(msg)
    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'─'*50}")
        print(f"Ran {total} checks: {GREEN}{self.passed} passed{RESET}", end="")
        if self.failed: print(f", {RED}{self.failed} failed{RESET}", end="")
        print()
        return self.failed == 0
    def assert_ok(self):
        """Raise if any check failed — this is what makes the suite real under pytest."""
        self.summary()
        if self.failed:
            raise AssertionError(
                f"{self.failed} of {self.passed + self.failed} checks failed:\n  - "
                + "\n  - ".join(self.failures)
            )


@pytest.fixture
def db_path():
    """Path to a built kf.db, from the KF_DB env var. Empty → DB tests skip."""
    return os.environ.get("KF_DB", "")

@pytest.fixture
def base_url():
    """Base URL of a running server, from KF_SERVER. Empty → server tests skip."""
    return os.environ.get("KF_SERVER", "")


# ── 1. Unit tests — parse_entry with filename-based entry_id ─────────────────

def test_entry_id_from_filename():
    """entry_id must come from filename, NOT from xml:id attribute."""
    import xml.etree.ElementTree as ET
    from build_db import parse_entry

    # Simulate a file named AA_0428_0002.xml
    # The root <TEI> has no xml:id → traditional parse_entry would give ''
    tree = ET.ElementTree(ET.fromstring(SAMPLE_TEI))
    fname = "AA_0428_0002"
    entry, spans = parse_entry(tree, fname)

    tr = Checks()
    tr.check(entry[0] == "AA_0428_0002", "entry_id == 'AA_0428_0002' (from filename)")
    tr.check(entry[1] == "Test Urkunde 1350", "title extracted from teiHeader/titleStmt")
    tr.check(entry[3] == 1350, f"year extracted from @when attribute (got {entry[3]})")
    tr.check(len(spans) >= 3, f"at least 3 named-entity spans extracted (got {len(spans)})")
    tr.check(spans[0][0] == "AA_0428_0002", "span entry_id matches filename")
    tr.assert_ok()


def test_year_comes_from_body_not_header():
    """A publication/revision date in the teiHeader must not become the entry year."""
    import xml.etree.ElementTree as ET
    from build_db import parse_entry, first_year

    tree = ET.ElementTree(ET.fromstring(SAMPLE_TEI_HEADER_DATE))
    entry, _ = parse_entry(tree, "AA_0001")

    tr = Checks()
    tr.check(entry[3] == 1350, f"body date wins over header dates (got {entry[3]})")
    tr.check(entry[3] != 2021, "publicationStmt year is not used")

    # Header-only fallback: no body date → sourceDesc, still never publicationStmt.
    no_body_date = SAMPLE_TEI_HEADER_DATE.replace(
        '<date when="1350-03-15">15. März 1350</date>', 'kein Datum')
    entry2, _ = parse_entry(ET.ElementTree(ET.fromstring(no_body_date)), "AA_0002")
    tr.check(entry2[3] == 1349, f"falls back to sourceDesc date (got {entry2[3]})")

    tr.check(first_year([]) is None, "first_year([]) is None")
    tr.assert_ok()


# ── 2. Unit tests — parse_people / parse_places / parse_orgs ─────────────────

def test_authority_parsers():
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

    tr = Checks()
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
    tr.assert_ok()


def test_adjacent_markup_does_not_glue_words():
    """Tags that touch in the source must not fuse words — '1350Heinrich' is unsearchable."""
    import xml.etree.ElementTree as ET
    from build_db import norm_text

    def flat(frag):
        return norm_text(ET.fromstring(
            f'<div xmlns="http://www.tei-c.org/ns/1.0">{frag}</div>'))

    tr = Checks()
    tr.check(flat('<date when="1350-03-15">15. März 1350</date><persName>Heinrich</persName>')
             == "15. März 1350 Heinrich", "adjacent phrase elements are separated")
    tr.check(flat('<persName>Heinrich</persName><placeName>Brugg</placeName>')
             == "Heinrich Brugg", "two adjacent entities are separated")
    tr.check(flat('<persName>Brugg</persName>, Schultheiss.') == "Brugg, Schultheiss.",
             "punctuation stays flush against the preceding word")
    tr.check(flat('<date>1350.</date><persName>Heinrich</persName>') == "1350. Heinrich",
             "a word after closing punctuation is separated")
    tr.check(flat('Klos<ex>ter</ex>felden') == "Klosterfelden",
             "abbreviation expansion stays inside the word")
    tr.check(flat('IIII<hi rend="sup">c</hi> Pfund') == "IIIIc Pfund",
             "hi is spliced into the word")
    tr.check(flat('Klos<lb break="no"/>ter') == "Kloster", 'lb break="no" continues the word')
    tr.check(flat('Heinrich<lb/>von Brugg') == "Heinrich von Brugg", "plain lb separates words")
    tr.check(flat('<hi>Kloster-</hi><hi>felden</hi>') == "Kloster-felden", "hyphen stays flush")
    tr.check(flat('  Heinrich   von\n\n  Brugg  ') == "Heinrich von Brugg", "whitespace collapsed")
    tr.assert_ok()


def test_malformed_record_does_not_truncate_register():
    """One bad record must be skipped, not abandon the rest of the register."""
    from build_db import parse_orgs, parse_people

    tr = Checks()
    with tempfile.TemporaryDirectory() as tmpdir:
        o = f"{tmpdir}/orgs.xml"
        with open(o, "w") as f: f.write(SAMPLE_ORGS_XML_MALFORMED)
        orgs = parse_orgs(o)

        ids = [r[0] for r in orgs]
        tr.check(len(orgs) == 3, f"all 3 orgs parsed despite the bad one (got {len(orgs)})")
        tr.check("org0003" in ids, "records after the malformed one survive")

        missing = parse_people(f"{tmpdir}/does_not_exist.xml")
        tr.check(missing == [], "missing authority file returns [] instead of raising")

        bad = f"{tmpdir}/broken.xml"
        with open(bad, "w") as f: f.write("<not valid xml")
        tr.check(parse_orgs(bad) == [], "malformed XML file returns [] instead of raising")
    tr.assert_ok()


def test_rebuild_is_idempotent():
    """Rebuilding over an existing DB must not duplicate spans."""
    from build_db import build

    tr = Checks()
    with tempfile.TemporaryDirectory() as tmpdir:
        docs, regs = f"{tmpdir}/docs", f"{tmpdir}/registers"
        os.makedirs(docs); os.makedirs(regs)
        with open(f"{docs}/AA_0001.xml", "w") as f: f.write(SAMPLE_TEI)
        with open(f"{regs}/people.xml", "w") as f: f.write(SAMPLE_PEOPLE_XML)
        with open(f"{regs}/places.xml", "w") as f: f.write(SAMPLE_PLACES_XML)
        with open(f"{regs}/organizations.xml", "w") as f: f.write(SAMPLE_ORGS_XML)

        dbp = f"{tmpdir}/kf.db"
        build(docs, regs, dbp)
        con = sqlite3.connect(dbp)
        counts1 = [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in ("entries", "spans", "persons", "fts_entries", "fts_spans")]
        con.close()

        build(docs, regs, dbp)   # same inputs, same DB
        con = sqlite3.connect(dbp)
        counts2 = [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in ("entries", "spans", "persons", "fts_entries", "fts_spans")]
        # raises if the external-content FTS index drifted from its content table
        con.execute("INSERT INTO fts_entries(fts_entries) VALUES('integrity-check')")
        con.close()

        tr.check(counts1 == counts2, f"row counts stable across rebuilds ({counts1} vs {counts2})")
        tr.check(counts1[1] > 0, "spans were actually written")
        tr.check(counts1[3] == counts1[0], "fts_entries matches entries")
    tr.assert_ok()


def test_like_wildcards_are_escaped():
    """A '%' or '_' in a search query must match itself, not act as a wildcard."""
    import db as db_module
    from build_db import DDL, TRIGGERS

    tr = Checks()
    with tempfile.TemporaryDirectory() as tmpdir:
        dbp = f"{tmpdir}/kf.db"
        con = sqlite3.connect(dbp)
        con.executescript(DDL); con.executescript(TRIGGERS)
        con.executemany("INSERT INTO persons VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            ("per1", "", "", "", "Heinrich von Brugg", "", "", "", "", "", ""),
            ("per2", "", "", "", "Anna 100% Sicher",   "", "", "", "", "", ""),
            ("per3", "", "", "", "Hans_Meier",         "", "", "", "", "", ""),
            ("per4", "", "", "", "Ulrich Zwingli",     "", "", "", "", "", ""),
        ])
        con.commit(); con.close()
        db_module.set_db_path(dbp)

        names = lambda rows: sorted(r["main_name"] for r in rows)
        tr.check(names(db_module.search_persons("%")) == ["Anna 100% Sicher"],
                 "'%' matches a literal percent, not every row")
        tr.check(names(db_module.search_persons("_")) == ["Hans_Meier"],
                 "'_' matches a literal underscore, not any character")
        tr.check(names(db_module.search_persons("100%")) == ["Anna 100% Sicher"],
                 "percent inside a query is literal")
        tr.check(db_module.search_persons("%Zwingli%") == [],
                 "caller-supplied wildcards do not expand")
        tr.check(names(db_module.search_persons("Heinrich")) == ["Heinrich von Brugg"],
                 "ordinary substring search still works")
        tr.check(db_module.like_pattern("a%b_c") == "%a\\%b\\_c%", "like_pattern escapes both wildcards")
    tr.assert_ok()


def test_person_index_reports_truncation():
    """kf://persons must say when it is only showing a prefix of the register."""
    import db as db_module
    from build_db import DDL, TRIGGERS

    tr = Checks()
    with tempfile.TemporaryDirectory() as tmpdir:
        dbp = f"{tmpdir}/kf.db"
        con = sqlite3.connect(dbp)
        con.executescript(DDL); con.executescript(TRIGGERS)
        con.executemany("INSERT INTO persons VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        [(f"per{i}", "", "", "", f"P{i}", "", "", "", "", "", "")
                         for i in range(10)])
        con.commit(); con.close()
        db_module.set_db_path(dbp)

        full = db_module.person_index()
        tr.check(full["total"] == 10 and full["returned"] == 10, "full index returns everything")
        tr.check(full["truncated"] is False, "full index is not flagged truncated")
        tr.check("note" not in full, "no truncation note when nothing is cut")

        cut = db_module.person_index(limit=3)
        tr.check(cut["total"] == 10 and cut["returned"] == 3, "truncated index reports both counts")
        tr.check(cut["truncated"] is True, "truncation is flagged")
        tr.check("search_persons" in cut.get("note", ""), "note points at search_persons")
    tr.assert_ok()


def test_http_path_is_normalised():
    """The endpoint path must match the public path exactly, however it is written.

    A sub-path deployment 404s when the app is mounted at /mcp while nginx forwards
    /mcp/kf/mcp, so this is the knob that has to be right."""
    pytest.importorskip("mcp", reason="mcp SDK not installed")
    import server as server_module

    tr = Checks()
    n = server_module.normalise_path
    tr.check(n("/mcp/kf/mcp") == "/mcp/kf/mcp", "an already-correct path is unchanged")
    tr.check(n("mcp/kf/mcp") == "/mcp/kf/mcp", "a missing leading slash is added")
    tr.check(n("/mcp/kf/mcp/") == "/mcp/kf/mcp", "a trailing slash is dropped")
    tr.check(n("") == "/mcp" and n(None) == "/mcp", "an empty path falls back to /mcp")

    tr.check(server_module.parse_args([]).http_path == "/mcp", "default endpoint path is /mcp")
    args = server_module.parse_args(["--http-path", "mcp/kf/mcp/"])
    tr.check(args.http_path == "/mcp/kf/mcp", "--http-path is normalised on the way in")
    tr.assert_ok()


# ── 3. DB query integration test ──────────────────────────────────────────────

def test_db_build_and_queries(db_path):
    """Query checks against a real built kf.db."""
    if not db_path or not os.path.exists(db_path):
        pytest.skip(f"kf.db not found at {db_path!r} — set KF_DB or pass --db")

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    tr = Checks()

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

    # Years must be document dates, not edition years — the teiHeader bug's fingerprint
    n_modern = cur.execute("SELECT COUNT(*) FROM entries WHERE year > 1800").fetchone()[0]
    tr.check(n_modern == 0, f"no entry dated after 1800 (got {n_modern})")
    yr = cur.execute("SELECT MIN(year),MAX(year) FROM entries WHERE year IS NOT NULL").fetchone()
    info(f"year range: {yr[0]}–{yr[1]}")

    # No duplicated spans from a repeated build
    dupes = cur.execute(
        "SELECT COUNT(*) FROM (SELECT entry_id,class,text,COUNT(*) c "
        "FROM spans GROUP BY entry_id,class,text HAVING c > 1)"
    ).fetchone()[0]
    info(f"repeated (entry,class,text) span groups: {dupes}")

    # Test get_entry — should work if entry_id is filename-based
    sample_entry = cur.execute("SELECT id FROM entries LIMIT 1").fetchone()
    if sample_entry:
        eid = sample_entry[0]
        row = cur.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
        tr.check(row is not None, f"get_entry by id='{eid}' returns a row")
        tr.check(bool(row[0]), f"entry id is non-empty string ('{row[0]}')")

    # Verify places table column count (9 columns)
    col_count = len(cur.execute("PRAGMA table_info(places)").fetchall())
    tr.check(col_count == 9, f"places table has 9 columns (got {col_count})")

    con.close()
    tr.assert_ok()


def test_db_layer_against_real_db(db_path):
    """db.py query helpers against a real kf.db, including the hostile inputs."""
    if not db_path or not os.path.exists(db_path):
        pytest.skip(f"kf.db not found at {db_path!r} — set KF_DB or pass --db")

    import db as db_module
    db_module.set_db_path(db_path)
    tr = Checks()

    s = db_module.stats()
    tr.check(s["n_entries"] > 0, f"stats(): {s['n_entries']} entries")

    # limits are clamped, never unbounded
    tr.check(len(db_module.list_entries(-1)) <= db_module.MAX_LIMIT, "list_entries(-1) is bounded")
    tr.check(len(db_module.list_entries(10**9)) <= db_module.MAX_LIMIT,
             f"list_entries(huge) capped at {db_module.MAX_LIMIT}")

    # FTS queries with operator characters must not raise
    for q in ['Heinrich"', "Brugg AND", "(unbalanced", "Heinr*"]:
        try:
            res = db_module.search_fulltext(q, 5)
            tr.check(isinstance(res, list), f"search_fulltext({q!r}) returned a list")
        except Exception as e:
            tr.check(False, f"search_fulltext({q!r}) raised {type(e).__name__}: {e}")

    # search_spans binds all three parameters
    try:
        tr.check(isinstance(db_module.search_spans("a", "persName", 5), list), "search_spans executes")
    except Exception as e:
        tr.check(False, f"search_spans raised {type(e).__name__}: {e}")

    tr.assert_ok()


# ── 4. Server integration test ────────────────────────────────────────────────

def _tool_payload(result):
    """Unwrap a CallToolResult into a Python object."""
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured.get("result", structured)
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            try: return json.loads(text)
            except json.JSONDecodeError: return text
    return None


def test_server(base_url):
    """Drive the running server over streamable HTTP using the official client."""
    if not base_url:
        pytest.skip("no server URL — set KF_SERVER or pass --server")
    try:
        import anyio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as e:
        pytest.skip(f"mcp client library not available: {e}")

    tr = Checks()
    expected = {"corpus_stats", "list_entries", "get_entry", "search_persons", "get_person",
                "search_places", "get_place", "search_orgs", "search_fulltext",
                "get_entries_for_person", "get_entries_for_place", "get_entries_by_year"}
    # KF_SERVER may be the bare origin (local run) or the full public endpoint
    # (https://host/mcp/kf/mcp). Only append the default path to the former.
    url = base_url.rstrip("/")
    if not url.rsplit("/", 1)[-1] == "mcp":
        url += "/mcp"

    async def exercise():
        # Streamable HTTP is a session protocol: the server issues a session id on
        # initialize and expects it on every later POST. Hand-rolled POSTs cannot work.
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                names = {t.name for t in (await session.list_tools()).tools}
                tr.check(bool(names), f"tools/list returned {len(names)} tools")
                missing = expected - names
                tr.check(not missing, f"all expected tools exposed (missing: {sorted(missing)})")

                stats = _tool_payload(await session.call_tool("corpus_stats", {}))
                tr.check(isinstance(stats, dict) and stats.get("n_entries", 0) > 0,
                         f"corpus_stats returns entries (got {stats})")

                for tool, args in [
                    ("search_persons",      {"query": "Heinrich", "limit": 5}),
                    ("search_places",       {"query": "Bern", "limit": 5}),
                    ("search_fulltext",     {"query": "König", "limit": 5}),
                    ("get_entries_by_year", {"year_from": 1350, "year_to": 1360}),
                ]:
                    res = await session.call_tool(tool, args)
                    tr.check(not res.is_error, f"{tool} call succeeded")
                    tr.check(_tool_payload(res) is not None, f"{tool} returned a payload")

                # Hostile input must come back as data, not a transport error
                res = await session.call_tool("search_fulltext", {"query": 'Heinrich"', "limit": 3})
                tr.check(not res.is_error, "search_fulltext survives an unbalanced quote")

                res = await session.call_tool("get_entry", {"entry_id": "definitely_not_an_id"})
                payload = _tool_payload(res)
                tr.check(isinstance(payload, dict) and "error" in payload,
                         f"unknown entry_id returns an error object (got {payload})")

                resources = {str(r.uri) for r in (await session.list_resources()).resources}
                tr.check("kf://stats" in resources, f"kf://stats resource listed (got {sorted(resources)})")

    anyio.run(exercise)
    tr.assert_ok()


# ── CLI ───────────────────────────────────────────────────────────────────────

def cli_run(label, fn, *fn_args):
    """Run one test in CLI mode, translating pytest outcomes into a bool."""
    print(f"\n{label}")
    try:
        fn(*fn_args)
        return True
    except pytest.skip.Exception as e:
        warn(f"skipped: {e}")
        return True
    except AssertionError as e:
        fail(str(e))
        return False
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
        return False


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
        ok_all &= cli_run("[1] Unit: parse_entry (filename-based entry_id)", test_entry_id_from_filename)
        ok_all &= cli_run("[2] Unit: year comes from body, not teiHeader", test_year_comes_from_body_not_header)
        ok_all &= cli_run("[3] Unit: authority parsers", test_authority_parsers)
        ok_all &= cli_run("[4] Unit: adjacent markup does not glue words",
                          test_adjacent_markup_does_not_glue_words)
        ok_all &= cli_run("[5] Unit: malformed record does not truncate register",
                          test_malformed_record_does_not_truncate_register)
        ok_all &= cli_run("[6] Unit: rebuild is idempotent", test_rebuild_is_idempotent)
        ok_all &= cli_run("[7] Unit: LIKE wildcards are escaped", test_like_wildcards_are_escaped)
        ok_all &= cli_run("[8] Unit: person index reports truncation",
                          test_person_index_reports_truncation)
        ok_all &= cli_run("[9] Unit: endpoint path is normalised", test_http_path_is_normalised)

    if args.db:
        ok_all &= cli_run(f"[10] DB: query integration ({args.db})", test_db_build_and_queries, args.db)
        ok_all &= cli_run(f"[11] DB: db.py query layer ({args.db})", test_db_layer_against_real_db, args.db)

    if args.server:
        ok_all &= cli_run(f"[12] Server: MCP integration ({args.server})", test_server, args.server)

    print(f"\n{'═'*50}")
    print(f"{GREEN}ALL PASSED{RESET}" if ok_all else f"{RED}FAILURES{RESET}")
    sys.exit(0 if ok_all else 1)
