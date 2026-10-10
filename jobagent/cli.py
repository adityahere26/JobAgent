"""Command line: init, fetch, add, rank, show, mark, tailor, apply, ui."""

import argparse
import shutil
import sys
from pathlib import Path

from pydantic import ValidationError

from jobagent import __version__, store
from jobagent.config import (
    ConfigError,
    Paths,
    Profile,
    load_boards,
    load_dotenv,
    load_profile,
    read_resume,
)
from jobagent.db import connect
from jobagent.ingest import ingest_all
from jobagent.llm import LLMError, make_client
from jobagent.models import manual_job, validation_messages
from jobagent.rank import rank_jobs
from jobagent.report import build_report
from jobagent.tailor import import_reply, prepare, render, tailor, write_prompt

EXAMPLES = {
    "profile.example.yaml": "profile.yaml",
    "companies.example.yaml": "companies.yaml",
    "resume.example.md": "resume.md",
    "applicant.example.yaml": "applicant.yaml",
}


def cmd_init(paths: Paths, _args: argparse.Namespace) -> int:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    for src, dst in EXAMPLES.items():
        target = paths.config_dir / dst
        example = paths.config_dir / src
        if target.exists():
            print(f"kept   config/{dst} (already exists)")
        elif example.exists():
            shutil.copyfile(example, target)
            print(f"wrote  config/{dst}")
        else:
            print(f"missing config/{src}")
    print("\nNext: edit config/profile.yaml, config/companies.yaml and config/resume.md.")
    return 0


def cmd_fetch(paths: Paths, _args: argparse.Namespace) -> int:
    boards = load_boards(paths)
    if not boards:
        print("No boards in config/companies.yaml yet. Add some, then run again.")
        return 1
    conn = connect(paths.db)
    store.sync_boards(conn, boards)
    failed = 0
    for o in ingest_all(conn):
        c = o.counts
        if o.status == "failed":
            failed += 1
            print(f"{o.company}: FAILED ({o.error})")
        else:
            note = "" if o.status == "success" else " (partial: nothing expired)"
            print(
                f"{o.company}: {c['seen']} seen, {c['inserted']} new, {c['updated']} changed, "
                f"{c['skipped']} skipped, {c['expired']} expired{note}"
            )
    return 1 if failed else 0


def _read_description(args: argparse.Namespace) -> str:
    if args.description_file:
        return Path(args.description_file).read_text(encoding="utf-8", errors="replace")
    if sys.stdin.isatty():
        print("Paste the job description, then press Ctrl-D on a new line:", file=sys.stderr)
    return sys.stdin.read()


def cmd_add(paths: Paths, args: argparse.Namespace) -> int:
    """Add one job found elsewhere (LinkedIn, Naukri, a company site). The user copies the text."""
    try:
        description = _read_description(args)
    except OSError as exc:
        print(f"Cannot read the description file: {exc.strerror}", file=sys.stderr)
        return 1
    try:
        job = manual_job(args.title, args.company, args.url, args.location, description)
    except ValidationError as exc:
        print("Job not added:", file=sys.stderr)
        for where, msg in validation_messages(exc):
            print(f"  - {where}: {msg}", file=sys.stderr)
        return 1
    conn = connect(paths.db)
    job_id, outcome = store.add_manual_job(conn, job)
    verb = {"inserted": "Added", "updated": "Updated", "unchanged": "Already saved:"}[outcome]
    print(f"{verb} job {job_id}: {job.title} at {job.company}")
    if not job.description:
        print("Warning: no description, so skills and experience cannot be scored.")
    print(f"Next: jobagent rank   or   jobagent show {job_id}")
    return 0


def cmd_rank(paths: Paths, args: argparse.Namespace) -> int:
    profile = load_profile(paths)
    resume = read_resume(paths, profile)
    client = None
    if not args.no_llm:
        try:
            client = make_client(profile.llm)
        except LLMError as exc:
            print(f"LLM disabled: {exc}", file=sys.stderr)
    if client is not None and not client.is_local:
        print(
            f"Note: your profile and resume (contact details removed) are sent to {client.host}.",
            file=sys.stderr,
        )
    conn = connect(paths.db)
    ranked, stats = rank_jobs(conn, profile, resume, client)
    if stats.llm_error:
        print(f"LLM stopped early: {stats.llm_error}. Using heuristic scores.", file=sys.stderr)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    out = paths.reports_dir / "latest.md"
    out.write_text(build_report(ranked, store.get_statuses(conn), args.limit), encoding="utf-8")
    print(
        f"{stats.considered} jobs, {stats.filtered} filtered out, {len(ranked)} matches "
        f"(llm: {stats.llm_scored} new, {stats.llm_cached} cached, {stats.llm_failed} failed)."
    )
    print(f"Report: {out}")
    return 0


def cmd_show(paths: Paths, args: argparse.Namespace) -> int:
    conn = connect(paths.db)
    job = store.get_job(conn, args.job_id)
    if job is None:
        print(f"No job with id {args.job_id}.")
        return 1
    print(f"{job.title} at {job.company}")
    print(f"{job.location_text or 'location not stated'}")
    print(f"Apply: {job.apply_url}\n")
    print(job.description[:3000])
    return 0


def cmd_mark(paths: Paths, args: argparse.Namespace) -> int:
    conn = connect(paths.db)
    try:
        store.set_status(conn, args.job_id, args.status, args.note)
    except ValueError as exc:
        print(str(exc))
        return 1
    print(f"Job {args.job_id} marked {args.status}.")
    return 0


def _tailor_offline(paths: Paths, args: argparse.Namespace, profile: Profile, resume: str) -> int:
    """--prompt writes a prompt to paste anywhere; --import checks the pasted reply. No network."""
    conn = connect(paths.db)
    job = store.get_job(conn, args.job_id)
    if job is None:
        print(f"No job with id {args.job_id}.")
        return 1
    try:
        prep = prepare(job, profile, resume)
    except ValueError as exc:
        print(f"Cannot tailor: {exc}", file=sys.stderr)
        return 1
    if args.prompt:
        out = write_prompt(paths.reports_dir, job, prep)
        print(f"Saved: {out}")
        print("Paste it into a chat model and save the reply, then run:")
        print(f"  jobagent tailor {job.id} --import reply.txt")
        return 0
    try:
        reply = Path(args.import_file).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"Cannot read the reply file: {exc.strerror}", file=sys.stderr)
        return 1
    try:
        result, out = import_reply(paths.reports_dir, job, prep, reply)
    except ValueError as exc:
        print(f"The reply is not usable: {exc}. It must be the JSON object.", file=sys.stderr)
        return 1
    for w in result.warnings:
        print(f"WARNING: {w}")
    print(f"Saved: {out}")
    return 0


def cmd_tailor(paths: Paths, args: argparse.Namespace) -> int:
    profile = load_profile(paths)
    resume = read_resume(paths, profile)
    if not resume.strip():
        print("config/resume.md is empty or missing.", file=sys.stderr)
        return 1
    if args.prompt or args.import_file:
        return _tailor_offline(paths, args, profile, resume)
    try:
        client = make_client(profile.llm)
    except LLMError as exc:
        print(f"Cannot use the LLM: {exc}", file=sys.stderr)
        return 1
    if client is None:
        print("Tailoring needs an LLM. Set llm.provider in config/profile.yaml.", file=sys.stderr)
        return 1
    conn = connect(paths.db)
    job = store.get_job(conn, args.job_id)
    if job is None:
        print(f"No job with id {args.job_id}.")
        return 1
    if not client.is_local:
        print(f"Note: resume text (contacts removed) is sent to {client.host}.", file=sys.stderr)
    rejected: list[str] = []
    try:
        result = tailor(client, job, profile, resume, rejected)
    except (LLMError, ValueError) as exc:
        print(f"Tailoring failed: {exc}", file=sys.stderr)
        return 1
    if result is None:
        print("The model did not return a usable answer:", file=sys.stderr)
        for reason in dict.fromkeys(rejected):
            print(f"  - {reason}", file=sys.stderr)
        print(
            f"Try a stronger model, or `jobagent tailor {job.id} --prompt` to use any chat model.",
            file=sys.stderr,
        )
        return 1
    out_dir = paths.reports_dir / "tailored"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"job-{job.id}.md"
    out.write_text(render(job, result), encoding="utf-8")
    for w in result.warnings:
        print(f"WARNING: {w}")
    print(f"Saved: {out}")
    return 0


def cmd_apply(paths: Paths, args: argparse.Namespace) -> int:
    from jobagent import apply_assist as aa

    conn = connect(paths.db)
    job = store.get_job(conn, args.job_id)
    if job is None:
        print(f"No job with id {args.job_id}.")
        return 1
    values, resume_pdf = aa.prepare(paths, job.id)
    print(f"Opening {job.title} at {job.company}. Nothing is submitted for you.")
    filled, left = aa.run(job.apply_url, values, resume_pdf)
    print(f"Pre-filled: {', '.join(filled) or 'nothing'}")
    if left:
        print(f"Left for you: {', '.join(left)}")
    store.set_status(conn, job.id, "saved", "apply-assist opened; confirm with `mark` once sent")
    print(f"If you submitted it: jobagent mark {job.id} applied")
    return 0


def cmd_ui(paths: Paths, args: argparse.Namespace) -> int:
    from jobagent.web.security import check_bind_host

    check_bind_host(args.host)  # before anything starts: never listen beyond this machine
    try:
        import uvicorn

        from jobagent.web.app import create_app
    except ImportError:
        print("The dashboard needs its extra packages: uv sync --extra ui", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{args.port}"
    print(f"jobagent dashboard on {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, webbrowser.open, [url]).start()
    app = create_app(paths, args.port)
    # No access log: URLs carry search terms, and nothing about jobs belongs in logs.
    uvicorn.run(
        app, host=args.host, port=args.port, log_level="warning", access_log=False,
        server_header=False,
    )  # fmt: skip
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobagent", description="Personal job search agent.")
    p.add_argument("--version", action="version", version=f"jobagent {__version__}")
    p.add_argument("--home", type=Path, default=Path.cwd(), help="project folder (default: here)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create config files from the examples")
    sub.add_parser("fetch", help="download jobs from the boards in companies.yaml")
    ad = sub.add_parser("add", help="add one job you found elsewhere (you paste the text)")
    ad.add_argument("--title", required=True)
    ad.add_argument("--company", required=True)
    ad.add_argument("--url", required=True, help="https link to the job or application page")
    ad.add_argument("--location", default=None, help='e.g. "Bengaluru" or "Remote, India"')
    ad.add_argument(
        "--description-file", default=None, help="text file with the job description (else stdin)"
    )
    r = sub.add_parser("rank", help="filter, score and write reports/latest.md")
    r.add_argument("--no-llm", action="store_true", help="heuristic scores only")
    r.add_argument("--limit", type=int, default=50, help="max jobs in the report")
    s = sub.add_parser("show", help="print one job")
    s.add_argument("job_id", type=int)
    t = sub.add_parser("tailor", help="draft summary, bullets and cover note for one job")
    t.add_argument("job_id", type=int)
    offline = t.add_mutually_exclusive_group()
    offline.add_argument(
        "--prompt", action="store_true", help="write a prompt to paste into any chat model"
    )
    offline.add_argument(
        "--import", dest="import_file", metavar="REPLY", help="check a pasted model reply"
    )
    a = sub.add_parser("apply", help="open the form in a browser and pre-fill it (you submit)")
    a.add_argument("job_id", type=int)
    m = sub.add_parser("mark", help="track a job's status")
    m.add_argument("job_id", type=int)
    m.add_argument("status", choices=store.STATUSES)
    m.add_argument("--note", default="")
    u = sub.add_parser("ui", help="open the local web dashboard")
    u.add_argument("--host", default="127.0.0.1", help="must be 127.0.0.1 (local only)")
    u.add_argument("--port", type=int, default=8000)
    u.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths(args.home)
    load_dotenv(paths.env)
    handlers = {
        "init": cmd_init,
        "fetch": cmd_fetch,
        "add": cmd_add,
        "rank": cmd_rank,
        "show": cmd_show,
        "mark": cmd_mark,
        "tailor": cmd_tailor,
        "apply": cmd_apply,
        "ui": cmd_ui,
    }
    try:
        return handlers[args.command](paths, args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
