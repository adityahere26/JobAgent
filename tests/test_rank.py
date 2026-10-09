import pytest
from pydantic import ValidationError

from jobagent import store
from jobagent.config import BoardConfig, LLMConfig
from jobagent.models import JobIn
from jobagent.rank import Ranked, blend, order, rank_jobs
from tests.helpers import make_job, make_profile

# (job id, title, heuristic, llm) from the live qwen2.5:3b run on 2026-10-09.
LIVE = [
    (3849, "Product Manager I, Meesho", 78, 45),
    (450, "Product Analyst II, HackerRank", 75, 40),
    (3884, "Product Manager I, Zeta", 78, 20),
    (4006, "Associate Product Manager, Paytm", 78, 20),
    (176, "Product Manager, Okta", 71, 20),
]
HEVO = (4139, "Product Manager, Hevo Data (asks 6+ years)", 45)  # capped, not LLM-scored


def live_ranking(weight: float) -> list[str]:
    ranked = [
        Ranked(make_job(id=i, title=t), blend(h, llm, weight), llm_used=True)
        for i, t, h, llm in LIVE
    ]
    ranked.append(Ranked(make_job(id=HEVO[0], title=HEVO[1]), HEVO[2], max_score=45))
    return [r.job.title for r in order(ranked)]


def test_hevo_capped_job_does_not_jump_above_better_jobs():
    assert live_ranking(0.3)[-1] == HEVO[1]
    # With the old fixed 0.6 weight the weak model dragged three 78s below the capped job.
    assert live_ranking(0.6).index(HEVO[1]) == 2


def test_cap_survives_blending():
    assert blend(45, 95, 0.3, max_score=45) == 45
    assert blend(45, 95, 1.0, max_score=45) == 45
    assert blend(78, 20, 0.3) == 61
    assert blend(78, 20, 0.0) == 78 and blend(78, 20, 1.0) == 20


def test_scored_jobs_lead_only_when_at_least_best_unscored():
    scored_tie = Ranked(make_job(id=1, title="A"), 50, llm_used=True)
    unscored = Ranked(make_job(id=2, title="B"), 50)
    scored_low = Ranked(make_job(id=3, title="C"), 40, llm_used=True)
    got = [r.job.title for r in order([unscored, scored_low, scored_tie])]
    assert got == ["A", "B", "C"]  # tie goes to the scored job; a lower one just sorts by score


def test_no_llm_is_plain_score_order():
    a, b = Ranked(make_job(id=1, title="A"), 40), Ranked(make_job(id=2, title="B"), 70)
    assert [r.job.title for r in order([a, b])] == ["B", "A"]


def test_weight_config():
    assert LLMConfig().weight == 0.3 and LLMConfig().provider == "none"
    for bad in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            LLMConfig(weight=bad)


class Fake:
    host, is_local = "fake", True

    def __init__(self, score):
        self.reply = f'{{"score": {score}, "reasons": ["fits"], "red_flags": []}}'

    def complete(self, system, user, schema):
        return self.reply


def test_rank_jobs_keeps_years_cap_with_enthusiastic_model(conn):
    store.sync_boards(conn, [BoardConfig(source="greenhouse", token="a", company="A")])
    fields = {
        "external_id": "1", "title": "Backend Engineer", "company": "A",
        "location_text": "Bangalore", "description": "Python SQL. Needs 6+ years.",
        "apply_url": "https://x.example/1",
    }  # fmt: skip
    store.upsert_job(conn, 1, JobIn.model_validate(fields), "2026-01-01T00:00:00")
    p = make_profile(experience_years=1, llm={"provider": "ollama", "model": "m", "weight": 1})
    [r], _ = rank_jobs(conn, p, "", Fake(100))
    assert r.llm_used and r.score == 45
