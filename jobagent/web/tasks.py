"""Work that outlives a request: the board fetch (with progress) and the apply-assist browser."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from jobagent import store
from jobagent.config import ConfigError, Paths, load_boards
from jobagent.db import connect
from jobagent.ingest import BoardOutcome, ingest_all

Launcher = Callable[[Callable[[], None]], None]


def start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True).start()


def outcome_line(o: BoardOutcome) -> str:
    """One plain-text progress line. Never contains job text."""
    if o.status == "failed":
        return f"{o.company}: failed ({o.error})"
    c = o.counts
    note = "" if o.status == "success" else " (partial: nothing expired)"
    return f"{o.company}: {c['seen']} seen, {c['inserted']} new, {c['updated']} changed{note}"


@dataclass
class FetchProgress:
    running: bool = False
    finished: bool = False
    done: int = 0
    total: int = 0
    new: int = 0
    failed: int = 0
    error: str = ""
    lines: list[str] = field(default_factory=list)


class FetchTask:
    """At most one fetch at a time. Progress is read by the page while it runs."""

    def __init__(self, paths: Paths, launch: Launcher = start_thread) -> None:
        self.paths = paths
        self.launch = launch
        self._lock = threading.Lock()
        self._progress = FetchProgress()

    def snapshot(self) -> dict:
        with self._lock:
            p = self._progress
            return {**p.__dict__, "lines": list(p.lines)}

    def start(self) -> bool:
        """Start a fetch. False if one is already running."""
        with self._lock:
            if self._progress.running:
                return False
            self._progress = FetchProgress(running=True)
        self.launch(self._run)
        return True

    def _on_board(self, o: BoardOutcome, done: int, total: int) -> None:
        with self._lock:
            p = self._progress
            p.done, p.total = done, total
            p.new += o.counts["inserted"]
            p.failed += o.status == "failed"
            p.lines.append(outcome_line(o))

    def _run(self) -> None:
        error = ""
        try:
            boards = load_boards(self.paths)
            if not boards:
                error = "No boards in config/companies.yaml yet."
            else:
                conn = connect(self.paths.db)  # a thread needs its own connection
                try:
                    store.sync_boards(conn, boards)
                    with self._lock:
                        self._progress.total = len(store.enabled_boards(conn))
                    ingest_all(conn, on_board=self._on_board)
                finally:
                    conn.close()
        except ConfigError as exc:
            error = str(exc)
        except Exception as exc:  # report, never crash the server thread
            error = f"Fetch stopped: {type(exc).__name__}"
        with self._lock:
            self._progress.running = False
            self._progress.finished = True
            self._progress.error = error
