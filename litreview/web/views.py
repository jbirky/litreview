"""JSON views of the library for the web UI. Everything here reads; nothing writes to the library.

Review text is sent as segments, not HTML: plain text pieces and citation groups. The browser
renders text as text, so nothing the model wrote is ever interpreted as markup, and the only
citations it can show are keys in the run's citable set.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from ..ads import paper_from_record
from ..fulltext import ADS_SCAN, UNPAYWALL, UPLOAD, is_ocr, upload_pages
from ..library import Library, Paper, StoredText
from ..papers import can_fetch
from ..pipeline import has_newer_text
from ..render import STRAY_KEY, Labels, cite_year, journal_abbrev, last_name, short_authors
from ..verify import KEY, KEY_GROUP, Draft, parse_draft, periods_of, text_problems

SOURCE_NAMES = {"arxiv_src": "LaTeX source", "arxiv_html": "arXiv HTML", "arxiv_pdf": "arXiv PDF",
                ADS_SCAN: "ADS scan", UNPAYWALL: "open-access copy", UPLOAD: "uploaded PDF", "abstract": "abstract"}
STATUSES = ("all", "found", "screened", "read", "carded", "cited")


# --- papers ---------------------------------------------------------------------------------

def find_paper(lib: Library, bibcode: str) -> Paper | None:
    """The paper a bibcode belongs to. A preprint record that another paper lists as an alternate
    resolves to that paper, as in the library table."""
    for row in lib.db.execute("SELECT bibcode FROM papers WHERE alternate_bibcodes LIKE ?", (f'%"{bibcode}"%',)):
        owner = lib.paper(row["bibcode"])
        if owner and bibcode in owner.alternate_bibcodes:
            return owner
    return lib.paper(bibcode)


def venue(p: Paper) -> str:
    """What to show where a journal goes: the journal, "book" or "book chapter", or "" for a preprint."""
    return p.book_kind or (journal_abbrev(p) if p.refereed else "")


def paper_brief(p: Paper) -> dict:
    return {
        "bibcode": p.bibcode,
        "title": p.title,
        "first_author": last_name(p.authors[0]) if p.authors else "",
        "n_authors": len(p.authors),
        "year": p.year,
        "pub_year": p.pub_year,
        "journal": venue(p),
        "kind": p.book_kind,
        "refereed": p.refereed,
        "arxiv_id": p.arxiv_id,
        "citation_count": p.citation_count,
    }


def text_label(source: str, version: str) -> str:
    """A stored text's name: "LaTeX source 2101.01234v3", "uploaded PDF, pages 120–180", "ADS scan, OCR"."""
    name = SOURCE_NAMES.get(source, source)
    if source.startswith("arxiv_"):
        return f"{name} {version}"
    if source == "abstract":
        return name
    pages = upload_pages(version)
    return name + (f", pages {pages[0]}–{pages[1]}" if pages else "") + (", OCR" if is_ocr(version) else "")


def text_origin(lib: Library, bibcode: str, source: str) -> str | None:
    """Where a downloaded PDF came from (an ADS scan or an open-access copy), from the fetch log."""
    if source not in (ADS_SCAN, UNPAYWALL):
        return None
    row = lib.db.execute("SELECT reason FROM fetch_log WHERE bibcode = ? AND source = ? AND ok = 1 ORDER BY at DESC LIMIT 1",
                         (bibcode, source)).fetchone()
    return row["reason"] if row and row["reason"].startswith("http") else None


def task_view(row) -> dict | None:
    return {"id": row["id"], "status": row["status"], "error": row["error"], "updated_at": row["updated_at"]} if row else None


def latest_tasks_for(lib: Library, p: Paper) -> dict:
    """The newest fetch and summary task for a paper, under any of its bibcodes."""
    latest = lib.latest_tasks(p.all_bibcodes)
    out = {}
    for kind in ("fetch", "summarize"):
        rows = [latest[(b, kind)] for b in p.all_bibcodes if (b, kind) in latest]
        out[kind] = task_view(max(rows, key=lambda r: r["id"])) if rows else None
    return out


def links(p: Paper) -> dict:
    out = {"ads": f"https://ui.adsabs.harvard.edu/abs/{quote(p.bibcode, safe='')}/abstract"}
    if p.arxiv_id:
        out["arxiv"] = f"https://arxiv.org/abs/{p.arxiv_id}"
    if p.doi:
        out["doi"] = f"https://doi.org/{p.doi}"
    return out


def label_items(items: list[dict], labels: list[str] | None) -> list[dict]:
    """Card or summary items with each passage index paired with its paragraph label."""
    out = []
    for it in items:
        passages = [{"i": i, "label": labels[i] if labels and 0 <= i < len(labels) else None}
                    for i in it.get("passages", [])]
        entry = {"text": it["text"], "passages": passages, "quotes": it.get("passage_text", [])}
        if "source" in it:
            entry["source"] = it["source"]
        out.append(entry)
    return out


def text_ref(text: StoredText | None, source: str, version: str, checksum: str) -> dict:
    """Which stored text an item's passages point into, and whether it is still byte-for-byte that text."""
    return {"source": source, "version": version, "source_name": SOURCE_NAMES.get(source, source),
            "label": text_label(source, version), "matches": bool(text and text.checksum == checksum)}


# --- runs -----------------------------------------------------------------------------------

def run_folder(runs_dir: Path, row) -> Path:
    # Stored as an absolute path on the machine that ran it; the container mounts runs/ elsewhere.
    return runs_dir / Path(row["folder"]).name


def cited_in_run(lib: Library, run_id: str) -> set[str]:
    md = lib.get(run_id, "final_draft")
    return parse_draft(md).keys() & lib.citable(run_id) if md else set()


def run_summary(lib: Library, row) -> dict:
    run_id = row["run_id"]
    cards = lib.cards(run_id)
    return {
        "run_id": run_id,
        "question": row["question"],
        "status": row["status"],
        "stage": row["stage"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "cost": round(lib.run_cost(run_id), 4),
        "selected": len(lib.get(run_id, "selected", []) or []),
        "cards": len(cards),
        "verified": sum(1 for c in cards if c["status"] == "verified"),
        "cited": len(cited_in_run(lib, run_id)),
        "has_review": lib.get(run_id, "final_draft") is not None,
    }


CAP_MESSAGE = re.compile(r"next call \(~\$([\d.]+)\) would take the run past its \$([\d.]+) cap \(spent \$([\d.]+)\)")


def cap_pause(lib: Library, run_id: str, error: str) -> dict | None:
    """Why a run stopped at its cost cap. Runs paused before litreview recorded this get it from
    the pause message."""
    pause = lib.get(run_id, "cap_pause")
    if pause is None and (m := CAP_MESSAGE.search(error or "")):
        pause = {"estimate": float(m.group(1)), "cap": float(m.group(2)), "spent": float(m.group(3))}
    return pause


def run_detail(lib: Library, run_id: str) -> dict | None:
    row = lib.run(run_id)
    if row is None:
        return None
    out = run_summary(lib, row)
    settings = json.loads(row["settings"])
    plan = lib.get(run_id, "plan") or {}
    interpretation = plan.get("interpretation")
    out.update({
        "settings": {k: settings.get(k) for k in ("since", "until", "max_papers", "refereed_only", "expand",
                                                   "abstracts_only", "use_batch", "cost_cap", "models", "efforts")},
        "interpretation": interpretation if interpretation and not text_problems(interpretation) else None,
        "since": plan.get("since"),
        "until": plan.get("until"),
        "queries": plan.get("queries", []),
        "error": (row["error"] or "").split("\n", 1)[0],
        "resume": f"litreview resume {run_id}" if row["status"] in ("paused", "failed") else None,
        "additions": [{"at": a["requested_at"], "added": a.get("added"), "reread": a.get("reread"),
                       "not_found": a.get("not_found"), "previous": a.get("previous"), "done": a["done"]}
                      for a in lib.get(run_id, "additions", []) or []],
    })
    return out


def _segments(text: str, labels: Labels, citable: set[str]) -> list[dict]:
    """Model text -> text pieces and citation groups. Keys outside the citable set never become
    citations, and stray @keys outside a group are dropped, as in review.md."""
    out: list[dict] = []
    pos = 0
    for m in KEY_GROUP.finditer(text):
        if m.start() > pos:
            out.append({"t": "text", "v": STRAY_KEY.sub("", text[pos:m.start()])})
        items = []
        for k in KEY.findall(m.group(1)):
            if k in citable and k in labels.labels:
                items.append({"bibcode": k, "label": labels[k]})
            else:
                items.append({"invalid": True})
        out.append({"t": "cite", "items": items})
        pos = m.end()
    if pos < len(text):
        out.append({"t": "text", "v": STRAY_KEY.sub("", text[pos:])})
    return [s for s in out if s["t"] == "cite" or s["v"]]


def review_view(lib: Library, run_id: str) -> dict | None:
    """The checked review, structured like review.md, for the reader page."""
    md = lib.get(run_id, "final_draft")
    if md is None:
        return None
    draft: Draft = parse_draft(md)
    citable = lib.citable(run_id)
    papers = lib.papers(sorted(draft.keys() & citable))
    cited = sorted(k for k in draft.keys() if k in citable and k in papers)
    labels = Labels({k: papers[k] for k in cited})
    partial = set(lib.get(run_id, "partial", []) or [])
    reasons = lib.get(run_id, "partial_reasons", {}) or {}

    def sentences(block) -> list[dict]:
        return [{"segments": _segments(s.text, labels, citable), "partial": s.text in partial,
                 "reason": reasons.get(s.text, "")} for s in block.sentences]

    blocks = []
    for b in draft.blocks:
        if b.kind in ("h2", "h3"):
            blocks.append({"kind": b.kind, "heading": _segments(b.heading, labels, citable)})
        elif b.key and (b.key not in citable or b.key not in papers):
            continue  # an entry for a paper that can't be cited, and what continues it
        elif b.kind == "entry":
            p, label = papers[b.key], labels[b.key]
            suffix = label[-1] if label[-1].isalpha() and label[-1].islower() else ""
            blocks.append({"kind": "entry", "sentences": sentences(b), "entry": {
                "bibcode": b.key, "year": p.year, "suffix": suffix, "name": label.rsplit(" ", 1)[0],
                "journal": venue(p) or None,
                "journal_year": p.pub_year if venue(p) and p.pub_year != p.year else None,
            }})
        elif b.sentences:
            blocks.append({"kind": b.kind, "sentences": sentences(b), "depth": b.depth})

    cards = {r["bibcode"]: r for r in lib.cards(run_id)}
    texts = lib.get(run_id, "texts", {}) or {}
    read = [b for b in (lib.get(run_id, "selected", []) or []) if b in cards]
    full = sum(1 for b in read if cards[b]["read_depth"] == "full_text")
    sources: dict[str, int] = {}
    for b in cited:
        src = SOURCE_NAMES.get(texts.get(b, {}).get("source", ""), "unknown")
        sources[src] = sources.get(src, 0) + 1

    periods = []
    for per in periods_of(draft, strict=False) or []:
        periods.append({"start": per.start, "end": per.end, "theme": per.theme,
                        "entries": [e.key for e in per.entries if e.key in labels.labels]})
    references = []
    for k in sorted(cited, key=lambda k: labels[k]):
        p = papers[k]
        references.append({**paper_brief(p), "label": labels[k], "authors": p.authors[:3],
                           "et_al": len(p.authors) > 3, "cite_year": cite_year(p), "pub": p.pub,
                           "sort_date": p.sort_date})
    return {
        "blocks": blocks,
        "stats": {"cited": len(cited), "read": len(read), "full_text": full,
                  "abstract_only": len(read) - full, "sources": sources},
        "periods": periods,
        "references": references,
        "notes": lib.get(run_id, "final_notes", []) or [],
    }


def card_view(lib: Library, run_id: str, bibcode: str) -> dict | None:
    row = lib.card(run_id, bibcode)
    if row is None:
        return None
    card = json.loads(row["card"])
    text = lib.text(bibcode, row["text_source"], row["text_version"])
    ref = text_ref(text, row["text_source"], row["text_version"], row["text_checksum"])
    labels = [p["label"] for p in text.paragraphs] if ref["matches"] else None
    p = lib.paper(bibcode)
    return {
        "run_id": run_id,
        "bibcode": bibcode,
        "paper": paper_brief(p) if p else {"bibcode": bibcode},
        "links": links(p) if p else {},
        "status": row["status"],
        "read_depth": row["read_depth"],
        "text": ref,
        "relevance": card.get("relevance"),
        "relevance_reason": card.get("relevance_reason", ""),
        "methods": label_items(card.get("methods", []), labels),
        "findings": label_items(card.get("findings", []), labels),
        "limitations": label_items(card.get("limitations", []), labels),
        "limitations_note": card.get("limitations_note"),
        "note": row["note"],
    }


def added_papers(lib: Library, run_id: str) -> tuple[set[str], set[str]]:
    """(papers added to the finished review, papers it read again from a newer text)."""
    additions = lib.get(run_id, "additions", []) or []
    return ({b for a in additions for b in a.get("added") or [] if b not in (a.get("reread") or [])},
            {b for a in additions for b in a.get("reread") or []})


def run_papers(lib: Library, run_id: str) -> list[dict]:
    """Every paper the run screened or read, with how far it got and why it was read."""
    selected = lib.get(run_id, "selected", []) or []
    texts = lib.get(run_id, "texts", {}) or {}
    cards = {r["bibcode"]: r for r in lib.cards(run_id)}
    cited = cited_in_run(lib, run_id)
    order = {m["bibcode"]: i for i, m in enumerate(lib.get(run_id, "merged", []) or [])}
    added, reread = added_papers(lib, run_id)
    for_citations = lib.get(run_id, "chosen_for_citations", {}) or {}
    screened = {s["bibcode"]: s for s in lib.screening(run_id)}
    rows = []
    # Papers added to a finished review may never have been screened.
    for b in [*screened, *(b for b in selected if b not in screened)]:
        p = lib.paper(b)
        if p is None:
            continue
        s, t, c = screened.get(b), texts.get(b), cards.get(b)
        rows.append({**paper_brief(p), "score": s["score"] if s else None, "reason": s["reason"] if s else "",
                     "round": s["round"] if s else None, "included": bool(s["include"]) if s else False,
                     "selected": b in selected, "added": b in added, "reread": b in reread,
                     "cited_by": for_citations.get(b),
                     "text": SOURCE_NAMES.get(t["source"], t["source"]) if t else None,
                     "newer_text": b in selected and has_newer_text(lib, texts, b),
                     "card": c["status"] if c else None, "cited": b in cited, "rank": order.get(b, 10**9)})
    rows.sort(key=lambda r: (not r["cited"], not r["selected"], -(r["score"] or 0), r["rank"]))
    return rows


def report_markdown(runs_dir: Path, lib: Library, run_id: str) -> str | None:
    row = lib.run(run_id)
    if row is None:
        return None
    path = run_folder(runs_dir, row) / "report.md"
    return path.read_text() if path.exists() else None


GAP_MIN = 2  # how many of the papers read must cite a paper before it is listed as a gap
GAPS_SHOWN = 15


def read_papers(lib: Library, run_id: str) -> list[str]:
    cards = {r["bibcode"] for r in lib.cards(run_id)}
    return [b for b in (lib.get(run_id, "selected", []) or []) if b in cards]


def _search_status(lib: Library, run_id: str, alias: dict[str, str]) -> dict[str, dict]:
    """Canonical bibcode -> what this run's searches and screening made of it."""
    out: dict[str, dict] = {}
    for r in lib.hits(run_id):
        out.setdefault(alias.get(r["bibcode"], r["bibcode"]), {"status": "unscreened"})
    for r in lib.screening(run_id):
        out[alias.get(r["bibcode"], r["bibcode"])] = {
            "status": "included" if r["include"] else "excluded", "score": r["score"], "reason": r["reason"]}
    return out


def graph_view(lib: Library, run_id: str) -> dict | None:
    """Who cites whom among the papers a run read, and the papers several of them cite that the run
    didn't read. Edges come from stored ADS reference lists; papers without one are listed as missing."""
    if lib.run(run_id) is None:
        return None
    read = read_papers(lib, run_id)
    papers = lib.papers(read)
    cited = cited_in_run(lib, run_id)
    labels = Labels({b: papers[b] for b in cited if b in papers})
    node_of = {k: b for b, p in papers.items() for k in p.all_bibcodes}
    alias = lib.aliases()
    stored = lib.references(list(node_of))

    edges: set[tuple[str, str]] = set()
    citers: dict[str, set[str]] = {}
    missing = []
    for b, p in papers.items():
        lists = [stored[k] for k in p.all_bibcodes if k in stored]
        if not lists:
            missing.append(b)
        for r in {r for refs in lists for r in refs}:
            target = node_of.get(r) or node_of.get(alias.get(r, r))
            if target and target != b:
                edges.add((b, target))
            elif not target:
                citers.setdefault(alias.get(r, r), set()).add(b)

    nodes = []
    for b, p in papers.items():
        nodes.append({**paper_brief(p), "label": labels.labels.get(b) or f"{short_authors(p)} {cite_year(p)}",
                      "sort_date": p.sort_date, "cited": b in cited, "has_refs": b not in missing,
                      "cites": sum(1 for e in edges if e[0] == b), "cited_by": sum(1 for e in edges if e[1] == b)})

    status = _search_status(lib, run_id, alias)
    not_read = {b for b, _, _ in lib.get(run_id, "not_read", []) or []}
    gaps = []
    for b, by in sorted(citers.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(by) < GAP_MIN or len(gaps) == GAPS_SHOWN:
            break
        p = lib.paper(b)
        found = status.get(b, {"status": "not_found"})
        if found["status"] == "included" and b in not_read:
            found = {**found, "status": "not_read"}
        gaps.append({"bibcode": b, "by": sorted(by), "in_library": p is not None,
                     "label": f"{short_authors(p)} {cite_year(p)}" if p else None, "title": p.title if p else None,
                     "year": p.year if p else int(b[:4]) if b[:4].isdigit() else None, **found})
    return {"nodes": nodes, "edges": sorted(edges), "missing": missing, "gaps": gaps}


def lookup_view(lib: Library, run_id: str, docs: list[dict], ids: list[str], total: int) -> dict:
    """Papers an ADS lookup returned, with what this review made of each (read, screened, or new to
    it) and how many of the papers it read cite each one, those cited most first."""
    alias = lib.aliases()
    status = _search_status(lib, run_id, alias)
    selected = set(lib.get(run_id, "selected", []) or [])
    read = lib.papers(read_papers(lib, run_id))
    stored = lib.references([k for p in read.values() for k in p.all_bibcodes])
    citers: dict[str, set[str]] = {}
    for b, p in read.items():
        for r in {r for k in p.all_bibcodes for r in stored.get(k, [])}:
            citers.setdefault(r, set()).add(b)

    results, seen = [], set()
    for doc in docs:
        record = paper_from_record(doc)
        b = next((alias[k] for k in record.all_bibcodes if k in alias), record.bibcode)
        if b in seen:
            continue
        seen.add(b)
        p = lib.paper(b)
        found = status.get(b, {"status": "in_library" if p else "new"})
        if b in selected:
            found = {**found, "status": "read"}
        by = set().union(*(citers.get(k, set()) for k in {*record.all_bibcodes, *(p.all_bibcodes if p else [])}))
        results.append({**paper_brief(p or record), **found, "cited_by": len(by - {b}),
                        "ads": f"https://ui.adsabs.harvard.edu/abs/{quote(b, safe='')}/abstract"})
    results.sort(key=lambda r: -r["cited_by"])

    known = {i.lower() for doc in docs for i in [doc["bibcode"], *(doc.get("identifier") or []),
                                                 *(doc.get("alternate_bibcode") or [])]}
    return {"results": results, "total": total, "not_found": [i for i in ids if i.lower() not in known]}


# --- paper page -----------------------------------------------------------------------------

def paper_view(lib: Library, bibcode: str) -> dict | None:
    p = find_paper(lib, bibcode)
    if p is None:
        return None
    marks = ",".join("?" * len(p.all_bibcodes))
    texts = []
    for t in lib.db.execute(
        f"SELECT bibcode, source, version, paragraphs, truncated, fetched_at FROM texts "
        f"WHERE bibcode IN ({marks}) ORDER BY fetched_at DESC", p.all_bibcodes
    ):
        texts.append({"bibcode": t["bibcode"], "source": t["source"], "version": t["version"],
                      "source_name": SOURCE_NAMES.get(t["source"], t["source"]),
                      "label": text_label(t["source"], t["version"]), "origin": text_origin(lib, t["bibcode"], t["source"]),
                      "paragraphs": len(json.loads(t["paragraphs"])), "truncated": bool(t["truncated"]),
                      "fetched_at": t["fetched_at"]})

    summary = None
    rows = lib.summaries(p.all_bibcodes)
    if rows:
        s = rows[0]
        text = lib.text(s["bibcode"], s["text_source"], s["text_version"])
        ref = text_ref(text, s["text_source"], s["text_version"], s["text_checksum"])
        labels = [x["label"] for x in text.paragraphs] if ref["matches"] else None
        data = json.loads(s["summary"])
        summary = {"bibcode": s["bibcode"], "text": ref, "model": s["model"], "run_id": s["run_id"],
                   "created_at": s["created_at"],
                   **{k: label_items(data.get(k, []), labels) for k in ("aim", "methods", "results", "limitations")}}

    cards = []
    for c in lib.db.execute(
        f"SELECT cards.*, runs.question FROM cards JOIN runs USING (run_id) WHERE bibcode IN ({marks}) "
        f"ORDER BY runs.created_at DESC", p.all_bibcodes
    ):
        view = card_view(lib, c["run_id"], c["bibcode"])
        view["question"] = c["question"]
        view["cited"] = c["bibcode"] in cited_in_run(lib, c["run_id"])
        cards.append(view)

    screening = [
        {"run_id": r["run_id"], "question": r["question"], "score": r["score"], "reason": r["reason"],
         "round": r["round"], "included": bool(r["include"])}
        for r in lib.db.execute(
            f"SELECT screening.*, runs.question FROM screening JOIN runs USING (run_id) "
            f"WHERE bibcode IN ({marks}) ORDER BY runs.created_at DESC", p.all_bibcodes)
    ]
    return {
        **paper_brief(p),
        "authors": p.authors,
        "pub": p.pub,
        "pubdate": p.pubdate,
        "sort_date": p.sort_date,
        "first_arxiv_date": p.first_arxiv_date,
        "arxiv_version": p.arxiv_version,
        "doi": p.doi,
        "doctype": p.doctype,
        "alternate_bibcodes": p.alternate_bibcodes,
        "abstract": p.abstract,
        "abstract_source": "arXiv" if p.arxiv_abstract else ("ADS" if p.ads_abstract else None),
        "links": links(p),
        "can_fetch": can_fetch(p),
        "tasks": latest_tasks_for(lib, p),
        "texts": texts,
        "summary": summary,
        "cards": cards,
        "screening": screening,
    }


def text_view(lib: Library, bibcode: str, source: str, version: str) -> dict | None:
    text = lib.text(bibcode, source, version)
    if text is None:
        return None
    return {"bibcode": bibcode, "source": source, "version": version,
            "source_name": SOURCE_NAMES.get(source, source), "truncated": text.truncated,
            "label": text_label(source, version), "origin": text_origin(lib, bibcode, source),
            "paragraphs": [{"i": i, "label": x["label"], "text": x["text"]} for i, x in enumerate(text.paragraphs)]}


# --- library --------------------------------------------------------------------------------

def _library_rows(lib: Library) -> list[dict]:
    """One row per paper, with alternate records (a preprint whose journal version is also
    stored) folded into the paper that lists them."""
    db = lib.db
    papers = [Paper.from_row(r) for r in db.execute("SELECT * FROM papers")]
    alternates = {a for p in papers for a in p.alternate_bibcodes}
    primary = {a: p.bibcode for p in papers for a in p.alternate_bibcodes}

    def owner(b: str) -> str:
        return primary.get(b, b)

    best_text: dict[str, str] = {}
    for t in db.execute("SELECT bibcode, source, version FROM texts WHERE source != 'abstract'"):
        best_text.setdefault(owner(t["bibcode"]), f"{t['source']} {t['version']}")
    cards: dict[str, int] = {}
    for c in db.execute("SELECT bibcode, COUNT(*) AS n FROM cards GROUP BY bibcode"):
        cards[owner(c["bibcode"])] = cards.get(owner(c["bibcode"]), 0) + c["n"]
    screened: dict[str, tuple[int, int]] = {}
    for s in db.execute("SELECT bibcode, COUNT(*) AS n, MAX(score) AS best FROM screening GROUP BY bibcode"):
        n, best = screened.get(owner(s["bibcode"]), (0, -1))
        screened[owner(s["bibcode"])] = (n + s["n"], max(best, s["best"]))
    cited: dict[str, int] = {}
    for r in db.execute("SELECT run_id FROM runs"):
        for b in cited_in_run(lib, r["run_id"]):
            cited[owner(b)] = cited.get(owner(b), 0) + 1
    summarised = {owner(r["bibcode"]) for r in db.execute("SELECT DISTINCT bibcode FROM summaries")}
    tasks: dict[tuple[str, str], dict] = {}
    for (b, kind), row in lib.latest_tasks().items():
        tasks[(owner(b), kind)] = task_view(row)

    rows = []
    for p in papers:
        if p.bibcode in alternates:
            continue
        n_screened, best = screened.get(p.bibcode, (0, None))
        text = best_text.get(p.bibcode)
        rows.append({**paper_brief(p), "text": text.split(" ")[0] if text else None,
                     "text_version": text.split(" ", 1)[1] if text else None,
                     "cards": cards.get(p.bibcode, 0), "screened": n_screened, "best_score": best,
                     "cited": cited.get(p.bibcode, 0), "has_summary": p.bibcode in summarised,
                     "can_fetch": can_fetch(p),
                     "tasks": {k: tasks.get((p.bibcode, k)) for k in ("fetch", "summarize")}})
    return rows


def _status_ok(row: dict, status: str) -> bool:
    return {
        "all": True,
        "found": row["screened"] == 0,
        "screened": row["screened"] > 0,
        "read": row["text"] is not None,
        "carded": row["cards"] > 0,
        "cited": row["cited"] > 0,
    }[status]


SORTS = {
    "year": lambda r: (r["year"] or 0, r["bibcode"]),
    "author": lambda r: (r["first_author"].lower(), r["year"] or 0),
    "title": lambda r: r["title"].lower(),
    "citations": lambda r: (r["citation_count"], r["bibcode"]),
    "score": lambda r: (r["best_score"] if r["best_score"] is not None else -1, r["year"] or 0),
}


def library_view(lib: Library, matching: list[str] | None, *, status: str = "read", refereed: bool = False,
                 year_from: int | None = None, year_to: int | None = None, sort: str = "year",
                 desc: bool = True, offset: int = 0, limit: int = 100) -> dict:
    """matching: bibcodes from a title/author/abstract search, best first, or None for no search."""
    rows = _library_rows(lib)
    totals = {s: sum(1 for r in rows if _status_ok(r, s)) for s in STATUSES}
    if matching is not None:
        rank = {b: i for i, b in enumerate(matching)}
        rows = [r for r in rows if r["bibcode"] in rank]
    rows = [r for r in rows if _status_ok(r, status) and (not refereed or r["refereed"])]
    years: dict[int, int] = {}
    for r in rows:
        if r["year"]:
            years[r["year"]] = years.get(r["year"], 0) + 1
    rows = [r for r in rows if (year_from is None or (r["year"] or 0) >= year_from)
            and (year_to is None or (r["year"] or 9999) <= year_to)]
    if sort == "relevance" and matching is not None:
        rows.sort(key=lambda r: rank[r["bibcode"]])
    else:
        rows.sort(key=SORTS.get(sort, SORTS["year"]), reverse=desc)
    return {"total": len(rows), "totals": totals, "years": years, "rows": rows[offset:offset + limit]}


def search_view(lib: Library, hits: list[dict]) -> list[dict]:
    out = []
    for h in hits:
        p = lib.paper(h["bibcode"])
        out.append({**h, "source_name": SOURCE_NAMES.get(h["source"], h["source"]),
                    "paper": paper_brief(p) if p else {"bibcode": h["bibcode"]}})
    return out


# --- overview -------------------------------------------------------------------------------

def overview(lib: Library, monthly_credits: float | None) -> dict:
    start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    spent = lib.db.execute("SELECT COALESCE(SUM(cost), 0) AS c FROM llm_calls WHERE at >= ?", (start,)).fetchone()["c"]
    count = lambda sql: lib.db.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "month_spend": round(float(spent), 4),
        "monthly_credits": monthly_credits,
        "papers": count("SELECT COUNT(*) FROM papers"),
        "read": count("SELECT COUNT(DISTINCT bibcode) FROM texts WHERE source != 'abstract'"),
        "cards": count("SELECT COUNT(*) FROM cards"),
        "runs": count("SELECT COUNT(*) FROM runs"),
        "now": time.time(),
    }
