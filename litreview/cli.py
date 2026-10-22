"""Command line: litreview run | resume | add | attach | search | card | recheck | report | share | refs | runs."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

from .ads import ADSClient, ADSError, build_query, lookup_queries, paper_from_record
from .config import DB_PATH, Settings, load_dotenv
from .library import Library, Paper
from .pipeline import Run, RunBusy, request_additions, resolve_folder
from .verify import parse_draft


def cmd_run(args, lib: Library) -> int:
    settings = Settings(
        question=args.question, since=args.since, until=args.until, refereed_only=args.refereed,
        max_papers=args.max_papers, expand=not args.no_expand, abstracts_only=args.abstracts_only,
        use_batch=not args.no_batch, cost_cap=args.cost_cap,
    )
    run = Run.create(lib, settings, poll_interval=args.poll)
    print(f"run {run.run_id} -> {run.folder}")
    return 0 if run.execute() in ("done", "paused") else 1


def cmd_resume(args, lib: Library) -> int:
    row = lib.run(args.run)
    if row is None:
        print(f"no run named {args.run}", file=sys.stderr)
        return 1
    if args.cost_cap is not None:
        settings = json.loads(row["settings"])
        settings["cost_cap"] = args.cost_cap
        lib.set_settings(args.run, settings)
    try:
        return 0 if Run(lib, args.run, poll_interval=args.poll).execute() in ("done", "paused") else 1
    except RunBusy as e:
        print(e, file=sys.stderr)
        return 1


def cmd_add(args, lib: Library) -> int:
    """Read more papers for a finished review and write it again; the previous version is kept."""
    row = lib.run(args.run)
    if row is None:
        print(f"no run named {args.run}", file=sys.stderr)
        return 1
    settings = json.loads(row["settings"])
    spent = lib.run_cost(args.run)
    if args.cost_cap is not None and args.cost_cap <= spent:
        print(f"the cap must be above the ${spent:.2f} already spent", file=sys.stderr)
        return 1
    try:
        wanted, skipped = request_additions(lib, args.run, args.bibcodes)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    for s in skipped:
        print(f"  {s['bibcode']}: {s['reason']}")
    if not wanted:
        print("nothing to add")
        return 1
    settings["use_batch"] = args.batch
    if args.cost_cap is not None:
        settings["cost_cap"] = args.cost_cap
    lib.set_settings(args.run, settings)
    try:
        return 0 if Run(lib, args.run, poll_interval=args.poll).execute() in ("done", "paused") else 1
    except RunBusy as e:
        print(e, file=sys.stderr)
        return 1


def cmd_search(args, lib: Library) -> int:
    """Run one ADS query with the default filters, cache the records, and list them."""
    ads = ADSClient()
    q = build_query(args.query, args.since, args.until, args.refereed)
    n, docs = ads.search(q, rows=args.rows)
    print(f"{n} results for: {q}")
    for doc in docs:
        p = paper_from_record(doc)
        lib.upsert_paper(p)
        first = p.authors[0].split(",")[0] if p.authors else "?"
        flag = f" [{p.book_kind}]" if p.book_kind else "" if p.refereed else " [preprint]"
        print(f"{p.bibcode}  {p.year or '????'}  {first:<16.16}  {p.title[:90]}{flag}")
    print(f"cached {len(docs)} records in {lib_path(args)}; ADS requests left today: {ads.remaining}")
    return 0


def find_or_fetch_paper(lib: Library, ident: str) -> Paper | None:
    """The library paper a bibcode, arXiv id, DOI or a link to one of them names, looked up in ADS
    and stored if the library doesn't have it yet."""
    from .web.views import find_paper

    queries, ids = lookup_queries(ident)
    if len(ids) != 1:
        return None
    key = ids[0]
    if key.startswith("arXiv:"):
        row = lib.db.execute("SELECT bibcode FROM papers WHERE arxiv_id = ?", (key[6:],)).fetchone()
    elif key.startswith("10."):
        row = lib.db.execute("SELECT bibcode FROM papers WHERE lower(doi) = ?", (key.lower(),)).fetchone()
    else:
        row = None
    p = find_paper(lib, row["bibcode"] if row else key)
    if p is not None:
        return p
    _, docs = ADSClient().search(queries[0], rows=5)
    if not docs:
        return None
    record = paper_from_record(docs[0])
    lib.upsert_paper(record)
    return lib.paper(record.bibcode)


def parse_pages(text: str | None) -> tuple[int, int] | None:
    """A page range: "120-180" -> (120, 180), "120" -> (120, 120), nothing -> None (every page)."""
    if not text:
        return None
    first, _, last = text.replace("–", "-").partition("-")
    if not first.strip().isdigit() or (last and not last.strip().isdigit()):
        raise ValueError(f"pages should look like 120-180, not {text!r}")
    return int(first), int(last or first)


def cmd_attach(args, lib: Library) -> int:
    """Store a PDF of a paper or book as its full text. Reviews that read the paper from then on read
    it; one that read it already reads it again with `litreview add`."""
    from .fulltext import ParseError, upload_pages
    from .papers import attach_pdf

    def fail(message: str) -> int:
        print(json.dumps({"error": message}) if args.json else message, file=sys.stdout if args.json else sys.stderr)
        return 1

    try:
        pages = parse_pages(args.pages)
    except ValueError as e:
        return fail(str(e))
    p = find_or_fetch_paper(lib, args.paper)
    if p is None:
        return fail(f"ADS has no record of {args.paper}; give one bibcode, arXiv id or DOI")
    try:
        text = attach_pdf(lib, p, Path(args.pdf).read_bytes(), pages)
    except OSError as e:
        return fail(f"can't read {args.pdf}: {e.strerror or e}")
    except ParseError as e:
        return fail(f"nothing stored: {e}")
    last = text.paragraphs[-1]["label"]
    if args.json:
        print(json.dumps({"bibcode": p.bibcode, "source": text.source, "version": text.version,
                          "pages": upload_pages(text.version), "paragraphs": len(text.paragraphs),
                          "truncated": text.truncated, "last_label": last}))
        return 0
    where = f"pages {pages[0]}-{pages[1]}" if pages else "every page"
    print(f"{p.bibcode}  {p.title[:80]}")
    print(f"stored {len(text.paragraphs)} paragraphs from {where} of {args.pdf} as its full text")
    if text.truncated:
        print(f"  cut at the length limit after \"{last}\"; choose fewer pages with --pages to read the rest")
    print(f"reviews that read it from now on read this text; to read it again in a finished review: "
          f"litreview add <run> {p.bibcode}")
    return 0


def cmd_card(args, lib: Library) -> int:
    row = lib.card(args.run, args.bibcode)
    if row is None:
        print(f"no card for {args.bibcode} in run {args.run}", file=sys.stderr)
        return 1
    card = json.loads(row["card"])
    text = lib.text(args.bibcode, row["text_source"], row["text_version"])
    paragraphs = text.paragraphs if text else []
    print(f"{args.bibcode}  status: {row['status']}  read: {row['read_depth']}  "
          f"text: {row['text_source']} {row['text_version']}")
    print(f"relevance: {card.get('relevance')} - {card.get('relevance_reason', '')}")
    if row["note"]:
        print(f"notes: {row['note']}")
    for kind in ("methods", "findings", "limitations"):
        for item in card.get(kind, []):
            src = f" ({item['source']})" if "source" in item else ""
            _print_item(f"{kind[:-1].upper()}{src}", item, paragraphs)
    if card.get("limitations_note"):
        print(f"\nlimitations: {card['limitations_note']}")
    return 0


def _print_item(label: str, item: dict, paragraphs: list[dict]) -> None:
    print(f"\n{label}: {item['text']}")
    for i in item["passages"]:
        if 0 <= i < len(paragraphs):
            para = paragraphs[i]
            body = textwrap.shorten(para["text"], 600, placeholder=" ...")
            print(textwrap.indent(f"[{i}] {para['label']}: {body}", "    "))


def cmd_summary(args, lib: Library) -> int:
    paper = lib.paper(args.bibcode)
    rows = lib.summaries(paper.all_bibcodes) if paper else []
    if not rows:
        print(f"no summary for {args.bibcode}; one is written the next time a review reads its full text",
              file=sys.stderr)
        return 1
    row = rows[0]
    summary = json.loads(row["summary"])
    text = lib.text(row["bibcode"], row["text_source"], row["text_version"])
    paragraphs = text.paragraphs if text and text.checksum == row["text_checksum"] else []
    print(f"{paper.bibcode}  {paper.title}")
    print(f"text: {row['text_source']} {row['text_version']}  written by {row['model']} in run {row['run_id']}")
    for kind, label in (("aim", "AIM"), ("methods", "METHOD"), ("results", "RESULT"), ("limitations", "LIMITATION")):
        for item in summary.get(kind, []):
            _print_item(label, item, paragraphs)
    return 0


def cmd_recheck(args, lib: Library) -> int:
    """Re-run rules 4-6 and the chronology check on a hand-edited draft.md, then re-render."""
    run = Run(lib, args.run)
    draft = parse_draft((run.folder / "draft.md").read_text())
    issues, labels = run.check(draft, "hand-edited draft")
    for i in issues:
        print(f"- {i.describe(draft)}")
    for sent, *_ in draft.sentences():
        if labels.get(sent.sid, ("",))[0] == "partial":
            sent.flags.append("partial")
    run.write_outputs(draft)
    print(f"{len(issues)} problems; review.md re-rendered (keys outside the citable set are not rendered)")
    return 0


def cmd_report(args, lib: Library) -> int:
    row = lib.run(args.run)
    if row is None:
        print(f"no run named {args.run}", file=sys.stderr)
        return 1
    path = Run(lib, args.run).folder / "report.md"
    print(path.read_text() if path.exists() else f"no report yet; run status: {row['status']} at {row['stage']}")
    return 0


def cmd_share(args, lib: Library) -> int:
    from .web import share

    row = lib.run(args.run)
    if row is None:
        print(f"no run named {args.run}", file=sys.stderr)
        return 1
    folder = resolve_folder(row["folder"])
    try:
        page = share.page(lib, args.run, folder)
    except share.AssetsMissing as e:
        print(e, file=sys.stderr)
        return 1
    if page is None:
        print(f"no review yet; run status: {row['status']} at {row['stage']}", file=sys.stderr)
        return 1
    out = Path(args.output) if args.output else folder / "share.html"
    out.write_text(page)
    print(f"wrote {out} ({len(page.encode()) / 1e6:.1f} MB); it opens in any browser, with no server")
    return 0


def cmd_refs(args, lib: Library) -> int:
    """Fetch what each paper a run read cites, for the papers stored before litreview kept it."""
    from .papers import fetch_references
    from .web.views import graph_view

    graph = graph_view(lib, args.run)
    if graph is None:
        print(f"no run named {args.run}", file=sys.stderr)
        return 1
    if not graph["missing"]:
        print(f"all {len(graph['nodes'])} papers read already have their citation links")
        return 0
    why = fetch_references(lib, graph["missing"], ADSClient())
    for b, reason in why.items():
        if reason:
            print(f"  {b}: {reason}")
    graph = graph_view(lib, args.run)
    print(f"stored {sum(1 for r in why.values() if not r)} of {len(why)} reference lists; "
          f"{len(graph['edges'])} citation links among the {len(graph['nodes'])} papers read")
    return 0


def cmd_runs(args, lib: Library) -> int:
    for r in lib.runs():
        print(f"{r['run_id']:<60} {r['status']:<8} {r['stage']:<10} ${lib.run_cost(r['run_id']):.2f}")
    return 0


def lib_path(args) -> str:
    return args.db or str(DB_PATH)


def cmd_tasks(args, lib: Library) -> int:
    from .arxiv import ArxivClient
    from functools import cache

    from .openaccess import OpenAccessClient
    from .papers import drain_tasks

    try:
        # One open-access client for every paper, so its per-host waits and refusals carry over.
        n = drain_tasks(lib, arxiv=ArxivClient(), ads=ADSClient, oa=cache(OpenAccessClient))
    except RunBusy:
        print("another process is already working through the tasks", file=sys.stderr)
        return 1
    print(f"{n} task{'s' if n != 1 else ''} done")
    return 0


def cmd_web(args, lib: Library) -> int:
    import uvicorn

    from .web.app import create_app

    lib.close()
    db = args.db or DB_PATH
    mode = "read-only" if args.read_only else "runs reviews"
    print(f"litreview web UI on http://{args.host}:{args.port}/ ({mode}; library {db})")
    uvicorn.run(create_app(db, run_jobs=not args.read_only), host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="litreview", description=__doc__)
    ap.add_argument("--db", help=f"library file (default {DB_PATH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="write a review for a question")
    p.add_argument("question")
    p.add_argument("--since", type=int)
    p.add_argument("--until", type=int)
    p.add_argument("--max-papers", type=int, default=40)
    p.add_argument("--refereed", action="store_true", help="refereed papers only (default: also arXiv preprints and books)")
    p.add_argument("--abstracts-only", action="store_true", help="read abstracts only (the v0 baseline)")
    p.add_argument("--no-expand", action="store_true", help="skip citation-graph expansion")
    p.add_argument("--no-batch", action="store_true", help="extract cards interactively instead of via Batches")
    p.add_argument("--cost-cap", type=float, default=10.0, help="stop before spending more than this (USD)")
    p.add_argument("--poll", type=float, default=30.0, help="seconds between batch status checks; -1 to pause instead")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("resume", help="continue a paused or interrupted run")
    p.add_argument("run")
    p.add_argument("--cost-cap", type=float)
    p.add_argument("--poll", type=float, default=30.0)
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("add", help="read more papers for a finished review and write it again")
    p.add_argument("run")
    p.add_argument("bibcodes", nargs="+")
    p.add_argument("--cost-cap", type=float, help="the run's new cap, counting what it has spent")
    p.add_argument("--batch", action="store_true", help="extract through Batches: half price, can take an hour")
    p.add_argument("--poll", type=float, default=30.0)
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("attach", help="store a PDF of a paper or book (from ADS) as its full text")
    p.add_argument("paper", help="the paper's ADS bibcode, arXiv id or DOI, or a link to one of them")
    p.add_argument("pdf")
    p.add_argument("--pages", help="read only these pages, counting the file's first page as 1: 120-180")
    p.add_argument("--json", action="store_true", help="print the result as one JSON line")
    p.set_defaults(fn=cmd_attach)

    p = sub.add_parser("search", help="run one ADS query with the default filters and cache the hits")
    p.add_argument("query")
    p.add_argument("--since", type=int)
    p.add_argument("--until", type=int)
    p.add_argument("--refereed", action="store_true")
    p.add_argument("--rows", type=int, default=25)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("card", help="show one card beside its source passages")
    p.add_argument("run")
    p.add_argument("bibcode")
    p.set_defaults(fn=cmd_card)

    p = sub.add_parser("summary", help="show a paper's question-independent summary beside its passages")
    p.add_argument("bibcode")
    p.set_defaults(fn=cmd_summary)

    p = sub.add_parser("tasks", help="run paper tasks queued from the web UI (fetch full text, write summaries)")
    p.set_defaults(fn=cmd_tasks)

    p = sub.add_parser("web", help="serve the web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5070)
    p.add_argument("--read-only", action="store_true", help="browse only: don't start or continue reviews")
    p.set_defaults(fn=cmd_web)

    p = sub.add_parser("recheck", help="re-run the checks after hand-editing draft.md")
    p.add_argument("run")
    p.set_defaults(fn=cmd_recheck)

    p = sub.add_parser("report", help="print report.md")
    p.add_argument("run")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("share", help="write the review as one HTML file to send to people")
    p.add_argument("run")
    p.add_argument("-o", "--output", help="where to write it (default runs/<run>/share.html)")
    p.set_defaults(fn=cmd_share)

    p = sub.add_parser("refs", help="fetch the citation links among a run's papers from ADS, where missing")
    p.add_argument("run")
    p.set_defaults(fn=cmd_refs)

    p = sub.add_parser("runs", help="list runs")
    p.set_defaults(fn=cmd_runs)

    args = ap.parse_args(argv)
    lib = Library(lib_path(args))
    try:
        return args.fn(args, lib)
    except ADSError as e:
        print(f"ADS: {e}", file=sys.stderr)
        return 1
    finally:
        lib.close()


if __name__ == "__main__":
    sys.exit(main())
