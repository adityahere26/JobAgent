"""Markdown report. Job text is untrusted, so only short cleaned fields are written."""

from datetime import UTC, datetime

from jobagent.rank import Ranked

_MD_SPECIAL = str.maketrans({c: f"\\{c}" for c in "\\`*_[]<>|#"})


def md_escape(text: str) -> str:
    return text.translate(_MD_SPECIAL)


def build_report(ranked: list[Ranked], statuses: dict[int, str], limit: int = 50) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Job matches ({stamp})", ""]
    if not ranked:
        lines.append("No matching jobs. Try `jobagent fetch`, more boards, or a lower `min_score`.")
        return "\n".join(lines) + "\n"
    lines.append(f"{min(len(ranked), limit)} of {len(ranked)} matches, best first.")
    lines.append("")
    for item in ranked[:limit]:
        job = item.job
        status = statuses.get(job.id)
        tag = f" _[{status}]_" if status else ""
        lines.append(f"## {item.score} · {md_escape(job.title)} at {md_escape(job.company)}{tag}")
        lines.append(f"- id: `{job.id}` · {md_escape(job.location_text or 'location not stated')}")
        lines.append(f"- apply: <{job.apply_url}>")
        if item.reasons:
            lines.append("- why: " + md_escape("; ".join(item.reasons)))
        if item.red_flags:
            lines.append("- watch out: " + md_escape("; ".join(item.red_flags)))
        lines.append("")
    return "\n".join(lines)
