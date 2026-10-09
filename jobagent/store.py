"""All SQLite reads and writes. Callers pass a connection; each board is one transaction."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jobagent.config import BoardConfig
from jobagent.db import now_iso
from jobagent.models import JobIn

STATUSES = ("saved", "applied", "interview", "offer", "rejected", "skipped")
STALE_DAYS = 30


@dataclass
class Job:
    id: int
    board_id: int
    external_id: str
    title: str
    company: str
    department: str | None
    location_text: str | None
    cities: list[str]
    countries: list[str]
    is_remote: bool
    description: str
    apply_url: str
    posted_at: str | None
    content_hash: str
    first_seen_at: str


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        board_id=row["board_id"],
        external_id=row["external_id"],
        title=row["title"],
        company=row["company"],
        department=row["department"],
        location_text=row["location_text"],
        cities=json.loads(row["cities"]),
        countries=json.loads(row["countries"]),
        is_remote=bool(row["is_remote"]),
        description=row["description"],
        apply_url=row["apply_url"],
        posted_at=row["posted_at"],
        content_hash=row["content_hash"],
        first_seen_at=row["first_seen_at"],
    )


def sync_boards(conn: sqlite3.Connection, boards: list[BoardConfig]) -> None:
    """Make the boards table match the config. Removed boards are disabled, never deleted."""
    with conn:
        wanted = set()
        for b in boards:
            wanted.add((b.source, b.token))
            conn.execute(
                "INSERT INTO boards (source, token, company, enabled) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (source, token) DO UPDATE SET company = excluded.company, "
                "enabled = excluded.enabled",
                (b.source, b.token, b.company, int(b.enabled)),
            )
        for row in conn.execute("SELECT id, source, token FROM boards").fetchall():
            if (row["source"], row["token"]) not in wanted:
                conn.execute("UPDATE boards SET enabled = 0 WHERE id = ?", (row["id"],))


def enabled_boards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM boards WHERE enabled = 1 ORDER BY id").fetchall()


def upsert_job(conn: sqlite3.Connection, board_id: int, job: JobIn, seen_at: str) -> str:
    """Insert or update one job. Returns 'inserted', 'updated' or 'unchanged'."""
    row = conn.execute(
        "SELECT id, content_hash FROM jobs WHERE board_id = ? AND external_id = ?",
        (board_id, job.external_id),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO jobs (board_id, external_id, title, company, department, location_text, "
            "cities, countries, is_remote, description, apply_url, posted_at, source_updated_at, "
            "first_seen_at, last_seen_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                board_id,
                job.external_id,
                job.title,
                job.company,
                job.department,
                job.location_text,
                json.dumps(job.cities),
                json.dumps(job.countries),
                int(job.is_remote),
                job.description,
                job.apply_url,
                job.posted_at,
                job.source_updated_at,
                seen_at,
                seen_at,
                job.content_hash,
            ),
        )
        return "inserted"
    if row["content_hash"] == job.content_hash:
        conn.execute(
            "UPDATE jobs SET last_seen_at = ?, expired_at = NULL, source_updated_at = ? "
            "WHERE id = ?",
            (seen_at, job.source_updated_at, row["id"]),
        )
        return "unchanged"
    conn.execute(
        "UPDATE jobs SET title = ?, company = ?, department = ?, location_text = ?, cities = ?, "
        "countries = ?, is_remote = ?, description = ?, apply_url = ?, posted_at = ?, "
        "source_updated_at = ?, last_seen_at = ?, expired_at = NULL, content_hash = ? "
        "WHERE id = ?",
        (
            job.title,
            job.company,
            job.department,
            job.location_text,
            json.dumps(job.cities),
            json.dumps(job.countries),
            int(job.is_remote),
            job.description,
            job.apply_url,
            job.posted_at,
            job.source_updated_at,
            seen_at,
            job.content_hash,
            row["id"],
        ),
    )
    return "updated"


def touch_job(conn: sqlite3.Connection, board_id: int, external_id: str, seen_at: str) -> None:
    """A listing we could not parse is still on the board: keep the stored copy alive."""
    conn.execute(
        "UPDATE jobs SET last_seen_at = ? WHERE board_id = ? AND external_id = ?",
        (seen_at, board_id, external_id),
    )


def active_count(conn: sqlite3.Connection, board_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE board_id = ? AND expired_at IS NULL", (board_id,)
    ).fetchone()[0]


def expire_unseen(conn: sqlite3.Connection, board_id: int, run_started: str) -> int:
    """Mark jobs not seen in this complete run as expired."""
    cur = conn.execute(
        "UPDATE jobs SET expired_at = ? WHERE board_id = ? AND expired_at IS NULL "
        "AND last_seen_at < ?",
        (now_iso(), board_id, run_started),
    )
    return cur.rowcount


def expire_stale(conn: sqlite3.Connection, days: int = STALE_DAYS) -> int:
    """Backstop: anything not seen for `days` days is expired even if runs never succeeded."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="microseconds")
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET expired_at = ? WHERE expired_at IS NULL AND last_seen_at < ?",
            (now_iso(), cutoff),
        )
    return cur.rowcount


def record_run(
    conn: sqlite3.Connection,
    board_id: int,
    started_at: str,
    status: str,
    error: str | None,
    counts: dict[str, int],
) -> None:
    finished = now_iso()
    conn.execute(
        "INSERT INTO runs (board_id, started_at, finished_at, status, error, seen, inserted, "
        "updated, unchanged, skipped, expired) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            board_id,
            started_at,
            finished,
            status,
            error,
            counts.get("seen", 0),
            counts.get("inserted", 0),
            counts.get("updated", 0),
            counts.get("unchanged", 0),
            counts.get("skipped", 0),
            counts.get("expired", 0),
        ),
    )
    conn.execute(
        "UPDATE boards SET last_run_at = ?, last_status = ? WHERE id = ?",
        (finished, status, board_id),
    )


def load_active_jobs(conn: sqlite3.Connection) -> list[Job]:
    rows = conn.execute(
        "SELECT j.* FROM jobs j JOIN boards b ON b.id = j.board_id "
        "WHERE j.expired_at IS NULL AND b.enabled = 1 ORDER BY j.id"
    ).fetchall()
    return [_job_from_row(r) for r in rows]


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_from_row(row) if row else None


def save_score(
    conn: sqlite3.Connection,
    job_id: int,
    method: str,
    score: int,
    reasons: list[str],
    red_flags: list[str],
    content_hash: str,
    profile_hash: str,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO scores (job_id, method, score, reasons, red_flags, content_hash, "
            "profile_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (job_id, method) DO UPDATE SET score = excluded.score, "
            "reasons = excluded.reasons, red_flags = excluded.red_flags, "
            "content_hash = excluded.content_hash, profile_hash = excluded.profile_hash, "
            "created_at = excluded.created_at",
            (
                job_id,
                method,
                score,
                json.dumps(reasons),
                json.dumps(red_flags),
                content_hash,
                profile_hash,
                now_iso(),
            ),
        )


def get_score(
    conn: sqlite3.Connection,
    job_id: int,
    method: str,
    content_hash: str | None = None,
    profile_hash: str | None = None,
) -> dict | None:
    """A stored score, or None if missing or stale (hashes given and different)."""
    row = conn.execute(
        "SELECT * FROM scores WHERE job_id = ? AND method = ?", (job_id, method)
    ).fetchone()
    if row is None:
        return None
    if content_hash is not None and row["content_hash"] != content_hash:
        return None
    if profile_hash is not None and row["profile_hash"] != profile_hash:
        return None
    return {
        "score": row["score"],
        "reasons": json.loads(row["reasons"]),
        "red_flags": json.loads(row["red_flags"]),
    }


def set_status(conn: sqlite3.Connection, job_id: int, status: str, note: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    if get_job(conn, job_id) is None:
        raise ValueError(f"no job with id {job_id}")
    with conn:
        conn.execute(
            "INSERT INTO applications (job_id, status, note, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (job_id) DO UPDATE SET status = excluded.status, note = excluded.note, "
            "updated_at = excluded.updated_at",
            (job_id, status, note, now_iso()),
        )


def get_statuses(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        r["job_id"]: r["status"] for r in conn.execute("SELECT job_id, status FROM applications")
    }
