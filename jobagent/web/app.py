"""The local dashboard: list, detail, add, tracker and refresh. Reuses store/rank/tailor code.

Job text is untrusted: templates autoescape everything and never mark anything safe, and the
script only ever writes text with textContent.
"""

import importlib.util
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError

from jobagent import apply_assist as aa
from jobagent import store
from jobagent.config import ConfigError, Paths, Profile, load_profile, read_resume
from jobagent.db import connect
from jobagent.filters import apply_filters
from jobagent.models import manual_job, validation_messages
from jobagent.rank import Ranked, rank_jobs
from jobagent.scoring.heuristic import score_job
from jobagent.tailor import Tailored, import_reply, prepare, prompt_path, result_path, write_prompt
from jobagent.web.security import GuardMiddleware, csrf_form, new_csrf_token
from jobagent.web.tasks import FetchTask, Launcher, start_thread

HERE = Path(__file__).parent
STATUS_LABELS = {
    "saved": "Saved",
    "applied": "Applied",
    "interview": "Interview",
    "offer": "Offer",
    "rejected": "Rejected",
    "skipped": "Skipped",
}
QUICK_STATUSES = ("saved", "applied", "skipped")
PAGE_SIZE = 100
# Notices are chosen by key, so nothing from the URL is ever echoed into the page.
MESSAGES = {
    "added": "Job added.",
    "updated": "Job updated with the new text.",
    "unchanged": "That job was already saved.",
    "status": "Status updated.",
    "prompt-ready": "Prompt saved. Copy it into a chat model, then paste the reply below.",
    "apply-opened": "Opening the form in a browser window. Review it and submit it yourself.",
    "refresh-started": "Refreshing jobs. This page will not update by itself; reload when done.",
}

Form = Annotated[dict[str, str], Depends(csrf_form)]


def browser_helper_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


def build_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


@dataclass
class ListFilters:
    min_score: int = 0
    city: str = ""
    remote: bool = False
    status: str = ""  # "", "new", or one of store.STATUSES
    q: str = ""


def parse_int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value or "")))
    except ValueError:
        return default


def filter_ranked(ranked: list[Ranked], statuses: dict[int, str], f: ListFilters) -> list[Ranked]:
    q = f.q.casefold().strip()
    city = f.city.casefold()
    out = []
    for r in ranked:
        job, status = r.job, statuses.get(r.job.id, "")
        if r.score < f.min_score:
            continue
        if city and city not in (c.casefold() for c in job.cities):
            continue
        if f.remote and not job.is_remote:
            continue
        if f.status == "new" and status:
            continue
        if f.status not in ("", "new") and status != f.status:
            continue
        if q and q not in f"{job.title} {job.company}".casefold():
            continue
        out.append(r)
    return out


def safe_next(value: str | None, default: str) -> str:
    """Only same-site paths, so a form cannot redirect elsewhere."""
    if value and value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return default


class RankCache:
    """Heuristic ranking of every active job, recomputed only when jobs or the profile change."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._ranked: list[Ranked] = []

    def get(self, conn: sqlite3.Connection, profile: Profile, profile_mtime: int) -> list[Ranked]:
        key = (store.jobs_fingerprint(conn), profile_mtime)
        with self._lock:
            if key != self._key:
                everything = profile.model_copy(update={"min_score": 0})
                self._ranked, _ = rank_jobs(conn, everything, "", None, save=False)
                self._key = key
            return self._ranked


def create_app(paths: Paths, port: int = 8000, launch: Launcher = start_thread) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GuardMiddleware, port=port)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.csrf = new_csrf_token()
    env = build_env()
    cache = RankCache()
    fetch = FetchTask(paths, launch)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(paths.db)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def page(name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        html = env.get_template(name).render(
            csrf=app.state.csrf, labels=STATUS_LABELS, statuses=store.STATUSES, **ctx
        )
        return HTMLResponse(html, status_code=status_code)

    def notice(msg: str) -> str:
        return MESSAGES.get(msg, "")

    def profile_or_none() -> tuple[Profile | None, str]:
        try:
            return load_profile(paths), ""
        except ConfigError as exc:
            return None, str(exc)

    def job_or_404(conn: sqlite3.Connection, job_id: int) -> store.Job:
        job = store.get_job(conn, job_id)
        if job is None:
            raise HTTPException(404, "No job with that id.")
        return job

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> PlainTextResponse:
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    @app.get("/", response_class=HTMLResponse)
    def jobs_list(
        request: Request,
        conn: Conn,
        min_score: str = "",
        city: str = "",
        remote: str = "",
        status: str = "",
        q: str = "",
        limit: str = "",
        msg: str = "",
    ) -> HTMLResponse:
        profile, problem = profile_or_none()
        if profile is None:
            return page("problem.html", 200, title="Set up your profile", problem=problem)
        ranked = cache.get(conn, profile, paths.profile.stat().st_mtime_ns)
        statuses = store.get_statuses(conn)
        f = ListFilters(
            min_score=parse_int(min_score, profile.min_score, 0, 100),
            city=city,
            remote=remote == "1",
            status=status if status in ("new", *store.STATUSES) else "",
            q=q[:200],
        )
        rows = filter_ranked(ranked, statuses, f)
        shown = parse_int(limit, PAGE_SIZE, 1, 100_000)
        cities = sorted({c for r in ranked for c in r.job.cities})
        params = {k: v for k, v in request.query_params.items() if k not in ("msg", "limit")}
        here = "/?" + urlencode(params) if params else "/"
        more_query = urlencode({**params, "limit": shown + PAGE_SIZE})
        return page(
            "jobs.html",
            title="Jobs",
            nav="jobs",
            rows=rows[:shown],
            total=len(rows),
            all_count=len(ranked),
            shown=shown,
            page_size=PAGE_SIZE,
            f=f,
            cities=cities,
            job_statuses=statuses,
            quick=QUICK_STATUSES,
            here=here,
            more_query=more_query,
            notice=notice(msg),
        )

    def job_page(
        conn: sqlite3.Connection,
        job: store.Job,
        status_code: int = 200,
        message: str = "",
        error: str = "",
        tailor_error: str = "",
        tailored: Tailored | None = None,
        reply: str = "",
    ) -> HTMLResponse:
        profile, problem = profile_or_none()
        score = reasons = flags = None
        hidden = ""
        if profile is not None:
            flt = apply_filters(job, profile)
            if flt.keep:
                s = score_job(job, profile, flt)
                score, reasons, flags = s.score, s.reasons, s.red_flags
            else:
                hidden = flt.reason
        prompt_file = prompt_path(paths.reports_dir, job.id)
        saved_file = result_path(paths.reports_dir, job.id)
        return page(
            "job.html",
            status_code,
            title=f"{job.title} at {job.company}",
            nav="jobs",
            job=job,
            status=store.get_statuses(conn).get(job.id, ""),
            score=score,
            reasons=reasons or [],
            flags=flags or [],
            hidden=hidden,
            problem=problem,
            prompt=prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else "",
            saved=saved_file.read_text(encoding="utf-8") if saved_file.is_file() else "",
            notice=message,
            error=error,
            tailor_error=tailor_error,
            tailored=tailored,
            reply=reply,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(job_id: int, conn: Conn, msg: str = "") -> HTMLResponse:
        return job_page(conn, job_or_404(conn, job_id), message=notice(msg))

    @app.post("/jobs/{job_id}/status")
    def job_status(job_id: int, form: Form, conn: Conn) -> RedirectResponse:
        job = job_or_404(conn, job_id)
        try:
            store.set_status(conn, job.id, form.get("status", ""), form.get("note", "")[:500])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        target = safe_next(form.get("next"), f"/jobs/{job.id}?msg=status")
        return RedirectResponse(target, status_code=303)

    def tailoring_inputs(job: store.Job):
        profile = load_profile(paths)
        resume = read_resume(paths, profile)
        if not resume.strip():
            raise ValueError("config/resume.md is empty or missing")
        return prepare(job, profile, resume)

    @app.post("/jobs/{job_id}/tailor/prompt", response_model=None)
    def tailor_prompt(job_id: int, form: Form, conn: Conn) -> HTMLResponse | RedirectResponse:
        job = job_or_404(conn, job_id)
        try:
            write_prompt(paths.reports_dir, job, tailoring_inputs(job))
        except (ConfigError, ValueError) as exc:
            return job_page(conn, job, 400, tailor_error=f"Cannot tailor: {exc}")
        return RedirectResponse(f"/jobs/{job.id}?msg=prompt-ready#tailor", status_code=303)

    @app.post("/jobs/{job_id}/tailor/import", response_class=HTMLResponse)
    def tailor_import(job_id: int, form: Form, conn: Conn) -> HTMLResponse:
        job = job_or_404(conn, job_id)
        reply = form.get("reply", "")
        try:
            result, _ = import_reply(paths.reports_dir, job, tailoring_inputs(job), reply)
        except (ConfigError, ValueError) as exc:
            return job_page(
                conn,
                job,
                400,
                tailor_error=f"The reply is not usable: {exc}. It must be the JSON object.",
                reply=reply,
            )
        return job_page(conn, job, message="Reply checked and saved.", tailored=result)

    @app.post("/jobs/{job_id}/apply", response_model=None)
    def job_apply(job_id: int, form: Form, conn: Conn) -> HTMLResponse | RedirectResponse:
        job = job_or_404(conn, job_id)
        if not browser_helper_installed():
            return job_page(conn, job, 400, error="Install the browser helper first: "
                            "uv sync --extra apply --extra ui")  # fmt: skip
        try:
            values, resume_pdf = aa.prepare(paths, job.id)
        except ConfigError as exc:
            return job_page(conn, job, 400, error=str(exc))

        def work() -> None:
            # Opens the page and pre-fills it. Never submits: aa.run never clicks anything.
            try:
                aa.run(job.apply_url, values, resume_pdf)
            except Exception as exc:  # the browser was closed early, or failed to start
                print(f"apply-assist for job {job.id} stopped: {type(exc).__name__}")
                return
            done = connect(paths.db)
            try:
                if job.id not in store.get_statuses(done):
                    store.set_status(done, job.id, "saved", "apply-assist opened; mark when sent")
            finally:
                done.close()

        launch(work)
        return RedirectResponse(f"/jobs/{job.id}?msg=apply-opened", status_code=303)

    @app.get("/add", response_class=HTMLResponse)
    def add_form() -> HTMLResponse:
        return page("add.html", title="Add a job", nav="add", values={}, errors=[])

    @app.post("/add", response_model=None)
    def add_submit(form: Form, conn: Conn) -> HTMLResponse | RedirectResponse:
        values = {
            k: form.get(k, "") for k in ("title", "company", "url", "location", "description")
        }
        try:
            job = manual_job(
                values["title"],
                values["company"],
                values["url"],
                values["location"] or None,
                values["description"],
            )
        except ValidationError as exc:
            errors = validation_messages(exc)
            return page("add.html", 400, title="Add a job", nav="add", values=values, errors=errors)
        job_id, outcome = store.add_manual_job(conn, job)
        msg = "added" if outcome == "inserted" else outcome
        return RedirectResponse(f"/jobs/{job_id}?msg={msg}", status_code=303)

    @app.get("/tracker", response_class=HTMLResponse)
    def tracker(conn: Conn) -> HTMLResponse:
        tracked = store.tracked_jobs(conn)
        groups = {s: [t for t in tracked if t.status == s] for s in store.STATUSES}
        return page("tracker.html", title="Tracker", nav="tracker", groups=groups)

    @app.post("/refresh", response_model=None)
    def refresh(request: Request, form: Form) -> JSONResponse | RedirectResponse:
        started = fetch.start()
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"started": started, **fetch.snapshot()})
        return RedirectResponse("/?msg=refresh-started", status_code=303)

    @app.get("/refresh/status")
    def refresh_status() -> JSONResponse:
        return JSONResponse(fetch.snapshot())

    return app
