import pytest

from jobagent.tailor import render, resume_bullets, tailor, ungrounded_terms
from tests.helpers import make_job, make_profile

RESUME = (
    "# Asha\n- Built APIs in Python serving 2M users\n- Cut SQL query time by 40%\nplain line\n"
)


class Fake:
    host, is_local = "fake", True

    def __init__(self, *replies):
        self.replies = list(replies)

    def complete(self, system, user, schema):
        return self.replies.pop(0)


def reply(summary="Backend engineer with Python.", nums="[1, 2, 9]", note="Hello Acme team."):
    return f'{{"summary": "{summary}", "bullet_numbers": {nums}, "cover_note": "{note}"}}'


def test_bullets_extracted():
    assert resume_bullets(RESUME) == [
        "Built APIs in Python serving 2M users",
        "Cut SQL query time by 40%",
    ]


def test_ungrounded_detection():
    allowed = RESUME + " Acme"
    assert ungrounded_terms("I cut costs by 40% using Python.", allowed) == []
    bad = ungrounded_terms("I led a team of 12 at Google using Kubernetes.", allowed)
    assert "12" in bad and "Google" in bad and "Kubernetes" in bad


def test_picks_only_real_bullets_and_ignores_bad_numbers():
    t = tailor(Fake(reply()), make_job(), make_profile(), RESUME)
    assert t.bullets == resume_bullets(RESUME) and not t.warnings


def test_invention_retries_then_warns():
    lie = reply(summary="Ex-Google lead with 12 years.")
    t = tailor(Fake(lie, lie), make_job(), make_profile(), RESUME)
    assert t.warnings and "Google" in t.warnings[0]
    assert "Warning" in render(make_job(), t)


def test_retry_recovers_with_clean_answer():
    t = tailor(
        Fake(reply(summary="Led 50 engineers."), reply()), make_job(), make_profile(), RESUME
    )
    assert not t.warnings


def test_no_bullets_and_garbage():
    with pytest.raises(ValueError):
        tailor(Fake(), make_job(), make_profile(), "no bullets here")
    assert tailor(Fake("junk", "junk"), make_job(), make_profile(), RESUME) is None
