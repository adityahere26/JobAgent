"""SQLite connection and schema migrations (tracked with PRAGMA user_version)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS boards (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        token TEXT NOT NULL,
        company TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run_at TEXT,
        last_status TEXT,
        UNIQUE (source, token)
    );

    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY,
        board_id INTEGER NOT NULL REFERENCES boards(id),
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        seen INTEGER NOT NULL DEFAULT 0,
        inserted INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0,
        unchanged INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        expired INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY,
        board_id INTEGER NOT NULL REFERENCES boards(id),
        external_id TEXT NOT NULL,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        department TEXT,
        location_text TEXT,
        cities TEXT NOT NULL DEFAULT '[]',
        countries TEXT NOT NULL DEFAULT '[]',
        is_remote INTEGER NOT NULL DEFAULT 0,
        description TEXT NOT NULL DEFAULT '',
        apply_url TEXT NOT NULL,
        posted_at TEXT,
        source_updated_at TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expired_at TEXT,
        content_hash TEXT NOT NULL,
        UNIQUE (board_id, external_id)
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_active
        ON jobs (posted_at DESC) WHERE expired_at IS NULL;
    CREATE INDEX IF NOT EXISTS idx_jobs_seen
        ON jobs (board_id, last_seen_at) WHERE expired_at IS NULL;

    CREATE TABLE IF NOT EXISTS scores (
        job_id INTEGER NOT NULL REFERENCES jobs(id),
        method TEXT NOT NULL,
        score INTEGER NOT NULL,
        reasons TEXT NOT NULL,
        red_flags TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        profile_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (job_id, method)
    );

    CREATE TABLE IF NOT EXISTS applications (
        job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
        status TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    """,
}


def now_iso() -> str:
    """Current UTC time as a sortable ISO string. All stored timestamps use this format."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError("This database was created by a newer jobagent. Please upgrade.")
    for target in range(version + 1, SCHEMA_VERSION + 1):
        conn.executescript(_MIGRATIONS[target])  # idempotent statements; commits implicitly
        conn.execute(f"PRAGMA user_version = {int(target)}")
    conn.commit()


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn
