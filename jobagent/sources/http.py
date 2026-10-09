"""One GET with a hard total deadline, shared by the source adapters.

httpx timeouts apply per operation, so a server that trickles bytes can keep a single request
alive for many minutes (seen live: a Lever board took 15 minutes to fail). Here the download
streams with a monotonic-clock check between chunks, and runs in a daemon thread that the caller
waits on for at most `deadline` seconds, so no board can hold up a fetch for longer than that.
"""

import json
import threading
import time
from typing import Any

import httpx

from jobagent import __version__
from jobagent.sources.base import SourceError

DEADLINE = 30.0
TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
HEADERS = {"User-Agent": f"jobagent/{__version__} (personal job search tool)"}


class _DeadlineHit(Exception):
    pass


def _download(url: str, params: dict[str, str], stop_at: float) -> tuple[int, bytes]:
    with (
        httpx.Client(timeout=TIMEOUT, headers=HEADERS) as client,
        client.stream("GET", url, params=params) as resp,
    ):
        if resp.status_code != 200:
            return resp.status_code, b""
        chunks: list[bytes] = []
        for chunk in resp.iter_bytes():
            if time.monotonic() > stop_at:
                raise _DeadlineHit
            chunks.append(chunk)
        return 200, b"".join(chunks)


def get_json(url: str, params: dict[str, str], deadline: float | None = None) -> Any:
    """GET `url` and parse JSON, finishing within `deadline` seconds or raising SourceError."""
    limit = DEADLINE if deadline is None else deadline
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = _download(url, params, time.monotonic() + limit)
        except Exception as exc:  # handed to the caller's thread below
            box["error"] = exc

    worker = threading.Thread(target=run, name="jobagent-fetch", daemon=True)
    worker.start()
    worker.join(limit)
    if worker.is_alive():
        raise SourceError("timed out")  # the daemon thread stops at its own next timeout

    error = box.get("error")
    if isinstance(error, (_DeadlineHit, httpx.TimeoutException)):
        raise SourceError("timed out") from error
    if error is not None:
        raise SourceError(f"request failed: {type(error).__name__}") from error

    status, body = box["result"]
    if status != 200:
        raise SourceError(f"board returned HTTP {status}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SourceError("board returned invalid JSON") from exc
