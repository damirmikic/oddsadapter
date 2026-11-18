"""Column renames for the SQLite stores (clv, price history, marks), applied when a store opens an older file."""
from __future__ import annotations


def rename_columns(db, table, renames):
    """Rename `{old: new}` columns of `table` that still carry the old name (SQLite 3.25+)."""
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for old, new in renames.items():
        if old in columns and new not in columns:
            db.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
