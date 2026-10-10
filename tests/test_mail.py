import json
import stat

import httpx
import pytest

from jobagent import cli, mail, store
from jobagent.config import BoardConfig, ConfigError, Paths
from jobagent.db import connect
from jobagent.mail import Message
from jobagent.models import JobIn
from tests.helpers import make_job, mock_http


@pytest.mark.parametrize(
    ("subject", "snippet", "status"),
    [
        ("Interview invitation", "Please share your availability", "interview"),
        ("Your application", "Unfortunately we will not be moving forward", "rejected"),
        ("Update", "We regret to inform you that the position has been filled", "rejected"),
        ("Offer letter", "We are pleased to offer you the role", "offer"),
        ("Thank you for applying", "We received your application and will review it", "other"),
        ("Next steps", "Unfortunately, after careful review...", "rejected"),
    ],
)
def test_classify(subject, snippet, status):
    assert mail.classify(subject, snippet) == status


def test_sender_domain_and_matching_rules():
    assert mail.sender_domain('"Talent Team" <talent@Careers.Northwind.example>') == (
        "careers.northwind.example"
    )
    assert mail.sender_domain("not an address") == ""
    assert mail.domain_matches("mail.northwindlabs.example", "Northwind Labs Pvt Ltd")
    assert mail.domain_matches("northwind.example", "Northwind Labs")
    assert not mail.domain_matches("greenhouse.io", "Greenhouse")  # a tracking system, not them
    assert not mail.domain_matches("northwinds.example", "Northwind Labs")
    assert mail.name_matches("Bluefin Tea Recruiting", "Bluefin Tea")
    assert not mail.name_matches("Bluefinch updates", "Bluefin Tea")


def msg(id_="m1", sender="Recruiting <no-reply@greenhouse.io>", subject="", snippet=""):
    return Message(id_, "2026-10-05", sender, subject, snippet)


def test_match_jobs_prefers_named_title():
    pm = make_job(id=1, title="Product Manager", company="Bluefin Tea")
    pa = make_job(id=2, title="Product Analyst", company="Bluefin Tea")
    other = make_job(id=3, title="Product Manager", company="Fabrikam")
    m = msg(subject="Bluefin Tea: your Product Analyst application")
    assert [j.id for j in mail.match_jobs(m, [pm, pa, other])] == [2]
    m = msg(sender="Bluefin Tea Hiring <jobs@greenhouse-mail.io>", subject="Thanks")
    assert [j.id for j in mail.match_jobs(m, [pm, pa, other])] == [1, 2]
    m = msg(sender="Ana <ana@fabrikam.example>", subject="Quick chat?")
    assert [j.id for j in mail.match_jobs(m, [pm, pa, other])] == [3]
    assert mail.match_jobs(msg(subject="Weekly newsletter"), [pm, pa, other]) == []


@pytest.fixture
def applied(conn):
    store.sync_boards(conn, [BoardConfig(source="greenhouse", token="a", company="A")])
    for ext, company in (("1", "Bluefin Tea"), ("2", "Fabrikam")):
        job = JobIn.model_validate({"external_id": ext, "title": "Product Analyst",
                                    "company": company,
                                    "apply_url": f"https://x.example/{ext}"})  # fmt: skip
        store.upsert_job(conn, 1, job, "2026-10-01T00:00:00")
    store.set_status(conn, 1, "applied")  # job 2 is only saved: never matched
    store.set_status(conn, 2, "saved")
    return conn


SECRET_SUBJECT = "Interview for Product Analyst at Bluefin Tea, room 4B"
SECRET_SNIPPET = "Hi Mira, can you do Tuesday 3pm? Call me on 98765 43210"


def test_suggest_stores_no_email_text(applied):
    messages = [
        msg("m1", "Bluefin Tea <talent@bluefin.example>", SECRET_SUBJECT, SECRET_SNIPPET),
        msg("m2", "Fabrikam <hr@fabrikam.example>", "Interview", "Unfortunately no"),
    ]
    new = mail.suggest(applied, messages)
    assert [(s.message_id, s.job_id, s.suggested_status) for s in new] == [("m1", 1, "interview")]
    rows = applied.execute("SELECT * FROM mail_suggestions").fetchall()
    assert set(rows[0].keys()) == {"message_id", "job_id", "received_on", "sender_domain",
                                   "suggested_status", "state", "created_at"}  # fmt: skip
    stored = " ".join(str(v) for r in rows for v in tuple(r))
    for secret in ("Tuesday", "98765", "Mira", "room 4B", "Interview for"):
        assert secret not in stored
    assert rows[0]["sender_domain"] == "bluefin.example"
    assert mail.suggest(applied, messages) == []  # already seen: not suggested again


def test_confirm_and_dismiss(applied):
    mail.suggest(applied, [msg("m1", "Bluefin Tea <a@bluefin.example>", "Interview", "")])
    store.resolve_suggestion(applied, "m1", 1, confirm=True)
    assert store.get_statuses(applied)[1] == "interview"
    assert store.pending_suggestions(applied) == []
    # Still watched while in interview: a later reply is suggested too.
    mail.suggest(applied, [msg("m2", "Bluefin Tea <a@bluefin.example>", "Thanks", "")])
    store.resolve_suggestion(applied, "m2", 1, confirm=True)  # "other": nothing to apply
    assert store.get_statuses(applied)[1] == "interview"
    with pytest.raises(ValueError):
        store.resolve_suggestion(applied, "m2", 1, confirm=False)  # no longer pending


def gmail_handler(seen):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={
            "id": "m1", "internalDate": "1791158400000", "snippet": SECRET_SNIPPET,
            "payload": {"headers": [{"name": "From", "value": "Bluefin Tea <t@bluefin.example>"},
                                    {"name": "Subject", "value": SECRET_SUBJECT}]},
        })  # fmt: skip

    return handle


def test_gmail_client_reads_headers_only(monkeypatch):
    seen = []
    mock_http(monkeypatch, gmail_handler(seen))
    [m] = mail.GmailClient("tok").recent_messages()
    assert m.subject == SECRET_SUBJECT and m.received_on == "2026-10-05"
    assert all(r.method == "GET" for r in seen)
    assert all(r.headers["authorization"] == "Bearer tok" for r in seen)
    assert "newer_than:30d" in str(seen[0].url.params["q"])
    assert seen[1].url.params["format"] == "metadata"


def test_gmail_client_unauthorised_explains(monkeypatch):
    mock_http(monkeypatch, lambda r: httpx.Response(401, json={}))
    with pytest.raises(ConfigError, match="mail setup"):
        mail.GmailClient("tok").recent_messages()


def test_token_file_is_private_and_scope_checked(tmp_path):
    path = tmp_path / "data/gmail_token.json"
    path.parent.mkdir()
    path.write_text("{}")
    path.chmod(0o644)
    mail.write_private(path, json.dumps({"scopes": [mail.SCOPE, "https://mail.google.com/"]}))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ConfigError, match="only the"):
        mail.access_token(Paths(tmp_path))  # refused before any Google library is used
    mail.check_scopes([mail.SCOPE])


def test_setup_needs_client_secret(tmp_path):
    with pytest.raises(ConfigError, match="client_secret.json"):
        mail.setup(Paths(tmp_path))


def test_cli_mail_check_prints_no_email_text(tmp_path, monkeypatch, capsys):
    conn = connect(tmp_path / "data/jobagent.db")
    store.sync_boards(conn, [BoardConfig(source="greenhouse", token="a", company="A")])
    job = JobIn.model_validate({"external_id": "1", "title": "Product Analyst",
                                "company": "Bluefin Tea",
                                "apply_url": "https://x.example/1"})  # fmt: skip
    store.upsert_job(conn, 1, job, "2026-10-01T00:00:00")
    store.set_status(conn, 1, "applied")
    monkeypatch.setattr(mail, "access_token", lambda paths: "tok")
    mock_http(monkeypatch, gmail_handler([]))
    assert cli.main(["--home", str(tmp_path), "mail", "check"]) == 0
    out = capsys.readouterr().out
    assert "job 1 Product Analyst at Bluefin Tea: interview" in out
    assert "bluefin.example" in out and "Tuesday" not in out and "room 4B" not in out
