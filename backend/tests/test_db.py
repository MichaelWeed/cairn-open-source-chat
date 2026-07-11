from pathlib import Path

from app.db import bootstrap, run_migrations


def test_bootstrap_enables_wal(tmp_path: Path) -> None:
    conn = bootstrap(tmp_path / "test.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()


def test_bootstrap_creates_parent_dir(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "test.db"
    conn = bootstrap(db_path)
    assert db_path.exists()
    conn.close()


def test_run_migrations_records_versions(tmp_path: Path) -> None:
    conn = bootstrap(tmp_path / "test.db")
    versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    assert 1 in versions
    conn.close()


def test_run_migrations_is_idempotent(tmp_path: Path) -> None:
    conn = bootstrap(tmp_path / "test.db")
    assert run_migrations(conn) == []
    conn.close()
