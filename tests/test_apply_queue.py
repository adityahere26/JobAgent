from contextlib import contextmanager

import pytest

from jobagent import apply_assist as aa
from jobagent import store
from jobagent.apply_queue import DONE_JS, MAX_JOBS, WALL_JS, ApplyQueue
from jobagent.config import BoardConfig, ConfigError, Paths
from jobagent.db import connect
from jobagent.models import JobIn


class FakeLoc:
    def __init__(self, page, idx):
        self.page, self.idx = page, idx

    def fill(self, value):
        self.page.log.append(("fill", self.idx, value))

    def set_input_files(self, value):
        self.page.log.append(("file", self.idx, value))

    def click(self, *_a, **_k):
        raise AssertionError("the queue must never click anything")

    def check(self, *_a, **_k):
        raise AssertionError("the queue must never tick anything")


class FakePage:
    """A form page. `done` is what the site's "application received" check returns, in order."""

    def __init__(self, fields=(), wall="", done=(), close_after=None):
        self.fields, self.wall, self.done = list(fields), wall, list(done)
        self.log, self.url, self.closed, self.waits = [], None, False, 0
        self.close_after = close_after

    def goto(self, url, **_k):
        self.url = url

    def evaluate(self, js):
        if js is WALL_JS:
            return self.wall
        if js is DONE_JS:
            return self.done.pop(0) if self.done else False
        assert js is aa._FIELD_JS
        return self.fields

    def locator(self, sel):
        return FakeLoc(self, int(sel.split('"')[1]))

    def wait_for_timeout(self, _ms):
        self.waits += 1
        if self.close_after is not None and self.waits >= self.close_after:
            self.closed = True
        assert self.waits < 50, "the queue waited forever"

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, pages):
        self.pages, self.opened = list(pages), []

    @contextmanager
    def __call__(self):
        yield self

    def new_page(self):
        page = self.pages.pop(0)
        self.opened.append(page)
        return page


class Deferred:
    """Holds the queue's thread body so a test can queue commands, then run it inline."""

    def __call__(self, target):
        self.target = target


def field(idx, text, type_="text"):
    return {"idx": idx, "tag": "input", "type": type_, "text": text, "visible": True,
            "filled": False}  # fmt: skip


FORM = [
    field(0, "First name"),
    field(1, "Notice period"),
    field(2, "Expected salary"),
    field(3, "I consent to the privacy policy", "checkbox"),
    field(4, "Submit application", "submit"),
    field(5, "Gender"),
]


@pytest.fixture
def home(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/applicant.yaml").write_text("first_name: Mira\nemail: mira@example.com\n")
    (tmp_path / "config/answers.yaml").write_text('notice_period: "30 days"\nexpected_salary:\n')
    conn = connect(tmp_path / "data/jobagent.db")
    store.sync_boards(conn, [BoardConfig(source="greenhouse", token="a", company="A")])
    with conn:
        for i in range(1, 13):
            job = JobIn.model_validate({"external_id": str(i), "title": f"Product Analyst {i}",
                                        "company": "Bluefin Tea",
                                        "apply_url": f"https://forms.example/{i}"})  # fmt: skip
            store.upsert_job(conn, 1, job, "2026-10-01T00:00:00")
    conn.close()
    return tmp_path


def jobs(home, *ids):
    conn = connect(home / "data/jobagent.db")
    return [store.get_job(conn, i) for i in ids]


def statuses(home):
    return store.get_statuses(connect(home / "data/jobagent.db"))


def run(home, pages, job_ids, commands=()):
    launch, browser = Deferred(), FakeBrowser(pages)
    q = ApplyQueue(Paths(home), launch, browser)
    q.start(jobs(home, *job_ids))
    for c in commands:
        q.command(c)
    launch.target()
    return q, browser


def results(q):
    return [i["result"] for i in q.snapshot()["items"]]


def test_prefills_waits_and_never_submits(home):
    first, second = FakePage(FORM), FakePage(FORM)
    q, browser = run(home, [first, second], [1, 2], ["submitted", "skip"])
    assert results(q) == ["applied", "skipped"]
    assert [p.url for p in browser.opened] == ["https://forms.example/1", "https://forms.example/2"]
    assert first.log == [("fill", 0, "Mira"), ("fill", 1, "30 days")]  # no checkbox, no submit
    item = q.snapshot()["items"][0]
    assert item["filled"] == ["first_name", "notice_period"]
    assert item["left"] == ["Expected salary", "Gender"]  # empty answer: left for the human
    assert statuses(home) == {1: "applied"}  # only after the human said they submitted
    snap = q.snapshot()
    assert not snap["running"] and snap["phase"] == "done"
    assert first.closed and second.closed


def test_site_confirmation_and_closed_tab(home):
    confirmed = FakePage(FORM, done=[False, False, True])  # baseline, one poll, then submitted
    closed = FakePage(FORM, close_after=2)
    q, _ = run(home, [confirmed, closed], [1, 2])
    assert results(q) == ["applied", "skipped"]
    assert statuses(home) == {1: "applied"}


@pytest.mark.parametrize(("wall", "words"), [("captcha", "CAPTCHA"), ("login", "sign in")])
def test_captcha_or_login_wall_stops_the_queue(home, wall, words):
    blocked = FakePage(FORM, wall=wall)
    q, browser = run(home, [blocked, FakePage(FORM)], [1, 2, 3], ["stop"])
    assert results(q) == ["blocked", "not opened", "not opened"]
    assert blocked.log == []  # nothing filled on a wall
    assert len(browser.opened) == 1
    snap = q.snapshot()
    assert snap["phase"] == "stopped" and words in snap["message"] and not snap["running"]
    assert statuses(home) == {}


def test_stop_ends_the_queue(home):
    q, browser = run(home, [FakePage(FORM), FakePage(FORM)], [1, 2], ["stop"])
    assert results(q) == ["stopped", "not opened"]
    assert len(browser.opened) == 1 and statuses(home) == {}


def test_queue_limits(home):
    q = ApplyQueue(Paths(home), Deferred(), FakeBrowser([]))
    with pytest.raises(ValueError, match="at most 10"):
        q.start(jobs(home, *range(1, MAX_JOBS + 2)))
    with pytest.raises(ValueError, match="at least one"):
        q.start([])
    q.start(jobs(home, 1))
    with pytest.raises(ValueError, match="already running"):
        q.start(jobs(home, 2))
    assert q.command("submitted") and not q.command("click-submit")


def test_missing_applicant_file_fails_before_opening(home):
    (home / "config/applicant.yaml").unlink()
    q = ApplyQueue(Paths(home), Deferred(), FakeBrowser([]))
    with pytest.raises(ConfigError, match="applicant.yaml"):
        q.start(jobs(home, 1))
    assert not q.snapshot()["running"]


def test_answers_file_is_validated(home):
    (home / "config/answers.yaml").write_text("favourite_colour: teal\n")
    with pytest.raises(ConfigError, match="answers.yaml"):
        aa.load_answers(Paths(home))
    (home / "config/answers.yaml").unlink()
    assert aa.load_answers(Paths(home)) == aa.Answers()


def test_answers_example_matches_the_model():
    from pathlib import Path

    import yaml

    example = Path(__file__).parent.parent / "config/answers.example.yaml"
    data = yaml.safe_load(example.read_text())
    assert set(data) == set(aa.Answers.model_fields)
    for key in data:
        assert any(k == key for k, _ in aa.RULES), f"{key} has no label rule"
