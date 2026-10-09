"""Rank stored jobs: hard filters, heuristic score, optional LLM score on the top N."""

import sqlite3
from dataclasses import dataclass, field

from jobagent import store
from jobagent.config import Profile
from jobagent.filters import apply_filters
from jobagent.llm import LLMClient, LLMError
from jobagent.scoring.heuristic import score_job
from jobagent.scoring.llm_scorer import llm_score, profile_hash

HEURISTIC_WEIGHT = 0.4
LLM_WEIGHT = 0.6


@dataclass
class Ranked:
    job: store.Job
    score: int
    reasons: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    llm_used: bool = False


@dataclass
class RankStats:
    considered: int = 0
    filtered: int = 0
    llm_scored: int = 0
    llm_cached: int = 0
    llm_failed: int = 0
    llm_error: str | None = None


def rank_jobs(
    conn: sqlite3.Connection,
    profile: Profile,
    resume: str,
    client: LLMClient | None,
) -> tuple[list[Ranked], RankStats]:
    stats = RankStats()
    ranked: list[Ranked] = []
    for job in store.load_active_jobs(conn):
        stats.considered += 1
        flt = apply_filters(job, profile)
        if not flt.keep:
            stats.filtered += 1
            continue
        s = score_job(job, profile, flt)
        store.save_score(
            conn, job.id, "heuristic", s.score, s.reasons, s.red_flags, job.content_hash, ""
        )
        ranked.append(Ranked(job, s.score, s.reasons, s.red_flags))

    ranked.sort(key=lambda r: (-r.score, -r.job.id))

    if client is not None:
        phash = profile_hash(profile, resume)
        for item in ranked[: profile.llm.top_n]:
            cached = store.get_score(conn, item.job.id, "llm", item.job.content_hash, phash)
            verdict = None
            if cached:
                stats.llm_cached += 1
                verdict = cached
            else:
                try:
                    result = llm_score(client, item.job, profile, resume)
                except LLMError as exc:
                    stats.llm_error = str(exc)
                    break  # endpoint is down or misconfigured: stop wasting time
                if result is None:
                    stats.llm_failed += 1
                    continue
                verdict = {
                    "score": result.score,
                    "reasons": result.reasons,
                    "red_flags": result.red_flags,
                }
                store.save_score(
                    conn,
                    item.job.id,
                    "llm",
                    result.score,
                    result.reasons,
                    result.red_flags,
                    item.job.content_hash,
                    phash,
                )
                stats.llm_scored += 1
            item.score = round(HEURISTIC_WEIGHT * item.score + LLM_WEIGHT * verdict["score"])
            item.reasons = verdict["reasons"] or item.reasons
            item.red_flags = list(dict.fromkeys(item.red_flags + verdict["red_flags"]))
            item.llm_used = True
        ranked.sort(key=lambda r: (-r.score, -r.job.id))

    ranked = [r for r in ranked if r.score >= profile.min_score]
    return ranked, stats
