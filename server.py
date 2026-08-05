"""server.py — Königsfelden MCP server (mcp 2.0 MCPServer, HTTP SSE)."""
import argparse, json, logging, os
from mcp.server.mcpserver import MCPServer
import db as db_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Defaults come from the environment so that importing this module never touches
# sys.argv — argparse at import time would hijack the arguments of any process that
# imports the server (tests, an ASGI loader). The CLI overrides these in main().
DEFAULT_DB   = os.environ.get("KF_DB", "/data/kf.db")
DEFAULT_HOST = os.environ.get("KF_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("KF_PORT", "8001"))
SSE_PATH     = "/sse"

db_module.set_db_path(DEFAULT_DB)

# mcp 2.0 renamed FastMCP to MCPServer and moved the bind address out of the
# constructor: host and port are arguments to run(), not server settings.
mcp = MCPServer(
    name="Königsfelden",
    version="1.0.0",
    instructions=(
        "Königsfelden monastic records (1300–1658), edited by the project "
        "'Die Urkunden und Akten des Klosters und der Hofmeisterei Königsfelden'. "
        "Covers the Cistercian convent and the Hofmeisterei. "
        "Persons use HLS identifiers (perXXXXXX), places use locXXXXXX. "
        "Use get_entry for a document; search_persons/search_places for authority lookups; "
        "search_fulltext for keyword search across transcriptions."
    ),
)

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def corpus_stats() -> dict:
    """High-level counts for the Königsfelden corpus."""
    return db_module.stats()

@mcp.tool()
def list_entries(limit: int = 50, offset: int = 0) -> list[dict]:
    """Paginated list of all register entries, newest first."""
    return db_module.list_entries(limit, offset)

@mcp.tool()
def get_entry(entry_id: str) -> dict:
    """Full entry: title, year, source, and all named-entity spans."""
    result = db_module.get_entry(entry_id)
    if not result:
        return {"error": f"Entry '{entry_id}' not found."}
    return result

@mcp.tool()
def search_persons(query: str, limit: int = 50) -> list[dict]:
    """Search the person authority file by name. Returns HLS id, occupation, life dates."""
    return db_module.search_persons(query, limit)

@mcp.tool()
def get_person(pid: str) -> dict:
    """Person authority record with HLS id, occupation, birth/death, and all entry mentions."""
    result = db_module.get_person(pid)
    if not result:
        return {"error": f"Person '{pid}' not found."}
    return result

@mcp.tool()
def search_places(query: str, limit: int = 50) -> list[dict]:
    """Search the place authority file by name. Returns HLS id, geo coordinates, type."""
    return db_module.search_places(query, limit)

@mcp.tool()
def get_place(pid: str) -> dict:
    """Place authority record with geo, HLS id, GND id, and all entry mentions."""
    result = db_module.get_place(pid)
    if not result:
        return {"error": f"Place '{pid}' not found."}
    return result

@mcp.tool()
def search_orgs(query: str, limit: int = 50) -> list[dict]:
    """Search the organisation authority file by name."""
    return db_module.search_orgs(query, limit)

@mcp.tool()
def search_fulltext(query: str, limit: int = 20) -> list[dict]:
    """Full-text search across all document transcriptions. Returns snippets with highlights."""
    return db_module.search_fulltext(query, limit)

@mcp.tool()
def get_entries_for_person(pid: str, limit: int = 50) -> list[dict]:
    """All register entries that mention a given person (by authority id)."""
    return db_module.get_entries_for_person(pid, limit)

@mcp.tool()
def get_entries_for_place(pid: str, limit: int = 50) -> list[dict]:
    """All register entries that mention a given place (by authority id)."""
    return db_module.get_entries_for_place(pid, limit)

@mcp.tool()
def get_entries_by_year(year_from: int, year_to: int, limit: int = 100) -> list[dict]:
    """All entries within a given year range (inclusive)."""
    if year_to < year_from:
        return [{"error": "year_to must be >= year_from"}]
    if year_to - year_from > 300:
        return [{"error": "Year range too large; max 300 years."}]
    return db_module.get_entries_by_year(year_from, year_to, limit)

# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("kf://stats")
def resource_stats() -> str:
    return json.dumps(db_module.stats(), indent=2)

@mcp.resource("kf://persons")
def resource_persons() -> str:
    """Brief person index: id, name, occupation, HLS id. Flags its own truncation."""
    return json.dumps(db_module.person_index(), indent=2, ensure_ascii=False)

@mcp.resource("kf://entry/{entry_id}")
def resource_entry(entry_id: str) -> str:
    result = db_module.get_entry(entry_id)
    if not result:
        return json.dumps({"error": f"Entry '{entry_id}' not found."})
    return json.dumps(result, indent=2, ensure_ascii=False)

# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Königsfelden MCP server")
    ap.add_argument("--db",   default=DEFAULT_DB,   help="Path to kf.db (env KF_DB)")
    ap.add_argument("--host", default=DEFAULT_HOST, help="Bind address (env KF_HOST)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port (env KF_PORT)")
    return ap.parse_args(argv)

def main(argv=None):
    args = parse_args(argv)
    db_module.set_db_path(args.db)

    logger.info(f"Database: {args.db}")
    try:
        s = db_module.stats()
        logger.info(f"Corpus: {s['n_entries']:,} entries, {s['n_persons']:,} persons, {s['n_places']:,} places")
    except Exception as e:
        logger.warning(f"Could not read DB stats: {e}")
    logger.info(f"Starting KF MCP server on {args.host}:{args.port}{SSE_PATH}")
    mcp.run(transport="sse", host=args.host, port=args.port, sse_path=SSE_PATH)

if __name__ == "__main__":
    main()
