"""Work on library papers outside any review: read a paper's full text (from arXiv, a scan ADS
hosts, an open-access copy, or a PDF someone uploads), write its question-independent summary, or
fetch what it cites. Reviews read papers the same way; the web UI queues these as tasks,
and `litreview tasks` (started by the UI's worker) runs them one at a time, except reference lists,
which are fetched for every queued paper at once.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .arxiv import ArxivClient, posting_month
from .config import Settings, unpaywall_email
from .extract import build_card, parse_tagged, summary_of, summary_params, verify_card
from .fulltext import ADS_SCAN, RATE_LIMITED, UNPAYWALL, UPLOAD, ParseError, fetch_fulltext, read_pdf
from .library import Library, Paper, StoredText
from .llm import LLM, Refused
from .openaccess import OpenAccessClient, Pdf

SUMMARY_MAX_TOKENS = 16000
TASK_COST_CAP = 1.0  # per task; a summary usually costs well under $0.10
TASKS_LOCK = "_tasks"  # the lock name a process holds while it works through tasks
REFS_BATCH = 100  # reference-list tasks done together, about 3 ADS requests


class RateLimited(Exception):
    """arXiv is throttling this machine, so the paper couldn't be read now."""


def read_full_text(lib: Library, p: Paper, *, arxiv: ArxivClient, ads=None, oa=None) -> tuple[StoredText | None, str]:
    """The paper's full text: stored already, fetched from arXiv (LaTeX, then HTML, then PDF), or
    failing that, a scan ADS hosts or an open-access copy Unpaywall finds (open_access_text). oa()
    gives the OpenAccessClient for those; without one they aren't tried. ads() gives an ADS client,
    for papers whose arXiv id ADS knows only through its full-text links. Returns (text, why not).
    Raises RateLimited if arXiv is throttling this machine, rather than read a poorer copy."""
    cached = lib.best_text(p.all_bibcodes, full_text=True)
    if cached:
        if cached.bibcode == p.bibcode:
            return cached, ""
        return lib.add_text(p.bibcode, cached.source, cached.version, cached.paragraphs, cached.truncated), ""
    if not p.arxiv_id and ads is not None and any(e.startswith("EPRINT") for e in p.esources):
        found = ads().resolver_arxiv_id(p.bibcode)
        if found:
            lib.set_arxiv(p.bibcode, arxiv_id=found, first_date=None if p.first_arxiv_date else posting_month(found))
            p = lib.paper(p.bibcode)
    if p.arxiv_id:
        text, attempts = fetch_fulltext(arxiv, p.arxiv_id, p.arxiv_version)
        for a in attempts:
            lib.log_fetch(p.bibcode, p.arxiv_id, a.source, a.ok, a.reason)
        if text is not None:
            paragraphs = [{"label": x.label, "text": x.text} for x in text.paragraphs]
            return lib.add_text(p.bibcode, text.source, text.version, paragraphs, text.truncated), ""
        if any(a.reason == RATE_LIMITED for a in attempts):
            hosts = ", ".join(sorted(arxiv.throttled))
            raise RateLimited(f"arXiv is rate limiting this machine ({hosts}); try again in an hour or so")
        why = ["nothing readable on arXiv: " + "; ".join(f"{a.source} {a.reason}" for a in attempts)]
    else:
        lib.log_fetch(p.bibcode, None, "arxiv", False, "no arXiv version")
        why = ["no arXiv version"]
    if oa is not None and ("ADS_PDF" in p.esources or p.doi):
        text, more = open_access_text(lib, p, oa())
        if text is not None:
            return text, ""
        why += more
    return None, "; ".join(why) + "; upload a PDF of it to read it in full"


def can_fetch_open_access(p: Paper) -> bool:
    """Whether ADS hosts a scan of the paper, or Unpaywall could be asked about its DOI."""
    return "ADS_PDF" in p.esources or bool(p.doi and unpaywall_email())


def can_fetch(p: Paper) -> bool:
    """Whether a full text could be fetched for the paper, from arXiv or a legal open copy."""
    return bool(p.arxiv_id) or any(e.startswith("EPRINT") for e in p.esources) or can_fetch_open_access(p)


def open_access_text(lib: Library, p: Paper, client: OpenAccessClient) -> tuple[StoredText | None, list[str]]:
    """The scan ADS hosts of the paper, else the first of up to three open-access copies Unpaywall
    knows of that can be read; scanned pages are read by OCR. Returns (text, why not, per source)."""
    why = []
    if "ADS_PDF" in p.esources:
        text, reason = _store_pdf(lib, p, ADS_SCAN, *client.ads_scan(p.bibcode))
        if text is not None:
            return text, []
        why.append(f"ADS scan: {reason}")
    if p.doi and client.email:
        links, reason = client.unpaywall(p.doi)
        if not links:
            lib.log_fetch(p.bibcode, None, UNPAYWALL, False, reason)
            why.append(f"Unpaywall: {reason}")
        for link in links:
            text, reason = _store_pdf(lib, p, UNPAYWALL, *client.pdf(link))
            if text is not None:
                return text, []
            why.append(f"open-access copy at {urlparse(link).hostname}: {reason}")
    return None, why


def _store_pdf(lib: Library, p: Paper, source: str, pdf: Pdf | None, reason: str) -> tuple[StoredText | None, str]:
    if pdf is not None:
        try:
            text = read_pdf(pdf.data, source=source, book=False)
        except ParseError as e:
            reason = str(e)
        else:
            paragraphs = [{"label": x.label, "text": x.text} for x in text.paragraphs]
            stored = lib.add_text(p.bibcode, text.source, text.version, paragraphs, text.truncated)
            lib.log_fetch(p.bibcode, None, source, True, pdf.url)  # a fetch that worked notes where from
            return stored, ""
    lib.log_fetch(p.bibcode, None, source, False, reason)
    return None, reason


def attach_pdf(lib: Library, p: Paper, data: bytes, pages: tuple[int, int] | None = None) -> StoredText:
    """Store an uploaded PDF (all of it, or pages[0] to pages[1]) as the paper's full text. Being the
    newest, it is the text that reviews read from now on. Raises fulltext.ParseError."""
    text = read_pdf(data, pages)
    paragraphs = [{"label": x.label, "text": x.text} for x in text.paragraphs]
    stored = lib.add_text(p.bibcode, text.source, text.version, paragraphs, text.truncated)
    lib.touch_text(stored)  # the same file and pages uploaded again become the newest once more
    lib.log_fetch(p.bibcode, None, UPLOAD, True, "")
    return stored


def summarize_paper(lib: Library, llm: LLM, p: Paper, text: StoredText, *, run_id: str,
                    settings: Settings | None = None) -> tuple[dict | None, list[str]]:
    """Ask for the SUMMARY lines alone, check every line's passages, and store the summary.
    Returns (summary or None, notes on what was dropped)."""
    s = settings or Settings(question="")
    params = summary_params(p, text)
    reply = llm.call("summary", s.models["extract"], s.efforts["extract"], params["messages"],
                     system=params["system"], max_tokens=SUMMARY_MAX_TOKENS)
    card = build_card(p, text, parse_tagged(reply.blocks))
    card, _, notes = verify_card(card, len(text.paragraphs))
    notes = [n for n in notes if n != "no RELEVANCE line"]  # a summary request has none
    summary = summary_of(card)
    if summary:
        lib.put_summary(p.bibcode, text, summary, model=reply.model, run_id=run_id)
    if reply.stop_reason == "max_tokens":
        notes.append("reply cut off at max_tokens")
    return summary, notes


def fetch_references(lib: Library, bibcodes: list[str], ads) -> dict[str, str]:
    """Store what each paper cites, from ADS, asking under all of its bibcodes. Returns bibcode ->
    why it couldn't be stored ("" when it was). ads is an ADSClient. No Claude cost."""
    bibs = {b: (p.all_bibcodes if (p := lib.paper(b)) else [b]) for b in bibcodes}
    found = ads.references([k for ks in bibs.values() for k in ks])
    out = {}
    for b, ks in bibs.items():
        refs = next((found[k] for k in ks if k in found), None)
        if refs is None:
            out[b] = "ADS has no record under this paper's bibcodes"
        else:
            lib.put_references(b, refs)
            out[b] = ""
    return out


def run_task(lib: Library, task, *, arxiv: ArxivClient, ads=None, oa=None, llm_client=None) -> tuple[str, str]:
    """Do one queued task. Returns (status, error) for finish_task."""
    p = lib.paper(task["bibcode"])
    if p is None:
        return "failed", "not in the library"
    try:
        text, why = read_full_text(lib, p, arxiv=arxiv, ads=ads, oa=oa)
    except RateLimited as e:
        return "failed", str(e)
    if text is None:
        return "failed", why
    if task["kind"] == "fetch":
        return "done", ""
    if lib.summary_for(p.all_bibcodes, text) is not None:
        return "done", ""
    run_id = f"task-{task['id']}"
    llm = LLM(lib, run_id, TASK_COST_CAP, client=llm_client)
    try:
        summary, notes = summarize_paper(lib, llm, p, text, run_id=run_id)
    except Refused as e:
        return "failed", f"Claude declined to summarise this paper ({e})"
    if summary is None:
        return "failed", "the reply had no summary result with a cited passage" + (f" ({'; '.join(notes)})" if notes else "")
    return "done", ""


def drain_tasks(lib: Library, *, arxiv: ArxivClient, ads=None, oa=None, llm_client=None, log=print) -> int:
    """Run queued tasks until none are left. A task interrupted by Ctrl-C goes back on the queue."""
    from .pipeline import run_lock

    done = 0
    with run_lock(lib, TASKS_LOCK):
        while (task := lib.claim_task()) is not None:
            if task["kind"] == "refs":
                done += _refs_tasks(lib, [task, *lib.claim_tasks("refs", REFS_BATCH - 1)], ads, log)
                continue
            log(f"task {task['id']}: {task['kind']} {task['bibcode']}")
            try:
                status, error = run_task(lib, task, arxiv=arxiv, ads=ads, oa=oa, llm_client=llm_client)
            except KeyboardInterrupt:
                lib.finish_task(task["id"], "queued")
                raise
            except Exception as e:  # one bad paper mustn't stop the rest
                status, error = "failed", f"{type(e).__name__}: {e}"[:500]
            lib.finish_task(task["id"], status, error)
            log(f"  {status}{': ' + error if error else ''}")
            done += 1
    return done


def _refs_tasks(lib: Library, tasks: list, ads, log) -> int:
    log(f"tasks {tasks[0]['id']}-{tasks[-1]['id']}: citation links for {len(tasks)} papers")
    try:
        if ads is None:
            raise RuntimeError("no ADS client to fetch citation links with")
        why = fetch_references(lib, [t["bibcode"] for t in tasks], ads())
    except KeyboardInterrupt:
        for t in tasks:
            lib.finish_task(t["id"], "queued")
        raise
    except Exception as e:
        why = {t["bibcode"]: f"{type(e).__name__}: {e}"[:500] for t in tasks}
    for t in tasks:
        lib.finish_task(t["id"], "failed" if why[t["bibcode"]] else "done", why[t["bibcode"]])
    log(f"  {sum(1 for t in tasks if not why[t['bibcode']])} stored")
    return len(tasks)
