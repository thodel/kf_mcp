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
                # Der Text allein ist nicht maschinell von einem Ausfall zu
                # unterscheiden; das Feld ist es (ch-h-bot#497).
                tr.check(isinstance(payload, dict) and payload.get("not_found") is True,
                         f"unknown entry_id says so in `not_found` (got {payload})")

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


# ── The vendored embeddings.py ───────────────────────────────────────────────

@pytest.mark.parametrize("entry", ["server", "embed_db"])
def test_the_vendored_embeddings_read_the_kf_settings(entry):
    # embeddings.py is byte-identical across the fleet and finds KF_EMBED_MODEL
    # and the other KF_* settings only through MCP_ENV_PREFIX, which corpus.py
    # sets before importing it. Without that, an override would be read by
    # nothing and queries would be embedded with a model the index was not
    # built with.
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "MCP_ENV_PREFIX"}
    env["KF_EMBED_MODEL"] = "probe-model"
    out = subprocess.run(
        [sys.executable, "-c",
         f"import {entry}, embeddings; print(embeddings.ENV_PREFIX, embeddings.DEFAULT_MODEL)"],
        cwd=os.path.dirname(os.path.abspath(__file__)), env=env,
        capture_output=True, text=True)
    assert out.stdout.split() == ["KF", "probe-model"], out.stderr


def test_queries_carry_this_corpus_instruction():
    # The shared embed_query only knows "Swiss history"; corpus.embed_query
    # keeps the instruction this index has always been queried with.
    import corpus as corpus_module
    seen = {}

    class FakeClient:
        class embeddings:
            @staticmethod
            def create(model, input):
                seen["input"] = input
                return type("R", (), {"data": [type("D", (), {"embedding": [1.0, 0.0]})()]})()

    corpus_module.embed_query("Wer?", client=FakeClient())
    assert seen["input"][0].startswith("Instruct:")
    assert "Königsfelden" in seen["input"][0]
    assert seen["input"][0].endswith("Wer?")


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


# ── Semantic search ──────────────────────────────────────────────────────────
#
# No test here calls GPUStack: vectors are synthesised, so the suite runs off
# the UniBE VPN and in CI.

import db as _db
import corpus as corpus_module
import embeddings as emb


def _vocab(*texts):
    return corpus_module.build_vocab(texts)


# The corpus transcribes diplomatically: every abbreviated word appears as the
# raw manuscript form immediately followed by the editor's expansion. These
# cases are all taken from the real text.

def test_a_raw_form_that_is_a_whole_word_is_replaced_by_its_reading():
    vocab = _vocab("un̄ und dez jares " * 6)
    assert corpus_module.clean_entry_text("un̄ und dez jares", vocab) == "und dez jares"


def test_a_raw_form_that_continues_a_fragment_is_joined():
    # "Heinrich" is written "Hein" + raw "r₎" + "rich".
    corpus = ["nante Hein r₎ rich Swab"] * 8 + ["der Hein hiez"]
    assert "Heinrich" in corpus_module.clean_entry_text(corpus[0], _vocab(*corpus))


def test_a_real_word_before_an_expansion_is_not_glued_to_it():
    # "Diz ist dˀ der" must become "Diz ist der", never "Diz istder".
    corpus = ["Diz ist dˀ der brief"] + ["ist"] * 40
    out = corpus_module.clean_entry_text(corpus[0], _vocab(*corpus))
    assert out == "Diz ist der brief"


def test_a_frequent_token_can_still_be_a_fragment():
    # "Hein" is also a name, so a rare/common threshold cannot decide this;
    # the ratio of fragment to solo occurrences can.
    corpus = ["Hein r₎ rich"] * 20 + ["der Hein kam"] * 3
    vocab = _vocab(*corpus)
    fragment, solo = vocab["hein"]
    assert fragment > solo
    assert corpus_module.clean_entry_text("Hein r₎ rich", vocab) == "Heinrich"


def test_combining_diacritics_do_not_block_a_join():
    # "Oͤs" is O + U+0364 + s; str.isalpha() is False for the combining mark.
    corpus = ["von Oͤs tˀ ter rich"] * 6
    assert "Oͤster" in corpus_module.clean_entry_text(corpus[0], _vocab(*corpus))


def test_segment_markers_are_dropped():
    vocab = _vocab("dem lechenman gibt man")
    assert "✳" not in corpus_module.clean_entry_text("dem ✳ lechenman ✳ gibt", vocab)


def test_a_trailing_raw_form_with_no_expansion_is_dropped():
    assert corpus_module.clean_entry_text("der hof d₎", _vocab("der hof")) == "der hof"


def test_cleaning_empty_text_is_safe():
    assert corpus_module.clean_entry_text("", {}) == ""
    assert corpus_module.clean_entry_text(None, {}) == ""


def test_vocabulary_separates_fragments_from_words():
    vocab = _vocab("Hein r₎ rich und ist gut", "ist ist ist und und")
    assert vocab["hein"] == (1, 0)      # only ever before a raw form
    assert vocab["ist"][1] > vocab["ist"][0]


# ── Vector search ────────────────────────────────────────────────────────────
#
# The matrix and the search are vectorstore.py (shared, byte-identical across
# the fleet since #11); what is tested here is this repo's part: the join, the
# hit, the year filter, the per-parent cap — and the head that comes off the
# snippet.

import vectorstore
import embed_db
import migrate_parent_id


def _semantic_db(tmp_path):
    """A corpus with hand-placed vectors, so expected ranking is arithmetic.

    U-1 and U-2 carry the head line every passage gets from embed_db.py; U-3
    does not (an index built before the head existed), and its second passage
    begins with U-2's head — the one case the snippet cut must leave alone.
    """
    path = str(tmp_path / "kf_sem.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE entries(id TEXT PRIMARY KEY, title TEXT, short_id TEXT,
                             year INTEGER, source TEXT, pages TEXT, text_raw TEXT);
    """)
    con.executescript(vectorstore.SCHEMA_SQL)
    rows = [("U-1", "Urkunde 1", 1360, [1.0, 0.0, 0.0]),
            ("U-2", "Urkunde 2", 1450, [0.8, 0.6, 0.0]),
            ("U-3", "Urkunde 3", 1644, [0.0, 1.0, 0.0])]
    for eid, title, year, vec in rows:
        con.execute("INSERT INTO entries(id,title,short_id,year,source,text_raw) "
                    "VALUES (?,?,?,?,?,?)", (eid, title, eid, year, "ed.", "text"))
        for index in (0, 1):
            cid = f"{eid}#{index}"
            body = f"{title} Passage {index}"
            if eid != "U-3":
                text = f"{corpus_module.head_line(title, year)}\n{body}"
            elif index == 1:
                text = f"{corpus_module.head_line('Urkunde 2', 1450)}\n{body}"
            else:
                text = body
            con.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                        (cid, eid, index, 0, 10, text))
            con.execute("INSERT INTO embeddings VALUES (?,?,?,?)",
                        (cid, "test-model", 3, emb.pack(vec)))
    con.commit(); con.close()
    _db.set_db_path(path)
    return path


def test_passages_rank_by_cosine_similarity(tmp_path):
    _semantic_db(tmp_path)
    hits = _db.search_semantic([1.0, 0.0, 0.0], limit=3, model="test-model",
                               per_parent=1)
    assert [h["id"] for h in hits] == ["U-1", "U-2", "U-3"]
    assert hits[0]["score"] > hits[1]["score"]


def test_one_charter_cannot_fill_the_result_set(tmp_path):
    _semantic_db(tmp_path)
    hits = _db.search_semantic([1.0, 0.0, 0.0], limit=6, model="test-model",
                               per_parent=1)
    assert len({h["id"] for h in hits}) == len(hits)


def test_results_can_be_restricted_to_a_period(tmp_path):
    # For this corpus the period is often the point of the question.
    _semantic_db(tmp_path)
    hits = _db.search_semantic([1.0, 0.0, 0.0], limit=6, model="test-model",
                               year_from=1400, year_to=1500)
    assert {h["year"] for h in hits} == {1450}


def test_hits_carry_what_a_citation_needs(tmp_path):
    _semantic_db(tmp_path)
    hit = _db.search_semantic([1.0, 0.0, 0.0], limit=1, model="test-model")[0]
    for field in ("id", "chunk_id", "short_id", "year", "char_start", "score"):
        assert field in hit, field


def test_the_snippet_starts_with_the_text_not_the_head(tmp_path):
    # Fleet rule (ch-h-bot#555): the head `title year` is for the vector; the
    # hit carries title and year as fields, so the snippet is the passage
    # without its first line. Offsets never counted the head and stay.
    _semantic_db(tmp_path)
    hits = {h["chunk_id"]: h for h in _db.search_semantic(
        [1.0, 0.0, 0.0], limit=6, model="test-model", per_parent=2)}
    assert hits["U-1#0"]["snippet"] == "Urkunde 1 Passage 0"
    assert hits["U-2#1"]["snippet"] == "Urkunde 2 Passage 1"
    assert (hits["U-1#0"]["char_start"], hits["U-1#0"]["char_end"]) == (0, 10)


def test_only_this_entrys_own_head_is_cut(tmp_path):
    # A passage without a head (older index) is untouched, and so is one that
    # happens to begin with another entry's head: the cut uses the head of the
    # entry the chunk belongs to, built by the function that writes it.
    _semantic_db(tmp_path)
    hits = {h["chunk_id"]: h for h in _db.search_semantic(
        [0.0, 1.0, 0.0], limit=6, model="test-model", per_parent=2)}
    assert hits["U-3#0"]["snippet"] == "Urkunde 3 Passage 0"
    assert hits["U-3#1"]["snippet"] == "Urkunde 2 1450\nUrkunde 3 Passage 1"


def test_passage_body_cuts_exactly_the_head_line():
    head = corpus_module.head_line(" Hof ze Lind ", 1644)
    assert head == " Hof ze Lind  1644"                      # verbatim, no strip
    assert _db.passage_body(head + "\nDer hof", " Hof ze Lind ", 1644) == "Der hof"
    assert _db.passage_body(head + " Der hof", " Hof ze Lind ", 1644) == head + " Der hof"
    assert _db.passage_body("Der hof", " Hof ze Lind ", 1644) == "Der hof"
    assert _db.passage_body("\nDer hof", None, None) == "\nDer hof"   # no head, no cut


def test_a_dimension_mismatch_is_reported_not_silently_wrong(tmp_path):
    _semantic_db(tmp_path)
    with pytest.raises(ValueError, match="dimensions"):
        _db.search_semantic([1.0, 0.0], limit=1, model="test-model")


def test_an_unindexed_model_says_how_to_fix_it(tmp_path):
    _semantic_db(tmp_path)
    with pytest.raises(RuntimeError, match="embed_db"):
        _db.search_semantic([1.0, 0.0, 0.0], limit=1, model="never-run")


def test_warm_reports_size_rather_than_raising(tmp_path):
    _semantic_db(tmp_path)
    warm = _db.warm_semantic_index("test-model")
    assert warm["ready"] and warm["n_chunks"] == 6
    assert _db.warm_semantic_index("never-run")["ready"] is False


def test_semantic_stats_report_coverage(tmp_path):
    # The keys are the shared ones since #11: n_indexed / n_total, not
    # n_entries_indexed / n_entries_total. Nothing in the bot read the old
    # ones (ch-h-bot#555).
    _semantic_db(tmp_path)
    stats = _db.semantic_stats()
    assert stats["indexed"] and stats["coverage"] == 1.0
    assert stats["n_indexed"] == 3 and stats["n_total"] == 3
    assert stats["n_chunks"] == 6
    assert "n_entries_indexed" not in stats and "n_entries_total" not in stats


# ── The head and the reading are what the old embed_db.py produced ───────────
#
# Frozen on 22.10.2026 by running the embed_db.py of commit 71d2519 (the last
# with its own chunker) on this fixture; the shared embed_db.py with
# corpus.iter_documents/clean must write the same rows, or the 6,621 vectors
# in kf.db no longer belong to their passages. The fixture's vocabulary is
# six entries, so a token that is only ever followed by a raw form here
# ("druͥzehenhundert") counts as a fragment — that is the rule, applied to
# a small corpus, and the old code did the same.

_FREEZE_ENTRIES = [
    # (id, title, year, text_raw)
    ("AA_0001", "Urkunde 1350", 1350,
     "Diz ist dˀ der brief ✳ den Hein r₎ rich von Brugg ✳ un̄ und sin wip ✳ gaben ✳ "
     "der stif tˀin terin ze Kuͥngsvelden ✳ , dez jares ✳ do man zalt ✳ "
     "von gottes gebuͥrt ✳ druͥzehenhundert ✳ un̄ und fuͥnfzig jar ✳ . "
     "Hein r₎ rich ✳ hat gesworn ✳ ."),
    ("AA_0002", " Hof ze Lind ", 1644,                       # whitespace at the rim
     ("Der hof ze Lind ✳ gibt jaͤrlich ✳ zwen mütt kernen ✳ , un̄ und ein malter haber ✳ . " * 40)
     + "Hein r₎ rich ist der lechenman ✳ ."),
    ("AA_0003", None, 1401,                                  # no title
     "Ein brief ✳ ane titel ✳ von Brugg ✳ , un̄ und ane jar ✳ . Ze Brugg ✳ geben ✳ ."),
    ("AA_0004", "Ohne Jahr", None, "Diz ✳ hat kein jar ✳ un̄ und einen titel ✳ ."),
    ("AA_0005", "Leer", 1500, ""),                           # never chunked
    ("AA_0006", "Null", 1501, None),
]

# (chunk_id, parent_id, chunk_index, char_start, char_end, text) as the old
# embed_db.py wrote them. The four long passages of AA_0002 are frozen by
# offsets, length and the sha256 of their text.
_FROZEN_ROWS = [
    ('AA_0001#0', 'AA_0001', 0, 0, 186,
     'Urkunde 1350 1350\nDiz ist der brief den Heinrich von Brugg und sin wip gaben der '
     'stifterin ze Kuͥngsvelden, dez jares do man zalt von gottes gebuͥrt '
     'druͥzehenhundertund fuͥnfzig jar. Heinrich hat gesworn.'),
    ('AA_0003#0', 'AA_0003', 0, 0, 59,
     '1401\nEin brief ane titel von Brugg, und ane jar. Ze Brugg geben.'),
    ('AA_0004#0', 'AA_0004', 0, 0, 33,
     'Ohne Jahr\nDiz hat kein jar und einen titel.'),
]
_FROZEN_LONG = {   # chunk_id: (char_start, char_end, len(text), sha256(text)[:16])
    'AA_0002#0': (0, 980, 998, '77dd1f844cbe2ea7'),
    'AA_0002#1': (830, 1820, 1008, 'a96756a1a85a18e1'),
    'AA_0002#2': (1670, 2660, 1008, 'a96756a1a85a18e1'),
    'AA_0002#3': (2510, 2827, 336, '829ca9a573016c28'),
}
_FROZEN_LONG_HEAD = ' Hof ze Lind  1644'       # leading space, two spaces, no strip
_FROZEN_CLEAN = {
    'AA_0001': 'Diz ist der brief den Heinrich von Brugg und sin wip gaben der stifterin '
               'ze Kuͥngsvelden, dez jares do man zalt von gottes gebuͥrt '
               'druͥzehenhundertund fuͥnfzig jar. Heinrich hat gesworn.',
    'AA_0003': 'Ein brief ane titel von Brugg, und ane jar. Ze Brugg geben.',
    'AA_0004': 'Diz hat kein jar und einen titel.',
    'AA_0005': '',
    'AA_0006': '',
}
_FROZEN_CLEAN_LONG = ('AA_0002', 2827, 'da5f29d75b730138',
                      'Der hof ze Lind gibt jaͤrlich zwen mütt kernen, und ein malt',
                      'alter haber. Heinrich ist der lechenman.')


def _sha(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _freeze_db(entries=_FREEZE_ENTRIES):
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE entries(id TEXT PRIMARY KEY, title TEXT, short_id TEXT,
                             year INTEGER, source TEXT, pages TEXT, text_raw TEXT);
    """)
    con.executescript(vectorstore.SCHEMA_SQL)
    for eid, title, year, text in entries:
        con.execute("INSERT INTO entries(id,title,short_id,year,source,text_raw) "
                    "VALUES (?,?,?,?,?,?)", (eid, title, eid, year, "ed.", text))
    con.commit()
    return con


def test_the_shared_chunker_writes_the_rows_the_old_one_wrote():
    con = _freeze_db()
    assert embed_db.build_chunks(con, None, 1000, 150, True) == 7
    rows = {r[0]: tuple(r) for r in con.execute(
        "SELECT chunk_id, parent_id, chunk_index, char_start, char_end, text FROM chunks")}
    assert len(rows) == 7
    for frozen in _FROZEN_ROWS:
        assert rows[frozen[0]] == frozen, frozen[0]
    for cid, (start, end, length, sha) in _FROZEN_LONG.items():
        _, parent, _, s, e, text = rows[cid]
        assert parent == "AA_0002"
        assert (s, e, len(text), _sha(text)) == (start, end, length, sha), cid
        assert text.startswith(_FROZEN_LONG_HEAD + "\n"), cid


def test_the_head_is_verbatim_title_and_year():
    assert corpus_module.head_line("Urkunde 1350", 1350) == "Urkunde 1350 1350"
    assert corpus_module.head_line(" Hof ze Lind ", 1644) == _FROZEN_LONG_HEAD
    assert corpus_module.head_line(None, 1401) == "1401"
    assert corpus_module.head_line("Ohne Jahr", None) == "Ohne Jahr"
    assert corpus_module.head_line(None, None) == ""


def test_clean_is_the_old_reading_with_the_corpus_vocabulary():
    con = _freeze_db()
    # iter_documents builds the vocabulary over the whole table before its
    # first yield; clean() reads it.
    next(corpus_module.iter_documents(con))
    for eid, _, _, text in _FREEZE_ENTRIES:
        if eid in _FROZEN_CLEAN:
            assert corpus_module.clean(text) == _FROZEN_CLEAN[eid], eid
    eid, length, sha, head, tail = _FROZEN_CLEAN_LONG
    cleaned = corpus_module.clean(dict((e[0], e[3]) for e in _FREEZE_ENTRIES)[eid])
    assert (len(cleaned), _sha(cleaned)) == (length, sha)
    assert cleaned.startswith(head) and cleaned.endswith(tail)


def test_the_vocabulary_covers_the_whole_table_whatever_the_limit():
    # "Hein r₎ rich" in AA_0001 joins to "Heinrich" because "hein" is a
    # fragment everywhere in the corpus. With the vocabulary built over the
    # limited batch only it would still join here — so the counter-case is
    # a token whose evidence lies outside the batch: "ist" in AA_0001 is
    # followed by a raw form (dˀ) there and only there; with a whole-table
    # vocabulary it is a word (AA_0002 says so 40 times) and is not glued.
    con = _freeze_db()
    assert embed_db.build_chunks(con, 1, 1000, 150, True) == 1
    (text,) = con.execute("SELECT text FROM chunks WHERE chunk_id='AA_0001#0'").fetchone()
    assert text == _FROZEN_ROWS[0][5]
    assert "Diz ist der brief" in text and "istder" not in text


def test_iter_documents_skips_entries_that_have_chunks_and_empty_ones():
    con = _freeze_db()
    embed_db.build_chunks(con, None, 1000, 150, True)
    assert list(corpus_module.iter_documents(con)) == []
    con.execute("DELETE FROM chunks WHERE parent_id='AA_0003'")
    assert [d[0] for d in corpus_module.iter_documents(con)] == ["AA_0003"]


# ── The migration from the old index (chunks.entry_id) to the shared schema ──
#
# What kf.db looked like before #11, frozen from the old db.py: the parent
# column is entry_id, the runs table counts n_articles, the index is
# ix_chunks_entry. migrate_parent_id.py renames them in one transaction and
# nothing else; afterwards the shared embed_db.py finds every chunk with its
# vector and has nothing to do.

_OLD_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT    PRIMARY KEY,
    entry_id    TEXT    NOT NULL,
    chunk_index INTEGER NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    UNIQUE (entry_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT    PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    model    TEXT    NOT NULL,
    dims     INTEGER NOT NULL,
    vector   BLOB    NOT NULL
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


def _old_index_db(tmp_path, name="kf_old.db"):
    """kf.db as the old embed_db.py left it: a head on every passage, one
    vector per chunk, a finished run with n_articles."""
    path = str(tmp_path / name)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE entries(id TEXT PRIMARY KEY, title TEXT, short_id TEXT,
                             year INTEGER, source TEXT, pages TEXT, text_raw TEXT);
    """)
    con.executescript(_OLD_SCHEMA_SQL)
    rows = [("U-1", "Urkunde 1", 1360, [1.0, 0.0, 0.0]),
            ("U-2", "Urkunde 2", 1450, [0.8, 0.6, 0.0]),
            ("U-3", "Urkunde 3", 1644, [0.0, 1.0, 0.0])]
    for eid, title, year, vec in rows:
        con.execute("INSERT INTO entries(id,title,short_id,year,source,text_raw) "
                    "VALUES (?,?,?,?,?,?)", (eid, title, eid, year, "ed.", "text"))
        for index in (0, 1):
            cid = f"{eid}#{index}"
            con.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                        (cid, eid, index, 0, 10,
                         f"{corpus_module.head_line(title, year)}\n{title} Passage {index}"))
            con.execute("INSERT INTO embeddings VALUES (?,?,?,?)",
                        (cid, "test-model", 3, emb.pack(vec)))
    con.execute("INSERT INTO embedding_runs (run_id, started_at, finished_at, model, dims, "
                "n_articles, n_chunks) VALUES ('r1','2026-01-01T00:00:00Z',"
                "'2026-01-01T00:01:00Z','test-model',3,3,6)")
    con.commit(); con.close()
    return path


def _schema(path):
    con = sqlite3.connect(path)
    try:
        return {
            "chunks": [r[1] for r in con.execute("PRAGMA table_info(chunks)")],
            "runs": [r[1] for r in con.execute("PRAGMA table_info(embedding_runs)")],
            "indexes": sorted(r[1] for r in con.execute("PRAGMA index_list(chunks)")
                              if not r[1].startswith("sqlite_autoindex")),
        }
    finally:
        con.close()


def test_migration_renames_and_keeps_every_row(tmp_path):
    path = _old_index_db(tmp_path)
    assert _schema(path)["chunks"][1] == "entry_id"
    result = migrate_parent_id.migrate(path, quiet=True)
    assert result["integrity"] == "ok"
    assert result["before"] == result["after"] == {
        "chunks": 6, "embeddings": 6, "parents": 3, "runs": 1}
    schema = _schema(path)
    assert schema["chunks"][1] == "parent_id" and "entry_id" not in schema["chunks"]
    assert "n_parents" in schema["runs"] and "n_articles" not in schema["runs"]
    assert schema["indexes"] == ["ix_chunks_parent"]
    con = sqlite3.connect(path)
    assert con.execute("SELECT n_parents FROM embedding_runs").fetchone() == (3,)
    # The shared schema on top creates nothing twice (the reason for DROP INDEX).
    con.executescript(vectorstore.SCHEMA_SQL); con.commit(); con.close()
    assert _schema(path)["indexes"] == ["ix_chunks_parent"]


def test_after_migration_the_shared_embed_db_has_nothing_to_do(tmp_path, capsys):
    path = _old_index_db(tmp_path)
    migrate_parent_id.migrate(path, quiet=True)
    con = sqlite3.connect(path)
    con.executescript(vectorstore.SCHEMA_SQL)
    assert embed_db.build_chunks(con, None, 1000, 150, True) == 0
    assert embed_db.embed_pending(con, "test-model", 100, True) == (0, 0)
    con.close()
    assert embed_db.main(["--db", path, "--status"]) == 0
    out = capsys.readouterr().out
    assert "n_chunks: 6" in out and "n_indexed: 3" in out and "n_total: 3" in out
    # and the search goes through vectorstore on the renamed column
    _db.set_db_path(path)
    hits = _db.search_semantic([1.0, 0.0, 0.0], limit=3, model="test-model", per_parent=1)
    assert [h["id"] for h in hits] == ["U-1", "U-2", "U-3"]
    assert hits[0]["snippet"] == "Urkunde 1 Passage 0"
    assert _db.semantic_stats()["n_indexed"] == 3


def test_migration_refuses_a_migrated_or_foreign_database(tmp_path, capsys):
    import hashlib
    path = _old_index_db(tmp_path)
    assert migrate_parent_id.main(["--db", path]) == 0
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    # a second run says so and changes nothing
    assert migrate_parent_id.main(["--db", path]) == 1
    assert "already migrated" in capsys.readouterr().err
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == digest
    # a database built on the shared schema from the start
    fresh = str(tmp_path / "fresh.db")
    con = sqlite3.connect(fresh); con.executescript(vectorstore.SCHEMA_SQL); con.close()
    assert migrate_parent_id.main(["--db", fresh]) == 1
    assert "already migrated" in capsys.readouterr().err
    # a corpus without an index, and a missing file
    bare = str(tmp_path / "bare.db")
    con = sqlite3.connect(bare); con.execute("CREATE TABLE entries(id TEXT)"); con.close()
    assert migrate_parent_id.main(["--db", bare]) == 1
    assert "nothing to migrate" in capsys.readouterr().err
    assert migrate_parent_id.main(["--db", str(tmp_path / "none.db")]) == 1


def test_migration_refuses_a_half_renamed_database(tmp_path, capsys):
    # The old column renamed but the old index still there: neither schema.
    path = _old_index_db(tmp_path)
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE chunks RENAME COLUMN entry_id TO parent_id"); con.commit(); con.close()
    before = _schema(path)
    assert migrate_parent_id.main(["--db", path]) == 1
    err = capsys.readouterr().err
    assert "refusing" in err and "no column entry_id" in err and "already has parent_id" in err
    assert "n_articles" not in err and "ix_chunks" not in err      # those two are still fine
    assert _schema(path) == before


# ── The tool descriptions promise only what the database holds (#8) ──────────
#
# Measured on 14.10.2026 against kf.db (build of 30.09.2026): 0 of 5,260 persons
# have an hls_id, 237 of 1,333 places do (gnd_id 170, geo 384). The descriptions
# said «Returns HLS id» for persons, and a client got null for every one of them
# without a way to tell «missing» from «never there».

def _tool_docs():
    pytest.importorskip("mcp", reason="mcp SDK not installed")
    import anyio
    import server as server_module
    # The description keeps the docstring's line breaks; compare on single spaces.
    return {t.name: " ".join((t.description or "").split())
            for t in anyio.run(server_module.mcp.list_tools)}


def test_person_tools_do_not_promise_an_hls_id():
    docs = _tool_docs()
    for tool in ("search_persons", "get_person"):
        doc = docs[tool]
        assert "always null" in doc, f"{tool} must say hls_id is always null: {doc!r}"
        assert "0 of 5,260" in doc, f"{tool} must carry the measured count: {doc!r}"
        first_sentence = doc.split(". ")[0]
        assert "HLS id" not in first_sentence, f"{tool} still promises an HLS id: {first_sentence!r}"


def test_place_tools_say_the_ids_are_partial():
    docs = _tool_docs()
    for tool in ("search_places", "get_place"):
        assert "where the register has" in docs[tool], docs[tool]
        assert "237 of 1,333" in docs[tool], docs[tool]
    assert "gnd_id" in docs["get_place"] and "geo" in docs["get_place"]


def test_the_null_hls_id_still_ships_so_the_row_shape_is_stable():
    """The column stays (shape symmetry with places); the description is what changed."""
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture_db_for_shape(tmp)
        rows = db.search_persons("Habsburg")
        assert rows and all("hls_id" in r and r["hls_id"] is None for r in rows), rows


def _fixture_db_for_shape(tmp):
    import db as db_module
    from build_db import DDL, TRIGGERS
    path = f"{tmp}/kf.db"
    con = sqlite3.connect(path)
    con.executescript(DDL); con.executescript(TRIGGERS)
    con.execute(
        "INSERT INTO persons (id, forename, surname, full_name, main_name) VALUES (?,?,?,?,?)",
        ("per000001", "Agnes", "Habsburg", "Agnes von Habsburg", "Agnes von Habsburg"))
    con.commit(); con.close()
    db_module.set_db_path(path)
    return db_module
