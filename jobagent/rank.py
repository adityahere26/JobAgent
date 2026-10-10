"""Rank stored jobs: hard filters, heuristic score, optional LLM score on the top N."""

import sqlite3
from dataclasses import dataclass, field

from jobagent import store
from jobagent.config import Profile
from jobagent.filters import apply_filters
from jobagent.llm import LLMClient, LLMError
from jobagent.scoring.heuristic import score_job
from jobagent.scoring.llm_scorer import llm_score, profile_hash


@dataclass
class Ranked:
    job: store.Job
    score: int
    reasons: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    llm_used: bool = False
    max_score: int = 100


def blend(heuristic: int, llm: int, weight: float, max_score: int = 100) -> int:
    """(1-w)*heuristic + w*llm, never above the heuristic's rule cap (e.g. 45 for years)."""
    return min(round((1 - weight) * heuristic + weight * llm), max_score)


def order(ranked: list[Ranked]) -> list[Ranked]:
    """Best first. If any job was LLM-scored, those at or above the best unscored job lead.

    Jobs outside top_n were never shown to the model, so on a tie they go below scored ones.
    """
    by_score = sorted(ranked, key=lambda r: (-r.score, -r.job.id))
    if not any(r.llm_used for r in ranked):
        return by_score
    best_unscored = max((r.score for r in ranked if not r.llm_used), default=-1)
    lead = [r for r in by_score if r.llm_used and r.score >= best_unscored]
    lead_ids = {id(r) for r in lead}
    return lead + [r for r in by_score if id(r) not in lead_ids]


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
    save: bool = True,
) -> tuple[list[Ranked], RankStats]:
    """Filter and score active jobs. With save=False nothing is written (used by the web UI)."""
    stats = RankStats()
    ranked: list[Ranked] = []
    for job in store.load_active_jobs(conn):
        stats.considered += 1
        flt = apply_filters(job, profile)
        if not flt.keep:
            stats.filtered += 1
            continue
        s = score_job(job, profile, flt)
        if save:
            store.save_score(
                conn, job.id, "heuristic", s.score, s.reasons, s.red_flags, job.content_hash, ""
            )
        ranked.append(Ranked(job, s.score, s.reasons, s.red_flags, max_score=s.max_score))

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
            item.score = blend(item.score, verdict["score"], profile.llm.weight, item.max_score)
            item.reasons = verdict["reasons"] or item.reasons
            item.red_flags = list(dict.fromkeys(item.red_flags + verdict["red_flags"]))
            item.llm_used = True
        ranked = order(ranked)

    ranked = [r for r in ranked if r.score >= profile.min_score]
    return ranked, stats
