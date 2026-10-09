import pytest

from jobagent.tailor import (
    SYSTEM,
    check_reply,
    prepare,
    render,
    resume_bullets,
    tailor,
    ungrounded_terms,
)
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


# The reply qwen2.5:3b gave for Meesho PM I on 2026-10-09 (summary/cover note verbatim).
LIVE_SUMMARY = (
    "Tailored for the Product Manager I role at Meesho, highlighting skills and experience "
    "relevant to the job posting."
)
LIVE_NOTE = (
    "Asha has a strong background in product management, with hands-on experience in AI/ML. "
    "His experience aligns well with the role's requirements."
)
GOOD_NOTE = "I built Python tools and want to bring that to Acme."
LONG_RESUME = "# Asha Rao\n" + "".join(f"- Bullet {i} about Python\n" for i in range(1, 11))


def live_reply(summary=LIVE_SUMMARY, nums="[1, 2, 3, 4, 5, 6, 7, 8]", note=LIVE_NOTE):
    return reply(summary=summary, nums=nums, note=note)


def run(*replies, profile=None):
    return tailor(Fake(*replies), make_job(), profile or make_profile(), LONG_RESUME)


def test_live_meesho_reply_is_rejected_twice():
    rejected: list[str] = []
    t = tailor(Fake(live_reply(), live_reply()), make_job(), make_profile(), LONG_RESUME, rejected)
    assert t is None and len(rejected) == 2 and "document instead of you" in rejected[0]


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        (live_reply(nums="[3, 7, 1]", note=GOOD_NOTE), "document instead of you"),
        (live_reply(summary="I build APIs.", nums="[3, 7]"), "third person"),
        (reply(summary="He builds APIs.", nums="[3]", note=GOOD_NOTE), "third person"),
        (reply(summary="I build APIs.", nums="[1, 2, 3, 4]", note=GOOD_NOTE), "in order"),
    ],
)
def test_each_rule_rejects_then_retry_recovers(bad, why):
    good = reply(summary="I build Python tools.", nums="[7, 2, 9]", note=GOOD_NOTE)
    t = run(bad, good)
    assert t is not None and t.bullets == ["Bullet 7 about Python", "Bullet 2 about Python",
                                           "Bullet 9 about Python"]  # fmt: skip
    prep = prepare(make_job(), make_profile(), LONG_RESUME)
    with pytest.raises(ValueError, match=why):
        check_reply(bad, prep)


def test_name_from_profile_counts_as_third_person():
    p = make_profile(name="Asha Rao")
    bad = reply(summary="Asha builds APIs.", nums="[2]", note=GOOD_NOTE)
    with pytest.raises(ValueError, match="third person"):
        check_reply(bad, prepare(make_job(), p, LONG_RESUME))


def test_two_first_bullets_are_allowed():
    t = run(reply(summary="I build Python tools.", nums="[1, 2]", note=GOOD_NOTE))
    assert t is not None and len(t.bullets) == 2


def test_overclaim_words_warn_unless_resume_supports_them():
    puffed = reply(
        summary="I am a seasoned expert with extensive Python skills.", nums="[2]", note=GOOD_NOTE
    )
    t = run(puffed, puffed)
    assert t is not None
    assert any("seasoned" in w and "expert" in w and "extensive" in w for w in t.warnings)
    supported = tailor(Fake(puffed), make_job(), make_profile(),
                       LONG_RESUME + "- Python expert with extensive, seasoned use\n")  # fmt: skip
    assert not any("Claims" in w for w in supported.warnings)


def test_prompt_states_the_rules():
    for phrase in ("first person", "Tailored for", "most relevant to this job first", "seasoned"):
        assert phrase in SYSTEM
