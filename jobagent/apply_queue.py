"""Apply queue: open several application forms one at a time, pre-fill each, and wait for you.

The human submits. For each job the queue opens the form in a visible browser, fills only what it
is sure of (apply_assist rules: never clicks, never ticks checkboxes, leaves unknowns empty), then
waits until you press "I submitted it", "Skip" or "Stop" in the dashboard, close the tab, or the
site shows its own "application received" page. A CAPTCHA or login wall stops the queue.
"""

import queue
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from jobagent import apply_assist as aa
from jobagent import store
from jobagent.config import ConfigError, Paths
from jobagent.db import connect

MAX_JOBS = 10
POLL_MS = 1000
COMMANDS = ("submitted", "skip", "stop")

# Only a challenge the person must solve counts: invisible reCAPTCHA on a normal form does not.
WALL_JS = """
() => {
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const text = document.body ? document.body.innerText : '';
  const solver = /captcha|challenges\\.cloudflare/i;  // reCAPTCHA, hCaptcha, Turnstile
  const challenge = [...document.querySelectorAll('iframe')].some((f) => {
    const src = f.src || '';
    return solver.test(src) && !/size=invisible/i.test(src) && shown(f);
  });
  if (challenge || /verify you are (a )?human|are you a robot|complete the security check/i
      .test(text)) return 'captcha';
  if ([...document.querySelectorAll('input[type=password]')].some(shown)) return 'login';
  if (/\\b(sign|log) ?in to (continue|apply)\\b/i.test(text)) return 'login';
  return '';
}
"""
DONE_JS = """
() => [/thank you for (applying|your application)/i,
       /application (has been )?(submitted|received)/i,
       /we('ve| have) received your application/i]
  .some((re) => re.test(document.body ? document.body.innerText : ''))
"""

WALL_MESSAGES = {
    "captcha": "A CAPTCHA appeared",
    "login": "The site asks you to sign in",
}


@dataclass
class QueueItem:
    job_id: int
    title: str
    company: str
    url: str
    values: dict[str, str] = field(repr=False, default_factory=dict)
    resume_pdf: str = field(repr=False, default="")
    result: str = "waiting"  # waiting, open, applied, skipped, blocked, stopped, not opened
    filled: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)


@contextmanager
def open_browser() -> Iterator[Any]:
    """A visible Chromium window; yields a browser context. Closed when the queue ends."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ConfigError("Install the browser helper first: uv sync --extra apply") from exc
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            yield browser.new_context()
        finally:
            browser.close()


def detect_wall(page: Any) -> str:
    """'captcha', 'login' or '' for the page as it is now."""
    try:
        return str(page.evaluate(WALL_JS) or "")
    except Exception:  # the page navigated mid-check: look again next time
        return ""


def looks_submitted(page: Any) -> bool:
    try:
        return bool(page.evaluate(DONE_JS))
    except Exception:
        return False


class ApplyQueue:
    """One queue at a time. The browser lives on the queue's own thread; the dashboard only
    sends commands and reads snapshots."""

    def __init__(
        self,
        paths: Paths,
        launch: Callable[[Callable[[], None]], None],
        browser: Callable[[], Any] = open_browser,
    ) -> None:
        self.paths = paths
        self.launch = launch
        self.browser = browser
        self._lock = threading.Lock()
        self._commands: queue.Queue[str] = queue.Queue()
        self.running = False
        self.phase = "idle"  # idle, opening, waiting, blocked, done, stopped
        self.current = -1
        self.message = ""
        self.items: list[QueueItem] = []
        self.version = 0

    # --- called from request threads ---

    def start(self, jobs: list[store.Job]) -> None:
        """Validate and start. Raises ValueError (bad selection) or ConfigError (setup)."""
        if not jobs:
            raise ValueError("Pick at least one job.")
        if len(jobs) > MAX_JOBS:
            raise ValueError(f"Pick at most {MAX_JOBS} jobs for one queue.")
        items = []
        for job in jobs:
            values, pdf = aa.prepare(self.paths, job.id)  # fails early if applicant.yaml is bad
            items.append(QueueItem(job.id, job.title, job.company, job.apply_url, values, pdf))
        with self._lock:
            if self.running:
                raise ValueError("A queue is already running. Finish or stop it first.")
            self.running = True
            self.items, self.current, self.phase = items, -1, "opening"
            self.message = "Opening the browser…"
            self._commands = queue.Queue()
            self.version += 1
        self.launch(self._run)

    def command(self, cmd: str) -> bool:
        if cmd not in COMMANDS or not self.running:
            return False
        self._commands.put(cmd)
        return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "phase": self.phase,
                "current": self.current,
                "message": self.message,
                "version": self.version,
                "items": [
                    {
                        "job_id": i.job_id,
                        "title": i.title,
                        "company": i.company,
                        "result": i.result,
                        "filled": list(i.filled),
                        "left": list(i.left),
                    }
                    for i in self.items
                ],
            }

    # --- the queue thread ---

    def _set(self, **changes: Any) -> None:
        with self._lock:
            for k, v in changes.items():
                setattr(self, k, v)
            self.version += 1

    def _set_item(self, item: QueueItem, **changes: Any) -> None:
        with self._lock:
            for k, v in changes.items():
                setattr(item, k, v)
            self.version += 1

    def _wait(self, page: Any, already_done: bool) -> str:
        """Until a command arrives, the tab is closed, or the site confirms the application."""
        while True:
            try:
                return self._commands.get_nowait()
            except queue.Empty:
                pass
            if page.is_closed():
                return "closed"
            if not already_done and looks_submitted(page):
                return "confirmed"
            try:
                page.wait_for_timeout(POLL_MS)  # also lets Playwright process browser events
            except Exception:
                if page.is_closed():
                    return "closed"
                raise

    def _finish(self, phase: str, message: str) -> None:
        with self._lock:
            for item in self.items:
                if item.result in ("waiting", "open"):
                    item.result = "not opened" if item.result == "waiting" else "stopped"
            self.phase, self.message, self.running = phase, message, False
            self.version += 1

    def _mark_applied(self, conn: sqlite3.Connection, item: QueueItem, how: str) -> None:
        store.set_status(conn, item.job_id, "applied", f"submitted by you ({how})")
        self._set_item(item, result="applied")

    def _run(self) -> None:
        conn = connect(self.paths.db)
        try:
            with self.browser() as context:
                for index, item in enumerate(self.items):
                    if not self._process(conn, context, index, item):
                        return
            done = sum(i.result == "applied" for i in self.items)
            self._finish("done", f"Queue finished: {done} submitted by you, the rest skipped.")
        except Exception as exc:  # the browser window was closed, or failed to start
            reason = str(exc) if isinstance(exc, ConfigError) else type(exc).__name__
            self._finish("stopped", f"The queue stopped: {reason}.")
        finally:
            conn.close()

    def _process(self, conn: sqlite3.Connection, context: Any, index: int, item: QueueItem) -> bool:
        """Handle one job. False means the queue must stop here."""
        self._set(current=index, phase="opening", message=f"Opening {item.title}…")
        self._set_item(item, result="open")
        page = context.new_page()
        page.goto(item.url, wait_until="domcontentloaded")
        wall = detect_wall(page)
        if wall:
            stopped = f"{WALL_MESSAGES[wall]} on {item.title} at {item.company}. The queue stopped"
            self._set_item(item, result="blocked")
            self._set(
                phase="blocked",
                message=f"{stopped} and filled nothing. You can finish this one in the browser "
                "yourself, then press Stop to close the browser.",
            )
            outcome = self._wait(page, already_done=False)
            if outcome in ("submitted", "confirmed"):
                self._mark_applied(conn, item, "after a sign-in or CAPTCHA")
            self._finish("stopped", f"{stopped}; later jobs were not opened.")
            return False
        filled, left = aa.prefill(page, item.values, item.resume_pdf)
        self._set_item(item, filled=filled, left=left)
        self._set(
            phase="waiting",
            message=f"{item.title} at {item.company} is pre-filled. Check every answer, "
            "submit it in the browser yourself, then press “I submitted it”.",
        )
        outcome = self._wait(page, already_done=looks_submitted(page))
        if outcome in ("submitted", "confirmed"):
            how = "dashboard" if outcome == "submitted" else "site confirmation seen"
            self._mark_applied(conn, item, how)
        elif outcome in ("skip", "closed"):
            self._set_item(item, result="skipped")
        if not page.is_closed():
            page.close()
        if outcome == "stop":
            self._set_item(item, result="stopped")
            self._finish("stopped", "Queue stopped. Nothing else was opened.")
            return False
        return True
