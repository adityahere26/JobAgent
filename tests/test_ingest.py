import pytest

from jobagent import store
from jobagent.config import BoardConfig
from jobagent.ingest import ingest_all
from jobagent.sources import FetchResult, SourceError
from jobagent.sources.greenhouse import GreenhouseAdapter
from tests.helpers import gh_job


@pytest.fixture
def board(conn):
    store.sync_boards(conn, [BoardConfig(source="greenhouse", token="acme", company="Acme")])


def run(conn, monkeypatch, result=None, error=None):
    def fake(self, token):
        if error:
            raise SourceError(error)
        return result

    monkeypatch.setattr(GreenhouseAdapter, "fetch", fake)
    return ingest_all(conn)[0]


def active(conn):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE expired_at IS NULL").fetchone()[0]


def test_insert_unchanged_and_update(conn, board, monkeypatch):
    out = run(conn, monkeypatch, FetchResult([gh_job(1), gh_job(2)]))
    assert out.status == "success" and out.counts["inserted"] == 2
    out = run(conn, monkeypatch, FetchResult([gh_job(1), gh_job(2)]))
    assert out.counts["unchanged"] == 2
    out = run(conn, monkeypatch, FetchResult([gh_job(1, content="<p>New text</p>"), gh_job(2)]))
    assert out.counts["updated"] == 1


def test_missing_job_expires_only_on_trusted_run(conn, board, monkeypatch):
    run(conn, monkeypatch, FetchResult([gh_job(1), gh_job(2)]))
    out = run(conn, monkeypatch, FetchResult([gh_job(1)], complete=False))
    assert out.status == "partial" and active(conn) == 2
    out = run(conn, monkeypatch, FetchResult([gh_job(1)]))
    assert out.counts["expired"] == 1 and active(conn) == 1


def test_failed_fetch_expires_nothing(conn, board, monkeypatch):
    run(conn, monkeypatch, FetchResult([gh_job(1)]))
    out = run(conn, monkeypatch, error="HTTP 500")
    assert out.status == "failed" and active(conn) == 1


def test_empty_board_is_suspect(conn, board, monkeypatch):
    run(conn, monkeypatch, FetchResult([gh_job(1)]))
    out = run(conn, monkeypatch, FetchResult([]))
    assert out.status == "partial" and active(conn) == 1


def test_bad_listing_is_skipped_but_kept_alive(conn, board, monkeypatch):
    run(conn, monkeypatch, FetchResult([gh_job(1), gh_job(2)]))
    bad = gh_job(2, absolute_url="http://insecure.example")
    out = run(conn, monkeypatch, FetchResult([gh_job(1), bad]))
    assert out.counts["skipped"] == 1 and out.status == "success" and active(conn) == 2


def test_listing_without_id_blocks_expiry(conn, board, monkeypatch):
    run(conn, monkeypatch, FetchResult([gh_job(1), gh_job(2)]))
    out = run(conn, monkeypatch, FetchResult([gh_job(1), {"title": "no id"}]))
    assert out.status == "partial" and active(conn) == 2


def test_removed_board_disabled_not_deleted(conn, board):
    store.sync_boards(conn, [])
    assert store.enabled_boards(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 1


def test_expired_job_returns(conn, board, monkeypatch):
    run(conn, monkeypatch, FetchResult([gh_job(1), gh_job(2)]))
    run(conn, monkeypatch, FetchResult([gh_job(1)]))
    run(conn, monkeypatch, FetchResult([gh_job(1), gh_job(2)]))
    assert active(conn) == 2


def test_progress_callback_called_per_board(conn, board, monkeypatch):
    monkeypatch.setattr(
        GreenhouseAdapter, "fetch", lambda self, t: FetchResult([gh_job(1)], complete=True)
    )
    seen = []
    ingest_all(conn, on_board=lambda o, done, total: seen.append((o.company, done, total)))
    assert seen == [("Acme", 1, 1)]
