"""Fetch every enabled board and store the jobs, expiring missing ones only after a trusted run."""

import sqlite3
from dataclasses import dataclass

from pydantic import ValidationError

from jobagent import store
from jobagent.db import now_iso
from jobagent.models import JobIn
from jobagent.sources import ADAPTERS, SourceError


@dataclass
class BoardOutcome:
    company: str
    status: str
    counts: dict[str, int]
    error: str | None = None


def ingest_board(conn: sqlite3.Connection, board: sqlite3.Row) -> BoardOutcome:
    started = now_iso()
    counts = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0, "expired": 0}
    adapter_cls = ADAPTERS.get(board["source"])
    if adapter_cls is None:
        with conn:
            store.record_run(conn, board["id"], started, "failed", "unknown source", counts)
        return BoardOutcome(board["company"], "failed", counts, "unknown source")
    adapter = adapter_cls()

    try:
        result = adapter.fetch(board["token"])
    except SourceError as exc:
        with conn:
            store.record_run(conn, board["id"], started, "failed", str(exc), counts)
        return BoardOutcome(board["company"], "failed", counts, str(exc))

    trusted = result.complete
    prior_active = store.active_count(conn, board["id"])
    with conn:
        for raw in result.listings:
            counts["seen"] += 1
            ext_id = adapter.external_id(raw)
            if ext_id is None:
                trusted = False  # cannot tell which stored job this was
                counts["skipped"] += 1
                continue
            try:
                job = JobIn.model_validate(adapter.to_job_fields(raw, board["company"]))
            except (ValidationError, ValueError, TypeError):
                counts["skipped"] += 1
                store.touch_job(conn, board["id"], ext_id, started)
                continue
            counts[store.upsert_job(conn, board["id"], job, started)] += 1

        if not result.listings and prior_active > 0:
            trusted = False  # a board that suddenly shows nothing is suspect

        status = "success" if trusted else "partial"
        if trusted:
            counts["expired"] = store.expire_unseen(conn, board["id"], started)
        store.record_run(conn, board["id"], started, status, None, counts)
    return BoardOutcome(board["company"], status, counts)


def ingest_all(conn: sqlite3.Connection) -> list[BoardOutcome]:
    outcomes = [ingest_board(conn, b) for b in store.enabled_boards(conn)]
    store.expire_stale(conn)
    return outcomes
