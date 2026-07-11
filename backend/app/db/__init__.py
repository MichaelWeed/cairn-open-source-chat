"""SQLite (WAL) bootstrap and a numbered-SQL-file migration runner.

Migrations live in app/db/migrations/<NNNN>_<name>.sql, applied in order
inside a transaction each, tracked in schema_migrations. No ORM: this is
metadata/config storage (per DEVELOPER_README.md §1), not the vector store.
"""

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_FILENAME = re.compile(r"^(\d{4})_.+\.sql$")


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = _MIGRATION_FILENAME.match(path.name)
        if not match:
            continue
        files.append((int(match.group(1)), path))
    return sorted(files)


def run_migrations(conn: sqlite3.Connection) -> list[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    newly_applied = []
    for version, path in _migration_files():
        if version in applied:
            continue
        with conn:
            conn.executescript(path.read_text())
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        newly_applied.append(version)
    return newly_applied


def bootstrap(database_path: Path) -> sqlite3.Connection:
    conn = connect(database_path)
    run_migrations(conn)
    return conn
