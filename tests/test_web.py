import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobagent import cli, store
from jobagent.config import BoardConfig, ConfigError, Paths
from jobagent.db import connect
from jobagent.models import JobIn
from jobagent.sources import FetchResult
from jobagent.sources.greenhouse import GreenhouseAdapter
from jobagent.web import app as web
from jobagent.web.security import check_bind_host, csrf_form
from tests.helpers import gh_job

BASE = "http://127.0.0.1:8000"
ORIGIN = {"Origin": BASE}
EVIL_TITLE = '<script>alert("t")</script> Product Manager'
EVIL_DESC = 'Own the roadmap. <img src=x onerror=alert(1)> &lt;b&gt;bold&lt;/b&gt; "quoted"'
RAW_DESC = "Own the roadmap. <img src=x onerror=alert(1)> <b>bold</b>"
RESUME = (
    "# Mira Desai\nmira@example.com\n\n"
    "- Shipped a checkout redesign used by 40,000 people\n- Ran weekly user interviews\n"
)


def add(conn, ext, title, company, loc, desc="Roadmap, SQL, user research.", url=None):
    fields = {
        "external_id": ext, "title": title, "company": company, "location_text": loc,
        "description": desc, "apply_url": url or f"https://jobs.example/{ext}",
    }  # fmt: skip
    store.upsert_job(conn, 1, JobIn.model_validate(fields), "2026-10-01T00:00:00+00:00")


@pytest.fixture
def home(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "profile.yaml").write_text(
        "name: Mira Desai\ntarget_roles: [Product Manager]\nskills: [sql, roadmap]\n"
        "experience_years: 2\nlocations: [Bengaluru, Pune, Remote]\ncountries: [IN]\n"
        "min_score: 0\n"
    )
    (cfg / "resume.md").write_text(RESUME)
    (cfg / "companies.yaml").write_text(
        "boards:\n  - {source: greenhouse, token: northwind, company: Northwind Labs}\n"
    )
    (cfg / "applicant.yaml").write_text(
        "first_name: Mira\nlast_name: Desai\nemail: mira@example.com\n"
    )
    conn = connect(tmp_path / "data/jobagent.db")
    store.sync_boards(conn, store_boards())
    with conn:
        add(conn, "1", "Product Manager", "Northwind Labs", "Bengaluru, India")
        add(conn, "2", "Associate Product Manager", "Bluefin Tea", "Remote, India")
        add(conn, "3", EVIL_TITLE, "Contoso <i>Ltd</i>", "Pune, India", EVIL_DESC)
        # Ingest strips tags from descriptions; store raw HTML anyway to prove the page escapes.
        conn.execute("UPDATE jobs SET description = ? WHERE external_id = '3'", (RAW_DESC,))
    conn.close()
    return tmp_path


def store_boards():
    return [BoardConfig(source="greenhouse", token="northwind", company="Northwind Labs")]


class Sync:
    """Runs background work inline so tests can see its result."""

    def __call__(self, target):
        target()


@pytest.fixture
def app(home):
    return web.create_app(Paths(home), 8000, launch=Sync())


@pytest.fixture
def client(app):
    return TestClient(app, base_url=BASE)


def post(client, app, path, data=None, token=True, headers=None):
    body = dict(data or {})
    if token:
        body["csrf"] = app.state.csrf
    return client.post(
        path, data=body, headers={**ORIGIN, **(headers or {})}, follow_redirects=False
    )


def db(home):
    return connect(Path(home) / "data/jobagent.db")


# --- pages ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/jobs/1", "/add", "/tracker"])
def test_pages_render_with_security_headers(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    # No request leaves this machine: no absolute URLs in scripts, styles or images.
    assert not re.search(r'<(script|link|img)[^>]+(src|href)="(https?:)?//', r.text)


def test_jobs_list_shows_scores_and_filters(client):
    page = client.get("/").text
    assert "Product Manager" in page and "Northwind Labs" in page and "Bluefin Tea" in page
    remote_only = client.get("/?remote=1").text
    assert "Bluefin Tea" in remote_only and "Northwind Labs" not in remote_only
    searched = client.get("/?q=northwind").text
    assert "Northwind Labs" in searched and "Bluefin Tea" not in searched
    assert "Bluefin Tea" not in client.get("/?min_score=100").text
    assert "Bluefin Tea" not in client.get("/?city=Bengaluru").text


def test_status_filter_and_one_click_status(client, app, home):
    r = post(client, app, "/jobs/2/status", {"status": "applied", "next": "/?q=x#job-2"})
    assert r.status_code == 303 and r.headers["location"] == "/?q=x#job-2"
    assert store.get_statuses(db(home)) == {2: "applied"}
    assert "Bluefin Tea" in client.get("/?status=applied").text
    new = client.get("/?status=new").text
    assert "Bluefin Tea" not in new and "Northwind Labs" in new
    tracker = client.get("/tracker").text
    assert "Bluefin Tea" in tracker


def test_redirect_never_leaves_the_site(client, app):
    r = post(client, app, "/jobs/1/status", {"status": "saved", "next": "//evil.example/x"})
    assert r.headers["location"] == "/jobs/1?msg=status"


def test_unknown_job_is_404(client):
    assert client.get("/jobs/999").status_code == 404


# --- escaping -------------------------------------------------------------------------------


def test_malicious_title_and_description_are_escaped(client):
    for page in (client.get("/").text, client.get("/jobs/3").text):
        assert "<script>alert" not in page
        assert "&lt;script&gt;alert(&#34;t&#34;)&lt;/script&gt;" in page
        assert "<i>Ltd</i>" not in page and "Contoso &lt;i&gt;Ltd&lt;/i&gt;" in page
    detail = client.get("/jobs/3").text
    assert "<img" not in detail and "<b>bold" not in detail
    assert "&lt;img src=x onerror=alert(1)&gt;" in detail
    assert "&lt;b&gt;bold&lt;/b&gt;" in detail


def test_templates_never_bypass_escaping():
    for path in (Path(web.__file__).parent / "templates").glob("*.html"):
        text = path.read_text()
        assert "|safe" not in text.replace(" ", ""), path.name
        assert "autoescape" not in text, path.name
    script = (Path(web.__file__).parent / "static/app.js").read_text()
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


# --- CSRF, Origin, Host, bind ---------------------------------------------------------------


def test_post_without_or_with_wrong_csrf_token_is_refused(client, app, home):
    assert post(client, app, "/jobs/1/status", {"status": "saved"}, token=False).status_code == 403
    bad = {"status": "saved", "csrf": "not-the-token"}
    assert post(client, app, "/jobs/1/status", bad, token=False).status_code == 403
    assert store.get_statuses(db(home)) == {}


def test_post_from_another_origin_is_refused(client, app, home):
    for origin in ("http://evil.example", "null", "http://127.0.0.1:9999"):
        r = post(client, app, "/jobs/1/status", {"status": "saved"}, headers={"Origin": origin})
        assert r.status_code == 403
    no_origin = client.post(
        "/jobs/1/status", data={"status": "saved", "csrf": app.state.csrf}, follow_redirects=False
    )
    assert no_origin.status_code == 403
    assert store.get_statuses(db(home)) == {}


def test_unknown_host_header_is_refused(app):
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1 still sends its own Host.
    rebound = TestClient(app, base_url="http://evil.example:8000")
    assert rebound.get("/").status_code == 400
    assert TestClient(app, base_url="http://localhost:8000").get("/").status_code == 200


def test_every_post_route_checks_csrf(app):
    posts = [r for r in app.routes if "POST" in getattr(r, "methods", set())]
    assert len(posts) >= 6
    for route in posts:
        deps = [d.call for d in route.dependant.dependencies]
        assert csrf_form in deps, route.path


PUBLIC = "0.0.0.0"  # noqa: S104 (the address the dashboard must refuse)


@pytest.mark.parametrize("host", [PUBLIC, "localhost", "::", "::1", "192.168.1.20", ""])
def test_non_loopback_bind_is_refused(host):
    with pytest.raises(ConfigError):
        check_bind_host(host)
    check_bind_host("127.0.0.1")


def test_cli_refuses_public_host(tmp_path, capsys):
    assert cli.main(["--home", str(tmp_path), "ui", "--host", PUBLIC, "--no-browser"]) == 2
    assert "only listens on 127.0.0.1" in capsys.readouterr().err


# --- add, tailor, apply, refresh ------------------------------------------------------------


def test_add_job_form(client, app, home):
    data = {"title": "Product Analyst", "company": "Fabrikam",
            "url": "https://fabrikam.example/9", "location": "Pune",
            "description": "SQL and roadmap work. Call 98765 43210."}  # fmt: skip
    r = post(client, app, "/add", data)
    assert r.status_code == 303 and r.headers["location"].endswith("?msg=added")
    job = store.get_job(db(home), int(r.headers["location"].split("/")[2].split("?")[0]))
    assert job.title == "Product Analyst" and "98765" not in job.description
    again = post(client, app, "/add", data)
    assert again.headers["location"].endswith("?msg=unchanged")


def test_add_job_form_shows_errors_and_keeps_values(client, app):
    data = {"title": "Product Analyst", "company": "Fabrikam", "url": "http://plain.example"}
    r = post(client, app, "/add", data)
    assert r.status_code == 400
    assert "apply_url" in r.text and 'value="Product Analyst"' in r.text


def test_prepare_tailoring_and_import_reply(client, app, home):
    r = post(client, app, "/jobs/3/tailor/prompt")
    assert r.status_code == 303
    prompt = (home / "reports/tailored/job-3.prompt.md").read_text()
    assert "mira@example.com" not in prompt and "<<<JOB" in prompt
    page = client.get("/jobs/3").text
    assert 'id="prompt-text"' in page and "Copy prompt" in page
    assert "<script>alert" not in page  # the prompt quotes the title; still escaped

    bad = post(client, app, "/jobs/3/tailor/import", {"reply": "no json here"})
    assert bad.status_code == 400 and "not usable" in bad.text

    reply = {"summary": "I ran weekly user interviews.", "bullet_numbers": [2, 1],
             "cover_note": "I would like to own the roadmap."}  # fmt: skip
    ok = post(client, app, "/jobs/3/tailor/import", {"reply": json.dumps(reply)})
    assert ok.status_code == 200 and "I ran weekly user interviews." in ok.text
    assert (home / "reports/tailored/job-3.md").is_file()


def test_apply_only_opens_and_prefills(client, app, home, monkeypatch):
    calls = []
    monkeypatch.setattr(web, "browser_helper_installed", lambda: True)
    monkeypatch.setattr(web.aa, "run", lambda url, values, pdf: calls.append((url, values)))
    r = post(client, app, "/jobs/1/apply")
    assert r.status_code == 303 and r.headers["location"] == "/jobs/1?msg=apply-opened"
    [(url, values)] = calls
    assert url == "https://jobs.example/1" and values["first_name"] == "Mira"
    assert store.get_statuses(db(home)) == {1: "saved"}  # never "applied": the human submits


def test_apply_without_applicant_file_explains(client, app, home, monkeypatch):
    monkeypatch.setattr(web, "browser_helper_installed", lambda: True)
    (home / "config/applicant.yaml").unlink()
    r = post(client, app, "/jobs/1/apply")
    assert r.status_code == 400 and "applicant.yaml" in r.text


def test_refresh_runs_fetch_with_progress(client, app, home, monkeypatch):
    monkeypatch.setattr(
        GreenhouseAdapter, "fetch", lambda self, t: FetchResult([gh_job(77)], complete=True)
    )
    r = post(client, app, "/refresh", headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["started"] is True
    status = client.get("/refresh/status").json()
    assert status["finished"] and not status["running"] and status["error"] == ""
    assert status["done"] == status["total"] == 1 and status["new"] == 1
    assert status["lines"] == ["Northwind Labs: 1 seen, 1 new, 0 changed"]
    # The list picks up the new job without a restart (the score cache notices the change).
    assert "Backend Engineer" in client.get("/").text


def test_tracker_shows_mail_suggestion_with_confirm(client, app, home):
    from jobagent import mail

    conn = db(home)
    store.set_status(conn, 2, "applied")
    reply = mail.Message("m9", "2026-10-06", "Bluefin Tea <hr@bluefin.example>", "Interview", "")
    mail.suggest(conn, [reply])
    page = client.get("/tracker").text
    assert "Replies to check" in page and "bluefin.example" in page and "Confirm" in page
    form = {"message_id": "m9", "job_id": "2", "action": "confirm"}
    r = post(client, app, "/tracker/suggestion", form)
    assert r.status_code == 303
    assert store.get_statuses(db(home))[2] == "interview"
    assert "Replies to check" not in client.get("/tracker").text
    assert post(client, app, "/tracker/suggestion", form).status_code == 400  # already resolved


def test_apply_queue_from_the_jobs_list(home, monkeypatch):
    from tests.test_apply_queue import FORM, Deferred, FakeBrowser, FakePage

    launch, browser = Deferred(), FakeBrowser([FakePage(FORM), FakePage(FORM)])
    app = web.create_app(Paths(home), 8000, launch=launch, queue_browser=browser)
    client = TestClient(app, base_url=BASE)
    monkeypatch.setattr(web, "browser_helper_installed", lambda: True)
    page = client.get("/").text
    assert 'form="queue-form"' in page and "Apply queue" in page

    def start(ids):
        body = {"csrf": app.state.csrf, "job": [str(i) for i in ids]}
        return client.post("/queue", data=body, headers=ORIGIN, follow_redirects=False)

    assert start([]).headers["location"] == "/?msg=queue-empty"
    too_many = start(range(1, 12))
    assert too_many.status_code == 400 and "at most 10" in too_many.text

    r = start([1, 2])
    assert r.status_code == 303 and r.headers["location"] == "/queue"
    queue_page = client.get("/queue").text
    assert "Product Manager" in queue_page and "Bluefin Tea" in queue_page
    assert 'data-running="true"' in queue_page
    for command in ("submitted", "skip"):
        sent = post(client, app, "/queue/command", {"command": command})
        assert sent.status_code == 303
    launch.target()  # run the queue's thread body now
    status = client.get("/queue/status").json()
    assert [i["result"] for i in status["items"]] == ["applied", "skipped"]
    assert store.get_statuses(db(home)) == {1: "applied"}
    assert "Submitted by you" in client.get("/queue").text
