"""Hard filters: cheap yes/no rules from the profile, applied before any scoring."""

import re
from dataclasses import dataclass

from jobagent.config import Profile
from jobagent.store import Job


def contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word, case-insensitive match (so 'intern' does not match 'internal')."""
    phrase = phrase.strip()
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE) is not None


@dataclass
class FilterResult:
    keep: bool
    reason: str = ""
    location_unknown: bool = False


def location_check(job: Job, profile: Profile) -> tuple[bool, bool]:
    """Returns (ok, unknown). Unknown locations are kept but penalised by the scorer."""
    wants_cities = {c.lower() for c in profile.city_names}
    wants_countries = set(profile.countries)
    if not wants_cities and not profile.allows_remote and not wants_countries:
        return True, False  # no location preference given

    job_cities = {c.lower() for c in job.cities}
    if job_cities & wants_cities:
        return True, False
    if job.is_remote:
        if not profile.allows_remote:
            return False, False
        # Remote limited to other countries (e.g. "Remote - US") is not for this person.
        if job.countries and wants_countries and not (set(job.countries) & wants_countries):
            return False, False
        return True, False
    if not job.cities and not job.countries:
        return True, True
    if wants_countries and set(job.countries) & wants_countries and not wants_cities:
        return True, False
    return False, False


def apply_filters(job: Job, profile: Profile) -> FilterResult:
    if any(job.company.lower() == c.lower() for c in profile.avoid_companies):
        return FilterResult(False, "avoided company")
    if any(contains_phrase(job.title, w) for w in profile.avoid_in_title):
        return FilterResult(False, "avoided word in title")
    if profile.must_have_any:
        haystack = f"{job.title}\n{job.description}"
        if not any(contains_phrase(haystack, w) for w in profile.must_have_any):
            return FilterResult(False, "no must-have keyword")
    ok, unknown = location_check(job, profile)
    if not ok:
        return FilterResult(False, "location")
    return FilterResult(True, location_unknown=unknown)
