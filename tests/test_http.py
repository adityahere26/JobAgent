import threading
import time

import httpx
import pytest

from jobagent import store
from jobagent.config import BoardConfig
from jobagent.db import now_iso
from jobagent.ingest import MAX_PARALLEL, ingest_all
from jobagent.models import JobIn
from jobagent.sources import FetchResult, SourceError, http
from jobagent.sources.greenhouse import GreenhouseAdapter
from jobagent.sources.lever import LeverAdapter
from tests.helpers import gh_job, json_handler, mock_http


@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setattr(http, "DEADLINE", 0.3)


def trickle(request):
    # Live failure mode: headers arrive, then the body comes a few bytes at a time, forever.
    def body():
        yield b"["
        while True:
            time.sleep(0.05)
            yield b" "

    return httpx.Response(200, content=body())


def hang(request):
    time.sleep(2)  # never answers within the deadline
    return httpx.Response(200, json=[])


@pytest.mark.parametrize("handler", [trickle, hang])
def test_total_deadline(monkeypatch, short_deadline, handler):
    mock_http(monkeypatch, handler)
    began = time.monotonic()
    with pytest.raises(SourceError, match="timed out"):
        LeverAdapter().fetch("slowco")
    assert time.monotonic() - began < 1.0


def test_httpx_timeout_is_reported_as_timed_out(monkeypatch):
    def slow_read(request):
        raise httpx.ReadTimeout("slow", request=request)

    mock_http(monkeypatch, slow_read)
    with pytest.raises(SourceError, match="timed out"):
        GreenhouseAdapter().fetch("acme")


def test_timeout_settings():
    t = http.TIMEOUT
    assert (t.connect, t.read, t.write, t.pool) == (5.0, 10.0, 5.0, 5.0)
    assert http.DEADLINE == 30.0


def test_ok_and_invalid_json(monkeypatch):
    mock_http(monkeypatch, json_handler(payload=[1, 2]))
    assert http.get_json("https://x.example/a", {}) == [1, 2]
    mock_http(monkeypatch, lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(SourceError, match="invalid JSON"):
        http.get_json("https://x.example/a", {})


def test_timed_out_board_fails_and_expires_nothing(conn, monkeypatch, short_deadline):
    store.sync_boards(conn, [BoardConfig(source="lever", token="slowco", company="Slow")])
    job = LeverAdapter().to_job_fields(
        {"id": "x", "text": "PM", "hostedUrl": "https://jobs.lever.co/slowco/x"}, "Slow"
    )
    with conn:
        store.upsert_job(conn, 1, JobIn.model_validate(job), now_iso())
    mock_http(monkeypatch, trickle)
    [out] = ingest_all(conn)
    assert out.status == "failed" and out.error == "timed out"
    assert conn.execute("SELECT expired_at FROM jobs").fetchone()[0] is None


def test_boards_fetch_in_parallel_at_most_four(conn, monkeypatch):
    boards = [BoardConfig(source="greenhouse", token=f"b{i}", company=f"B{i}") for i in range(8)]
    store.sync_boards(conn, boards)
    lock, live, peak = threading.Lock(), [0], [0]

    def slow_fetch(self, token):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.2)
        with lock:
            live[0] -= 1
        return FetchResult([gh_job(int(token[1:]) + 1)])

    monkeypatch.setattr(GreenhouseAdapter, "fetch", slow_fetch)
    began = time.monotonic()
    outcomes = ingest_all(conn)
    elapsed = time.monotonic() - began
    assert peak[0] == MAX_PARALLEL == 4
    assert elapsed < 8 * 0.2 * 0.75  # well under the one-at-a-time time
    assert [o.company for o in outcomes] == [f"B{i}" for i in range(8)]  # order kept
    assert all(o.status == "success" for o in outcomes)
