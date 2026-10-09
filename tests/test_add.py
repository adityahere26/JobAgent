import io
from pathlib import Path

from jobagent import cli, store
from jobagent.db import connect
from jobagent.sources import FetchResult
from jobagent.sources.greenhouse import GreenhouseAdapter
from tests.helpers import gh_job

URL = "https://www.linkedin.com/jobs/view/1234567890/"
DESCRIPTION = (
    "We need a Backend Engineer with Python. 3 years.\n"
    "Send your CV to recruiter@example.com or call +91 98765 43210."
)


def setup_home(tmp_path: Path, boards: str = "boards: []\n") -> list[str]:
    (tmp_path / "config").mkdir()
    (tmp_path / "config/profile.yaml").write_text(
        "target_roles: [Backend Engineer]\nskills: [python]\nlocations: [Bengaluru, Remote]\n"
        "countries: [IN]\nmin_score: 35\n"
    )
    (tmp_path / "config/companies.yaml").write_text(boards)
    return ["--home", str(tmp_path)]


def add(h, tmp_path, url=URL, **extra) -> int:
    desc = tmp_path / "job.txt"
    desc.write_text(DESCRIPTION)
    args = [*h, "add", "--title", "Backend Engineer", "--company", "Found Elsewhere"]
    args += ["--url", url, "--location", "Bengaluru, India", "--description-file", str(desc)]
    return cli.main(args)


def manual_jobs(tmp_path):
    conn = connect(tmp_path / "data/jobagent.db")
    return conn.execute(
        "SELECT j.* FROM jobs j JOIN boards b ON b.id = j.board_id WHERE b.source = 'manual'"
    ).fetchall()


def test_add_then_rank_shows_it(tmp_path, capsys):
    h = setup_home(tmp_path)
    assert add(h, tmp_path) == 0
    assert "Added job 1" in capsys.readouterr().out
    assert cli.main([*h, "rank", "--no-llm"]) == 0
    report = (tmp_path / "reports/latest.md").read_text()
    assert "Backend Engineer at Found Elsewhere" in report and URL in report
    [row] = manual_jobs(tmp_path)
    # Same cleaning as fetched jobs: contacts stripped, location parsed.
    assert "recruiter@example.com" not in row["description"]
    assert "98765" not in row["description"]
    assert row["cities"] == '["Bengaluru"]'
    # mark/show work on it like any other job.
    assert cli.main([*h, "mark", "1", "applied"]) == 0
    assert cli.main([*h, "show", "1"]) == 0


def test_adding_same_url_updates_not_duplicates(tmp_path, capsys):
    h = setup_home(tmp_path)
    assert add(h, tmp_path) == 0
    assert add(h, tmp_path) == 0
    assert "Already saved" in capsys.readouterr().out
    assert len(manual_jobs(tmp_path)) == 1


def test_description_from_stdin(tmp_path, monkeypatch):
    h = setup_home(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(DESCRIPTION))
    args = [*h, "add", "--title", "Backend Engineer", "--company", "X", "--url", URL]
    assert cli.main(args) == 0
    assert "Python" in manual_jobs(tmp_path)[0]["description"]


def test_bad_url_rejected(tmp_path, capsys):
    h = setup_home(tmp_path)
    for url in ("http://example.com/job", "javascript:alert(1)", "not a url", "https://u:p@x.io/"):
        assert add(h, tmp_path, url=url) == 1, url
    err = capsys.readouterr().err
    assert "apply_url" in err and "u:p" not in err  # names the field, never echoes the value
    assert not (tmp_path / "data/jobagent.db").exists()  # rejected before touching the db


def test_fetch_never_expires_or_disables_manual_board(tmp_path, monkeypatch):
    h = setup_home(tmp_path, "boards:\n  - {source: greenhouse, token: acme, company: Acme}\n")
    assert add(h, tmp_path) == 0
    monkeypatch.setattr(GreenhouseAdapter, "fetch", lambda self, t: FetchResult([gh_job(1)]))
    for _ in range(2):  # complete, trusted runs: these expire unseen jobs on their own board
        assert cli.main([*h, "fetch"]) == 0
    # Even a config that names the manual board, or drops every board, cannot touch it.
    (tmp_path / "config/companies.yaml").write_text(
        "boards:\n  - {source: manual, token: manual, company: X, enabled: false}\n"
    )
    cli.main([*h, "fetch"])
    [row] = manual_jobs(tmp_path)
    assert row["expired_at"] is None

    conn = connect(tmp_path / "data/jobagent.db")
    assert conn.execute("SELECT enabled FROM boards WHERE source = 'manual'").fetchone()[0] == 1
    # The 30-day backstop skips manual jobs too.
    conn.execute("UPDATE jobs SET last_seen_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    store.expire_stale(conn)
    states = dict(
        conn.execute(
            "SELECT b.source, j.expired_at IS NULL FROM jobs j JOIN boards b ON b.id = j.board_id"
        ).fetchall()
    )
    assert states == {"manual": 1, "greenhouse": 0}
    assert [b["source"] for b in store.enabled_boards(conn)] == []
