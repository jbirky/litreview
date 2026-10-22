"""The litreview web UI: a browser over the library that can also start and continue reviews.

    litreview web                      # http://127.0.0.1:5070
    uvicorn litreview.web.app:create_app --factory --port 5070

Reading never writes to the library. Reviews started or continued here are queued and run one at
a time by a background worker (jobs.py), each in its own `litreview resume` process, which is the
only thing that calls Claude or arXiv. The server itself calls ADS only to look up papers to add to
a review. Requests that change anything, and those lookups, must be JSON from this same origin, so
another website can't start a paid review, or spend ADS requests, through your browser. The one
exception is an uploaded PDF, sent as itself (application/pdf, which browsers also won't send
across sites without asking) and read by a `litreview attach` process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from typing import Literal

from pydantic import BaseModel, Field

from ..ads import QUOTA_FILE, ADSClient, ADSError, lookup_queries, quota_used
from ..config import DB_PATH, RUNS_DIR, Settings
from ..library import Library
from ..pipeline import MAX_ADDED, RERUN_SETTINGS, Run, request_additions, run_is_active
from . import share, views
from .jobs import JobRunner
from .search import SearchIndex

STATIC = Path(__file__).resolve().parent / "ui" / "dist"
UPLOAD_PATH = "/api/papers/upload"
MAX_UPLOAD_BYTES = 300 * 2**20  # a whole textbook with its figures
ATTACH_TIMEOUT = 600  # seconds to read an uploaded PDF
LOOKUP_FIELDS = ["bibcode", "alternate_bibcode", "title", "author", "pubdate", "pub", "doi", "identifier",
                 "doctype", "property", "citation_count"]


def _credits() -> float | None:
    raw = os.environ.get("LITREVIEW_MONTHLY_CREDITS", "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def missing_keys() -> list[str]:
    """Credentials the worker needs and this server's environment lacks."""
    missing = [] if os.environ.get("ADS_API_TOKEN") else ["ADS_API_TOKEN"]
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_PROFILE")):
        missing.append("ANTHROPIC_API_KEY")
    return missing


class NewRun(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    since: int | None = Field(None, ge=1900, le=2100)
    until: int | None = Field(None, ge=1900, le=2100)
    max_papers: int = Field(20, ge=1, le=100)
    refereed_only: bool = False
    expand: bool = True
    abstracts_only: bool = False
    use_batch: bool = True
    cost_cap: float = Field(5.0, gt=0, le=100)


class Rerun(BaseModel):
    """Settings for running a finished review again; those left out keep the review's. A year of null is
    the planner's own."""
    since: int | None = Field(None, ge=1900, le=2100)
    until: int | None = Field(None, ge=1900, le=2100)
    max_papers: int | None = Field(None, ge=1, le=100)
    refereed_only: bool | None = None
    expand: bool | None = None
    abstracts_only: bool | None = None
    use_batch: bool | None = None
    cost_cap: float = Field(gt=0, le=100)


class Resume(BaseModel):
    cost_cap: float | None = Field(None, gt=0, le=100)


class AddPapers(BaseModel):
    bibcodes: list[str] = Field(min_length=1, max_length=MAX_ADDED)
    cost_cap: float = Field(gt=0, le=100)
    use_batch: bool = False


class Lookup(BaseModel):
    q: str = Field(min_length=1, max_length=500)


class TaskRequest(BaseModel):
    kind: Literal["fetch", "summarize", "refs"]
    bibcodes: list[str] = Field(min_length=1, max_length=200)


def create_app(db_path: Path | str | None = None, runs_dir: Path | str | None = None,
               search_path: Path | str | None = None, static_dir: Path | str | None = STATIC,
               run_jobs: bool = True, worker_command=None, task_command=None,
               share_assets: Path | str | None = None, ads_client=ADSClient,
               ads_quota_file: Path | str = QUOTA_FILE, max_upload_bytes: int = MAX_UPLOAD_BYTES) -> FastAPI:
    db_path = Path(db_path or DB_PATH)
    runs_dir = Path(runs_dir or RUNS_DIR)
    Library(db_path).close()  # create any tables a newer litreview added, once, before serving
    index = SearchIndex(search_path or db_path.with_name("search.sqlite"))
    runner = JobRunner(db_path, runs_dir, command=worker_command, task_command=task_command) if run_jobs else None

    @asynccontextmanager
    async def lifespan(app):
        if runner:
            runner.start()
        yield
        if runner:
            runner.stop()

    app = FastAPI(title="litreview", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                return JSONResponse({"detail": "requests from other sites can't change anything here"}, 403)
            kind = request.headers.get("content-type", "").split(";")[0].strip()
            if kind != "application/json" and not (request.url.path == UPLOAD_PATH and kind == "application/pdf"):
                return JSONResponse({"detail": "send JSON"}, 415)
        return await call_next(request)

    def library():
        lib = Library(db_path, schema=False, check_same_thread=False)
        try:
            yield lib
        finally:
            lib.close()

    def found(value, what: str):
        if value is None:
            raise HTTPException(404, f"no {what}")
        return value

    @app.get("/api/overview")
    def overview(lib: Library = Depends(library)):
        return {**views.overview(lib, _credits()), "ads_quota": quota_used(Path(ads_quota_file)),
                "can_run": runner is not None, "missing_keys": missing_keys()}

    @app.get("/api/runs")
    def runs(lib: Library = Depends(library)):
        return [views.run_summary(lib, r) for r in lib.runs()]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, lib: Library = Depends(library)):
        detail = found(views.run_detail(lib, run_id), "run")
        detail["active"] = detail["status"] == "running" and run_is_active(lib, run_id)
        detail["here"] = bool(runner) and runner.state()["current"] == run_id
        detail["can_add"] = detail["can_rerun"] = bool(runner) and detail["status"] == "done" and detail["has_review"]
        detail["cap_pause"] = views.cap_pause(lib, run_id, lib.run(run_id)["error"]) if detail["status"] == "paused" else None
        if detail["status"] == "queued":
            queued = [r["run_id"] for r in lib.db.execute(
                "SELECT run_id FROM runs WHERE status = 'queued' ORDER BY updated_at")]
            detail["queue_position"] = queued.index(run_id) + 1
        return detail

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(0, ge=0), lib: Library = Depends(library)):
        found(lib.run(run_id), "run")
        return [dict(e) for e in lib.events(run_id, after)]

    def need_runner():
        if runner is None:
            raise HTTPException(503, "this server isn't running reviews")

    @app.post("/api/runs", status_code=201)
    def new_run(body: NewRun, lib: Library = Depends(library)):
        need_runner()
        if body.since and body.until and body.since > body.until:
            raise HTTPException(422, "the first year is after the last")
        settings = Settings(**body.model_dump())
        run = Run.create(lib, settings, runs_dir=runs_dir, status="queued")
        run.log("queued from the web UI")
        return views.run_detail(lib, run.run_id)

    @app.post("/api/runs/{run_id}/rerun", status_code=201)
    def rerun(run_id: str, body: Rerun, lib: Library = Depends(library)):
        """Queue a finished review to run again as a new review (Run.rerun)."""
        need_runner()
        row = found(lib.run(run_id), "run")
        if row["status"] != "done":
            raise HTTPException(409, f"the run is {row['status']}; it can be run again once it's finished")
        given = body.model_dump(exclude_unset=True)
        changes = {k: v for k, v in given.items() if k in RERUN_SETTINGS and (v is not None or k in ("since", "until"))}
        try:
            run = Run.rerun(lib, run_id, changes, runs_dir=runs_dir, status="queued")
        except ValueError as e:
            raise HTTPException(422, str(e))
        run.log("queued from the web UI")
        return views.run_detail(lib, run.run_id)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, body: Resume, lib: Library = Depends(library)):
        need_runner()
        row = found(lib.run(run_id), "run")
        if row["status"] not in ("paused", "failed"):
            raise HTTPException(409, f"the run is {row['status']}")
        settings = json.loads(row["settings"])
        if body.cost_cap is not None:
            spent = lib.run_cost(run_id)
            if body.cost_cap <= spent:
                raise HTTPException(422, f"the cap must be above the ${spent:.2f} already spent")
            settings["cost_cap"] = body.cost_cap
            lib.set_settings(run_id, settings)
        lib.put(run_id, "cap_pause", None)
        lib.set_run(run_id, status="queued", error="")
        lib.add_event(run_id, f"queued to continue from the web UI (cap ${settings['cost_cap']:.2f})")
        return views.run_detail(lib, run_id)

    @app.post("/api/runs/{run_id}/add")
    def add_papers(run_id: str, body: AddPapers, lib: Library = Depends(library)):
        """Queue a finished review to read more papers and be written again (pipeline.request_additions)."""
        need_runner()
        row = found(lib.run(run_id), "run")
        if row["status"] != "done":
            raise HTTPException(409, f"the run is {row['status']}; papers can be added once it's finished")
        spent = lib.run_cost(run_id)
        if body.cost_cap <= spent:
            raise HTTPException(422, f"the cap must be above the ${spent:.2f} already spent")
        try:
            wanted, skipped = request_additions(lib, run_id, body.bibcodes)
        except ValueError as e:
            raise HTTPException(409, str(e))
        if not wanted:
            raise HTTPException(422, "; ".join(f"{s['bibcode']}: {s['reason']}" for s in skipped))
        settings = json.loads(row["settings"])
        settings.update(cost_cap=body.cost_cap, use_batch=body.use_batch)
        lib.set_settings(run_id, settings)
        lib.set_run(run_id, status="queued", error="")
        lib.add_event(run_id, f"queued from the web UI to read {len(wanted)} more paper{'s' if len(wanted) > 1 else ''} "
                              f"and write the review again (cap ${body.cost_cap:.2f})")
        return {**views.run_detail(lib, run_id), "skipped": skipped}

    @app.post("/api/runs/{run_id}/lookup")
    def lookup(run_id: str, body: Lookup, lib: Library = Depends(library)):
        """Find papers in ADS to add to a review: by bibcode, arXiv id, DOI, "Author et al. Year" or any
        ADS query. It changes nothing, but it is a POST so that only this page can spend ADS requests."""
        need_runner()
        found(lib.run(run_id), "run")
        queries, ids = lookup_queries(body.q)
        docs, total = [], 0
        try:
            ads = ads_client()
            try:
                for q in queries:
                    n, got = ads.search(q, rows=50 if ids else 20, fl=LOOKUP_FIELDS)
                    docs, total = docs + got, total + n
            finally:
                ads.http.close()
        except ADSError as e:
            raise HTTPException(502, str(e))
        return views.lookup_view(lib, run_id, docs, ids, total)

    @app.post("/api/tasks", status_code=201)
    def add_tasks(body: TaskRequest, lib: Library = Depends(library)):
        """Queue full-text fetches, summaries or citation links for library papers, skipping any already done."""
        need_runner()
        queued, skipped = [], []
        for b in body.bibcodes:
            p = views.find_paper(lib, b)
            if p is None:
                skipped.append({"bibcode": b, "reason": "not in the library"})
                continue
            if body.kind == "refs":
                if lib.references(p.all_bibcodes):
                    skipped.append({"bibcode": p.bibcode, "reason": "its citation links are already stored"})
                else:
                    queued.append({"id": lib.add_task("refs", p.bibcode), "bibcode": p.bibcode})
                continue
            text = lib.best_text(p.all_bibcodes, full_text=True)
            if body.kind == "fetch" and text:
                skipped.append({"bibcode": p.bibcode, "reason": "the full text is already stored"})
            elif body.kind == "summarize" and text and lib.summary_for(p.all_bibcodes, text):
                skipped.append({"bibcode": p.bibcode, "reason": "already summarised"})
            elif not text and not views.can_fetch(p):
                skipped.append({"bibcode": p.bibcode, "reason": "no arXiv version, ADS scan or open-access copy to read"})
            else:
                queued.append({"id": lib.add_task(body.kind, p.bibcode), "bibcode": p.bibcode})
        return {"queued": queued, "skipped": skipped}

    @app.post(UPLOAD_PATH)
    async def upload_pdf(request: Request, bibcode: str, pages: str | None = Query(None, max_length=20),
                         lib: Library = Depends(library)):
        """Store a PDF of a library paper or book (the request body) as its full text: every page, or
        pages "120-180". Reviews read it from then on. No Claude cost."""
        need_runner()
        p = found(views.find_paper(lib, bibcode), "paper")
        if int(request.headers.get("content-length") or 0) > max_upload_bytes:
            raise HTTPException(413, f"PDFs up to {max_upload_bytes // 2**20} MB can be uploaded")
        uploads = db_path.parent / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=uploads, suffix=".pdf", delete=False) as f:
            tmp = Path(f.name)
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_upload_bytes:
                    break
                f.write(chunk)
        try:
            if size > max_upload_bytes:
                raise HTTPException(413, f"PDFs up to {max_upload_bytes // 2**20} MB can be uploaded")
            if not size:
                raise HTTPException(422, "the upload was empty")
            command = [sys.executable, "-m", "litreview", "--db", str(db_path), "attach", p.bibcode, str(tmp),
                       "--json", *(["--pages", pages] if pages else [])]
            try:
                done = await run_in_threadpool(subprocess.run, command, capture_output=True, text=True,
                                               timeout=ATTACH_TIMEOUT, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                raise HTTPException(504, f"reading the PDF took over {ATTACH_TIMEOUT // 60} minutes; "
                                         "choose fewer pages, or run litreview attach")
            try:
                result = json.loads(done.stdout.strip().splitlines()[-1])
            except (IndexError, ValueError):
                raise HTTPException(500, (done.stderr.strip().splitlines() or ["reading the PDF failed"])[-1][:300])
            if "error" in result:
                raise HTTPException(422, result["error"])
            return result
        finally:
            tmp.unlink(missing_ok=True)

    @app.get("/api/tasks")
    def active_tasks(lib: Library = Depends(library)):
        rows = lib.db.execute("SELECT * FROM tasks WHERE status IN ('queued', 'running') ORDER BY id").fetchall()
        return [{"kind": r["kind"], "bibcode": r["bibcode"], **views.task_view(r)} for r in rows]

    @app.post("/api/runs/{run_id}/pause")
    def pause(run_id: str, lib: Library = Depends(library)):
        row = found(lib.run(run_id), "run")
        if row["status"] == "queued":
            lib.set_run(run_id, status="paused", error="taken off the queue")
            lib.add_event(run_id, "taken off the queue")
            return {"paused": True}
        if row["status"] == "running" and runner and runner.interrupt(run_id):
            lib.add_event(run_id, "pausing (requested from the web UI)")
            return {"pausing": True}
        if row["status"] == "running":
            raise HTTPException(409, "it's running in another process, such as the CLI; stop it there")
        raise HTTPException(409, f"the run is {row['status']}")

    @app.get("/api/runs/{run_id}/review")
    def review(run_id: str, lib: Library = Depends(library)):
        found(lib.run(run_id), "run")
        return {"review": views.review_view(lib, run_id)}

    @app.get("/api/runs/{run_id}/graph")
    def graph(run_id: str, lib: Library = Depends(library)):
        return found(views.graph_view(lib, run_id), "run")

    @app.get("/api/runs/{run_id}/share")
    def shared_page(run_id: str, download: bool = False, lib: Library = Depends(library)):
        """The review as one HTML file to send to people (share.py)."""
        row = found(lib.run(run_id), "run")
        try:
            page = share.page(lib, run_id, views.run_folder(runs_dir, row), share_assets)
        except share.AssetsMissing as e:
            raise HTTPException(503, str(e))
        if page is None:
            raise HTTPException(409, "the review isn't written yet")
        how = "attachment" if download else "inline"
        return HTMLResponse(page, headers={"Content-Disposition": f'{how}; filename="{share.filename(run_id)}"'})

    @app.get("/api/runs/{run_id}/papers")
    def run_papers(run_id: str, lib: Library = Depends(library)):
        found(lib.run(run_id), "run")
        return views.run_papers(lib, run_id)

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: str, lib: Library = Depends(library)):
        found(lib.run(run_id), "run")
        return {"markdown": views.report_markdown(runs_dir, lib, run_id)}

    @app.get("/api/cards")
    def card(run: str, bibcode: str, lib: Library = Depends(library)):
        return found(views.card_view(lib, run, bibcode), "card")

    @app.get("/api/papers")
    def paper(bibcode: str, lib: Library = Depends(library)):
        return found(views.paper_view(lib, bibcode), "paper")

    @app.get("/api/text")
    def text(bibcode: str, source: str, version: str, lib: Library = Depends(library)):
        return found(views.text_view(lib, bibcode, source, version), "stored text")

    @app.get("/api/library")
    def library_rows(
        status: str = Query("read", pattern="^(" + "|".join(views.STATUSES) + ")$"),
        refereed: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        q: str = "",
        sort: str = "year",
        desc: bool = True,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        lib: Library = Depends(library),
    ):
        matching = None
        if q.strip():
            index.ensure_fresh(lib.db)
            matching = index.papers(q) or []
        return views.library_view(lib, matching, status=status, refereed=refereed, year_from=year_from,
                                  year_to=year_to, sort=sort, desc=desc, offset=offset, limit=limit)

    @app.get("/api/search")
    def search(q: str, limit: int = Query(50, ge=1, le=200), lib: Library = Depends(library)):
        index.ensure_fresh(lib.db)
        return views.search_view(lib, index.paragraphs(q, limit))

    if static_dir and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    return app
