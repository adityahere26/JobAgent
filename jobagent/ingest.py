"""Fetch every enabled board and store the jobs, expiring missing ones only after a trusted run."""

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from pydantic import ValidationError

from jobagent import store
from jobagent.db import now_iso
from jobagent.models import JobIn
from jobagent.sources import ADAPTERS, FetchResult, SourceAdapter, SourceError

# Boards are downloaded this many at a time; storing stays on the calling thread (SQLite).
MAX_PARALLEL = 4


@dataclass
class Fetched:
    """One board's download, done off the main thread. `error` is set if it failed."""

    adapter: SourceAdapter | None
    result: FetchResult | None = None
    error: str | None = None


def fetch_board(board: sqlite3.Row) -> Fetched:
    """Network only: safe to run in a worker thread (touches no database)."""
    adapter_cls = ADAPTERS.get(board["source"])
    if adapter_cls is None:
        return Fetched(None, error="unknown source")
    adapter = adapter_cls()
    try:
        return Fetched(adapter, adapter.fetch(board["token"]))
    except SourceError as exc:
        return Fetched(adapter, error=str(exc))


@dataclass
class BoardOutcome:
    company: str
    status: str
    counts: dict[str, int]
    error: str | None = None


def ingest_board(
    conn: sqlite3.Connection,
    board: sqlite3.Row,
    fetched: Fetched | None = None,
    started: str | None = None,
) -> BoardOutcome:
    started = started or now_iso()
    counts = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0, "expired": 0}
    fetched = fetched or fetch_board(board)
    if fetched.error is not None or fetched.adapter is None or fetched.result is None:
        error = fetched.error or "unknown source"
        with conn:
            store.record_run(conn, board["id"], started, "failed", error, counts)
        return BoardOutcome(board["company"], "failed", counts, error)
    adapter, result = fetched.adapter, fetched.result

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


def ingest_all(
    conn: sqlite3.Connection,
    on_board: Callable[[BoardOutcome, int, int], None] | None = None,
) -> list[BoardOutcome]:
    """Download up to MAX_PARALLEL boards at once, then store each on this thread, in order.

    Each board's run starts when the parallel fetch starts; that time is its "unseen" cut-off.
    `on_board(outcome, done, total)` is called after each board is stored.
    """
    boards = store.enabled_boards(conn)
    starts = {b["id"]: now_iso() for b in boards}
    outcomes: list[BoardOutcome] = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix="board") as pool:
        downloads = pool.map(fetch_board, boards)
        for b, fetched in zip(boards, downloads, strict=True):
            outcomes.append(ingest_board(conn, b, fetched, starts[b["id"]]))
            if on_board is not None:
                on_board(outcomes[-1], len(outcomes), len(boards))
    store.expire_stale(conn)
    return outcomes
