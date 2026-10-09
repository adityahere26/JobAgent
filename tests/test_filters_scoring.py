from jobagent.filters import apply_filters, contains_phrase
from jobagent.scoring.heuristic import score_job
from tests.helpers import make_job, make_profile


def test_whole_word_match():
    assert contains_phrase("Intern wanted", "intern")
    assert not contains_phrase("Internal tools", "intern")


def test_filters():
    p = make_profile(
        avoid_companies=["Bad Co"], avoid_in_title=["intern"], must_have_any=["python"]
    )
    assert not apply_filters(make_job(company="bad co"), p).keep
    assert not apply_filters(make_job(title="Backend Intern"), p).keep
    assert not apply_filters(make_job(description="java only"), p).keep
    assert apply_filters(make_job(), p).keep


def test_location_rules():
    p = make_profile()
    assert apply_filters(make_job(), p).keep
    assert not apply_filters(make_job(cities=["Pune"], location_text="Pune"), p).keep
    us_remote = make_job(cities=[], countries=["US"], is_remote=True, location_text="Remote - US")
    assert not apply_filters(us_remote, p).keep
    anywhere = make_job(cities=[], countries=[], is_remote=True, location_text="Remote")
    assert apply_filters(anywhere, p).keep
    unknown = make_job(cities=[], countries=[], location_text=None)
    res = apply_filters(unknown, p)
    assert res.keep and res.location_unknown
    assert not apply_filters(anywhere, make_profile(locations=["Bengaluru"])).keep


def test_score_good_vs_bad():
    p = make_profile()
    good = score_job(make_job(), p, apply_filters(make_job(), p))
    other = make_job(title="Sales Manager", description="selling")
    bad = score_job(other, p, apply_filters(other, p))
    assert good.score >= 85 > bad.score
    assert any("title matches" in r for r in good.reasons)


def test_senior_flag():
    p = make_profile(experience_years=1)
    j = make_job(title="Senior Backend Engineer")
    s = score_job(j, p, apply_filters(j, p))
    assert any("senior" in f for f in s.red_flags)


PM_ROLES = [
    "Product Manager",
    "Associate Product Manager",
    "APM",
    "Product Analyst",
    "Product Owner",
]


def _pm_score(title: str):
    p = make_profile(target_roles=PM_ROLES)
    job = make_job(title=title)
    return score_job(job, p, apply_filters(job, p))


def test_generic_words_alone_give_no_partial_title_credit():
    # Real titles that reached a PM top 10 only through "Manager" / "Analyst".
    for title in (
        "Engineering Manager - Data",
        "Data Science Manager, Risk",
        "F&S COE Manager",
        "Manager, Technical Services",
        "Technical Program Manager",
        "Enterprise Risk Management Analyst",
        "Analyst, GTM Analytics",
    ):
        s = _pm_score(title)
        assert "title does not match your target roles" in s.red_flags, title
        assert s.score <= 30, title


def test_distinctive_word_still_gives_partial_credit():
    s = _pm_score("Engineering Manager - Access Essentials - Core Product")
    assert any(r.startswith("title partly matches") for r in s.reasons)
    # ...which is why "engineering" also belongs in avoid_in_title.
    p = make_profile(target_roles=PM_ROLES, avoid_in_title=["engineering", "data science"])
    title = "Engineering Manager - Access Essentials - Core Product"
    assert not apply_filters(make_job(title=title), p).keep
    assert not apply_filters(make_job(title="Data Science Manager, Risk"), p).keep


def test_full_title_matches_unchanged():
    for title in ("Product Analyst II", "Product Manager, Product Platform", "Product Manager"):
        assert any(r.startswith("title matches") for r in _pm_score(title).reasons), title


def _scored(title: str, description: str, years: int = 1):
    p = make_profile(target_roles=PM_ROLES, experience_years=years, skills=[])
    job = make_job(title=title, description=description)
    return score_job(job, p, apply_filters(job, p))


# Experience wording copied from live postings.
def test_over_experienced_jobs_are_capped_at_45():
    for title, text in (
        ("Product Manager II", "What you will need - Overall experience of 4+ yrs with at "
         "least 4+ yrs in product management"),  # Meesho
        ("Product Manager", "The ideal candidate should have at least 10 years of relevant "
         "experience"),  # Sigmoid
    ):  # fmt: skip
        s = _scored(title, text)
        assert s.score <= 45, title
        assert any("score capped at 45" in f for f in s.red_flags), title
        assert _scored(title, text, years=8).score > 45  # no cap with enough experience


def test_en_dash_range_reads_low_end():
    # Paytm APM: "3–5 years" means 3, which is within 2 of 1 year, so no cap.
    s = _scored(
        "Product Management - Associate Product Manager - Travel.",
        "What We Are Looking For: 1) Around 3–5 years of relevant experience",
    )
    assert "experience fits (asks ~3 years)" in s.reasons
    assert not any("capped" in f for f in s.red_flags) and s.score > 45


def test_year_ranges():
    for text in ("3-5 years", "3 – 5 yrs", "3 to 5 years", "3+ years"):
        assert "experience fits (asks ~3 years)" in _scored("Product Manager", text).reasons


def test_years_in_title_are_read():
    # Paytm (live): the years are only in the title; the description never states them.
    s = _scored(
        "Product Management - Lending Product | Experience : 4 -8 yrs",
        "Own the lending product roadmap and work with risk and engineering teams.",
    )
    assert s.score <= 45
    assert any("asks for 4+ years" in f and "capped" in f for f in s.red_flags)
