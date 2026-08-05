# Königsfelden — MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes the Königsfelden corpus —
the records of the Cistercian convent and the Hofmeisterei Königsfelden (1300–1658),
edited by *Die Urkunden und Akten des Klosters und der Hofmeisterei Königsfelden* —
to Claude and other MCP-compatible clients.

## Architecture

```
data/docs/*.xml       (TEI, one file per register entry)  ─┐
data/registers/                                            ├─► build_db.py ──► kf.db
  people.xml  places.xml  organizations.xml               ─┘                  (SQLite + FTS5)
                                                                                   │
                                                                              server.py
                                                                    (mcp 2.0 MCPServer / HTTP SSE)
                                                                                   │
                                                                    http://<host>:8001/sse
```

The TEI sources are parsed once into a SQLite database with two FTS5 indexes. The server
then runs stateless read-only queries against it (`PRAGMA query_only`).

The server targets **mcp 2.0**, which renamed the high-level server class
(`FastMCP` → `MCPServer`), removed `mcp.server.fastmcp`, and moved the bind address from
the constructor into `run()`; `requirements.txt` pins the major version accordingly. The
SSE endpoint is unchanged, so deployed clients keep working.

Entity identifiers come from the TEI `xml:id` attributes — persons `perXXXXXX`, places
`locXXXXXX`, organisations `orgXXXX`. Person and place records additionally carry HLS
identifiers (and GND, for places) where the edition supplies them.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Build the database

```bash
python build_db.py --docs ../data/docs --registers ../data/registers --db kf.db
```

`--docs` is a directory of per-entry TEI files; the entry id is the **filename** without
its extension. `--registers` must contain `people.xml`, `places.xml`, and
`organizations.xml`. Both default to `../data/docs` and `../data/registers`; `--batch`
(default 200) controls the commit batch size.

Run it once, and again whenever the TEI changes. **Rebuilding is destructive**: the five
tables the script owns (`entries`, `spans`, `persons`, `places`, `orgs`) and both FTS
indexes are cleared and repopulated, so a rebuild always mirrors the current sources
rather than accumulating duplicates. It prints `Existing database: clearing N entries`
when it does this. Nothing else in the file is touched.

Malformed records are skipped individually and reported on stderr as
`WARNING: N record(s) skipped` — check for that line, since the build otherwise
completes normally.

### 3. Start the server

```bash
python server.py --db kf.db --host 0.0.0.0 --port 8001
```

Each flag also has an environment variable — `KF_DB`, `KF_HOST`, `KF_PORT` — which the
flags override. Importing `server.py` never reads `sys.argv`, so it is safe to import
from tests or an ASGI loader.

### 4. Connect a client

Add to your `claude_desktop_config.json` (or equivalent):

```json
{
  "mcpServers": {
    "kf": {
      "url": "http://<server-ip>:8001/sse"
    }
  }
}
```

Or for Claude Code:

```bash
claude mcp add kf --transport sse --url http://<server-ip>:8001/sse
```

---

## Docker deployment

### Build image

```bash
docker compose build
```

### First-time: build the database

Copy the TEI sources onto the server (the compose file mounts `/home/dh/kf_data` as
`/data`), then:

```bash
docker run --rm -v /home/dh/kf_data:/data kf-mcp python build_db.py --docs /data/kf_raw/docs --registers /data/kf_raw/registers --db /data/kf.db
```

### Run

```bash
docker compose up -d
```

The container serves on port 8001 and expects `kf.db` at `/data/kf.db`. Adjust the volume
path in `docker-compose.yml` if your data lives elsewhere.

### Reverse proxy (nginx, optional but recommended)

```nginx
server {
    listen 443 ssl;
    server_name kf-mcp.example.unibe.ch;

    location / {
        proxy_pass         http://localhost:8001;
        proxy_http_version 1.1;
        # Required for SSE
        proxy_set_header   Connection '';
        proxy_buffering    off;
        proxy_cache        off;
        chunked_transfer_encoding on;
    }
}
```

> **Note:** the server has no authentication. By default `docker-compose.yml` publishes
> port 8001 on all interfaces; if a proxy fronts it, bind it to loopback instead so the
> corpus is not reachable directly:
>
> ```bash
> KF_BIND=127.0.0.1 docker compose up -d
> ```
>
> Otherwise restrict access at the firewall.

---

## Available tools

| Tool | Description |
|------|-------------|
| `corpus_stats()` | Entry/span/person/place/org counts and year range |
| `list_entries(limit=50, offset=0)` | Paginated list of register entries, ordered by year |
| `get_entry(entry_id)` | Full entry: title, year, source, pages, transcription, all spans |
| `search_persons(query, limit=50)` | Person authority file by name (substring match) |
| `get_person(pid)` | Person record with HLS id, occupation, life dates, and mentions |
| `search_places(query, limit=50)` | Place authority file by name (German or French) |
| `get_place(pid)` | Place record with geo, HLS id, GND id, and mentions |
| `search_orgs(query, limit=50)` | Organisation authority file by name or description |
| `search_fulltext(query, limit=20)` | Full-text search over transcriptions, with snippets |
| `get_entries_for_person(pid, limit=50)` | Entries mentioning a person, by authority id |
| `get_entries_for_place(pid, limit=50)` | Entries mentioning a place, by authority id |
| `get_entries_by_year(year_from, year_to, limit=100)` | Entries in a year range (max span 300 years) |

## Available resources

| URI | Description |
|-----|-------------|
| `kf://stats` | Corpus statistics (JSON) |
| `kf://persons` | Person index — `{total, returned, truncated, persons: [...]}`, capped at 9999 rows and flagged when truncated |
| `kf://entry/{entry_id}` | Single entry (JSON) |

## Query behaviour

**Limits.** Every `limit` is clamped to at most 500; a negative, zero, or non-numeric
value falls back to that tool's own default rather than returning the whole table. Use
`list_entries(limit, offset)` to page through the full corpus.

**Full-text search.** `search_fulltext` passes the query to FTS5, so operators work —
`Brugg OR Königsfelden`, `Heinr*`, `NEAR(...)`. If the query isn't valid FTS5 syntax
(a stray quote, a dangling `AND`), it silently falls back to a literal word search
instead of erroring. Only a query with no usable words returns `{"error": ...}`.

**Name search.** `search_persons`, `search_places`, and `search_orgs` do a plain
case-insensitive substring match. SQL wildcards in the query are escaped, so searching
for `100%` finds a literal "100%" rather than matching every record.

**Missing records.** `get_entry`, `get_person`, and `get_place` return
`{"error": "... not found."}` rather than raising.

**Spans.** `get_entry` returns every span in the entry — `persName`, `placeName`,
`orgName`, `date`, `measure`. The `ref` field holds the authority id and is empty for
unlinked mentions and for dates/measures; `norm` holds the normalised `@when` or
`@quantity` value.

**How entry years are assigned.** The year comes from the first `<date when="...">` in the
document `<body>`. If the body has no date, the `<sourceDesc>` in the header is used as a
fallback. Dates in `publicationStmt` or `revisionDesc` are never used — they describe the
edition, not the charter. Years outside 1000–1800 are ignored, and entries with no usable
date have `year = NULL`.

## Database schema

| Table | Contents |
|-------|----------|
| `entries` | id, title, short_id, year, source, pages, text_raw |
| `spans` | entry_id, span_id, class, ref, text, norm |
| `persons` | id, forename, surname, full_name, main_name, occupation, birth, death, org_ref, hls_id, note |
| `places` | id, name_de, name_fr, country, region, geo, hls_id, gnd_id, place_type |
| `orgs` | id, name, desc_de, desc_fr |
| `fts_entries`, `fts_spans` | FTS5 indexes (external content, populated by AFTER INSERT triggers at build time — there are no update/delete triggers, which is why a rebuild clears and repopulates) |

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest test_kf_mcp.py
```

Unit tests (TEI parsing, authority registers, rebuild idempotency) run with no setup.
The DB and server tests skip unless you point them at a built database and a running
server:

```bash
KF_DB=/home/dh/kf_data/kf.db KF_SERVER=http://localhost:8001 pytest test_kf_mcp.py
```

The suite also runs standalone, with grouped output and a non-zero exit on failure:

```bash
python test_kf_mcp.py --unit --db /home/dh/kf_data/kf.db --server http://localhost:8001
```

Note that the DB tests assert corpus-size floors (≥1550 entries, ≥5000 persons, ≥1300
places, ≥2000 orgs) — they will fail against a small sample database.
