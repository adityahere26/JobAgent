"""Free, offline 0-100 score from the profile: role 45, skills 35, location 10, experience 10."""

import re
from dataclasses import dataclass, field

from jobagent.config import Profile
from jobagent.filters import FilterResult, contains_phrase
from jobagent.store import Job

# "4+ years", "3-5 yrs", "3–5 years" (en dash, seen live), "3 to 5 years": read the low end.
_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|(?:[-–—]|to)\s*\d{1,2})?\s*(?:years|yrs)\b", re.IGNORECASE)
# A job asking for more than this many years above yours can score at most _OVER_YEARS_CAP.
_YEARS_SLACK = 2
_OVER_YEARS_CAP = 45
_SENIOR = ("senior", "sr.", "lead", "principal", "staff", "head of", "director", "vp")
# Words shared by many unrelated roles; they never count towards a partial title match.
_GENERIC = frozenset(
    {"manager", "engineer", "analyst", "associate", "senior", "lead", "specialist"}
)
_JUNIOR = ("intern", "junior", "jr.", "trainee", "graduate", "entry level", "fresher")


@dataclass
class Scored:
    score: int
    reasons: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)


def _role_points(job: Job, profile: Profile) -> tuple[int, str | None]:
    best, label = 0, None
    for role in profile.target_roles:
        if contains_phrase(job.title, role):
            return 45, f"title matches '{role}'"
        words = [w for w in re.findall(r"\w+", role.lower()) if len(w) > 2]
        if not words:
            continue
        # Only distinctive words earn partial credit, so "Engineering Manager" does not
        # partly match "Product Manager" through "manager" alone.
        hit = sum(1 for w in words if w not in _GENERIC and contains_phrase(job.title, w))
        pts = round(35 * hit / len(words)) if hit else 0
        if pts > best:
            best, label = pts, f"title partly matches '{role}'"
    return best, label


def _years_asked(job: Job) -> int | None:
    years = [int(m.group(1)) for m in _YEARS.finditer(job.description[:4000])]
    return min(years) if years else None


def _experience_points(job: Job, profile: Profile) -> tuple[int, str | None, str | None]:
    need = _years_asked(job)
    title = job.title.lower()
    mine = profile.experience_years
    if any(w in title for w in _SENIOR) and mine < 5:
        return 0, None, "title looks senior for your experience"
    if any(w in title for w in _JUNIOR) and mine >= 4:
        return 0, None, "title looks junior for your experience"
    if need is not None:
        if need > mine + _YEARS_SLACK:
            return 2, None, None  # flagged and capped in score_job
        return 10, f"experience fits (asks ~{need} years)", None
    return 6, None, None


def score_job(job: Job, profile: Profile, flt: FilterResult) -> Scored:
    out = Scored(0)
    total = 0

    role_pts, why = _role_points(job, profile)
    total += role_pts
    if why:
        out.reasons.append(why)

    if profile.skills:
        text = f"{job.title}\n{job.description}"
        found = [s for s in profile.skills if contains_phrase(text, s)]
        total += round(35 * len(found) / len(profile.skills))
        if found:
            out.reasons.append("skills: " + ", ".join(found[:6]))
    else:
        total += 15

    if flt.location_unknown:
        total += 4
        out.red_flags.append("location not stated")
    else:
        total += 10
        if job.is_remote:
            out.reasons.append("remote")
        elif job.cities:
            out.reasons.append("in " + ", ".join(job.cities[:2]))

    pts, why, flag = _experience_points(job, profile)
    total += pts
    if why:
        out.reasons.append(why)
    if flag:
        out.red_flags.append(flag)

    if role_pts == 0:
        # Skills and location alone must not rescue a job whose title is a different role.
        total = min(total, 30)
        out.red_flags.append("title does not match your target roles")

    need = _years_asked(job)
    if need is not None and need > profile.experience_years + _YEARS_SLACK:
        total = min(total, _OVER_YEARS_CAP)
        out.red_flags.append(
            f"asks for {need}+ years, you have {profile.experience_years} "
            f"(score capped at {_OVER_YEARS_CAP})"
        )

    out.score = max(0, min(100, total))
    return out
