#!/usr/bin/env python3
"""
migrate_parent_id.py — rename kf.db's index columns to the shared schema (#11).

    python migrate_parent_id.py --db /home/tobias/mcp/data/kf.db

The index built by the old embed_db.py names the parent column `chunks.entry_id`
and the run counter `embedding_runs.n_articles`; the shared embed_db.py and
vectorstore.py (ch-h-bot#555) say `parent_id` and `n_parents`. Nothing else
changes: every chunk keeps its id, offsets, text and vector, so nothing is
re-embedded. One transaction:

    ALTER TABLE chunks RENAME COLUMN entry_id TO parent_id;
    ALTER TABLE embedding_runs RENAME COLUMN n_articles TO n_parents;
    DROP INDEX ix_chunks_entry;
    CREATE INDEX ix_chunks_parent ON chunks(parent_id);

then PRAGMA integrity_check. DROP INDEX is needed: RENAME COLUMN re-points the
old index but keeps its name, and vectorstore.SCHEMA_SQL would then create a
second, identical one (SQLite cannot rename an index).

Idempotent in the sense that it refuses cleanly: a database already on the
shared schema, or one in any state between, is left untouched and named.

THIS SCRIPT MAKES NO BACKUP. The operator does, before running it, and the
server is stopped while it runs (the old db.py asks for `entry_id`, so a
running old server would answer every search_semantic with «no such column»):

    systemctl --user stop mcp-kf
    cp /home/tobias/mcp/data/kf.db /home/tobias/mcp/data/kf.db.pre-parent-id
    python migrate_parent_id.py --db /home/tobias/mcp/data/kf.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

OLD_COLUMN, NEW_COLUMN = "entry_id", "parent_id"
OLD_RUNS_COLUMN, NEW_RUNS_COLUMN = "n_articles", "n_parents"
OLD_INDEX, NEW_INDEX = "ix_chunks_entry", "ix_chunks_parent"

MIGRATION_SQL = f"""
ALTER TABLE chunks RENAME COLUMN {OLD_COLUMN} TO {NEW_COLUMN};
ALTER TABLE embedding_runs RENAME COLUMN {OLD_RUNS_COLUMN} TO {NEW_RUNS_COLUMN};
DROP INDEX {OLD_INDEX};
CREATE INDEX {NEW_INDEX} ON chunks({NEW_COLUMN});
"""


class NotMigratable(Exception):
    """The database is not in the old schema; nothing was changed."""


def _columns(conn, table) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _indexes(conn, table) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA index_list({table})")]


def counts(conn, parent_column: str) -> dict:
    """What has to be the same before and after: rows, vectors, parents."""
    return {
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "embeddings": conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        "parents": conn.execute(
            f"SELECT COUNT(DISTINCT {parent_column}) FROM chunks").fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM embedding_runs").fetchone()[0],
    }


def check_old_schema(conn) -> None:
    """Raise NotMigratable unless every part of the old schema is in place."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"chunks", "embeddings", "embedding_runs"} - tables
    if missing:
        raise NotMigratable(f"no semantic index here (missing {sorted(missing)}); "
                            "nothing to migrate")
    chunk_cols, run_cols, idx = _columns(conn, "chunks"), _columns(conn, "embedding_runs"), \
        _indexes(conn, "chunks")
    if NEW_COLUMN in chunk_cols and OLD_COLUMN not in chunk_cols \
            and NEW_RUNS_COLUMN in run_cols and NEW_INDEX in idx and OLD_INDEX not in idx:
        raise NotMigratable("already migrated: chunks.parent_id, embedding_runs.n_parents "
                            "and ix_chunks_parent are in place")
    problems = []
    if OLD_COLUMN not in chunk_cols:
        problems.append(f"chunks has no column {OLD_COLUMN} (columns: {chunk_cols})")
    if NEW_COLUMN in chunk_cols:
        problems.append(f"chunks already has {NEW_COLUMN}")
    if OLD_RUNS_COLUMN not in run_cols:
        problems.append(f"embedding_runs has no column {OLD_RUNS_COLUMN} (columns: {run_cols})")
    if NEW_RUNS_COLUMN in run_cols:
        problems.append(f"embedding_runs already has {NEW_RUNS_COLUMN}")
    if OLD_INDEX not in idx:
        problems.append(f"no index {OLD_INDEX} on chunks (indexes: {idx})")
    if NEW_INDEX in idx:
        problems.append(f"index {NEW_INDEX} already exists")
    if problems:
        raise NotMigratable("not the old schema, refusing: " + "; ".join(problems))


def migrate(path: str, quiet: bool = False) -> dict:
    """Run the migration on ``path``. Returns the counts before and after.

    Raises NotMigratable without touching the file when the schema is not the
    old one, and RuntimeError when the counts or the integrity check disagree
    after the rename (the transaction is then already committed; the operator's
    backup is the way back).
    """
    if not os.path.exists(path):
        raise NotMigratable(f"database not found at {path}")
    # isolation_level=None: sqlite3 would otherwise open no transaction for
    # DDL, and the four statements have to stand or fall together.
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        check_old_schema(conn)
        before = counts(conn, OLD_COLUMN)
        if not quiet:
            print(f"before: {before}")
        conn.execute("BEGIN")
        try:
            for statement in MIGRATION_SQL.strip().split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        after = counts(conn, NEW_COLUMN)
        if not quiet:
            print(f"after:  {after}")
            print(f"integrity_check: {integrity}")
        if integrity != "ok":
            raise RuntimeError(f"integrity_check after migration: {integrity}")
        if after != before:
            raise RuntimeError(f"counts changed: before {before}, after {after}")
        return {"before": before, "after": after, "integrity": integrity}
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("KF_DB", "/data/kf.db"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        migrate(args.db, quiet=args.quiet)
    except NotMigratable as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        print(f"migrated {args.db}: chunks.{OLD_COLUMN} -> {NEW_COLUMN}, "
              f"embedding_runs.{OLD_RUNS_COLUMN} -> {NEW_RUNS_COLUMN}, "
              f"{OLD_INDEX} -> {NEW_INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
