"""A review as one HTML file to send to people: the review, its timeline, the citation graph of the
papers read, every paper screened or read, each paper read's evidence card with the paragraphs the
card cites, and refs.bib.

    litreview share <run>                      # writes runs/<run>/share.html
    GET /api/runs/<run>/share?download=1       # the same page, from the web UI

The page is the UI's share bundle (ui/src/share, built by `npm run build` into ui/dist/share)
with the review's data inlined, so it opens from a file or an email attachment with no server.
It leaves out what only you need: costs, the run report, and the stored texts beyond the cited
paragraphs.
"""

from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path

from ..config import VERSION
from ..library import Library
from . import views

ASSETS = Path(__file__).resolve().parent / "ui" / "dist" / "share"
ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' "
        "height='32' rx='7' fill='%232b59c3'/%3E%3Cpath d='M9 8h3v13h9v3H9z' fill='white'/%3E%3C/svg%3E")


class AssetsMissing(RuntimeError):
    pass


QUOTE_LABEL = re.compile(r"^\[[^\]]*\]\s*")
# LaTeX math as the page finds it (ui/src/lib/math.js), so a quote isn't cut inside a formula.
MATH = re.compile(r"\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|(?<![\\$])\$(?=\S)(?:\\\$|[^$])+?(?<=\S)\$", re.S)
MATH_START = re.compile(r"\$|\\[(\[]")


def shown_quote(quotes: list[str]) -> list[str]:
    """Only what CardItems.svelte shows of an item's quotes: the first, cut where its short() cuts
    it. Each quote is a whole paragraph, and the page carries those once, under `paragraphs`."""
    if not quotes:
        return []
    m = QUOTE_LABEL.match(quotes[0])
    label = m.group(0) if m else ""
    body = " ".join(quotes[0][len(label):].split())
    return [label + (cut_before_math(body, 277) + "…" if len(body) > 280 else body)]


def cut_before_math(s: str, n: int) -> str:
    """The first n characters of s, cut before any formula that would be split there, as
    cutBeforeMath in math.js does."""
    cut = s[:n]
    end = max((m.end() for m in MATH.finditer(cut)), default=0)
    m = MATH_START.search(cut, end)
    return cut if m is None else cut[:m.start()].rstrip()


def cited_paragraphs(lib: Library, card: dict) -> dict[int, dict]:
    """The stored paragraphs a card's items cite, and only those."""
    ref = card["text"]
    text = lib.text(card["bibcode"], ref["source"], ref["version"]) if ref["matches"] else None
    if text is None:
        return {}
    wanted = {p["i"] for kind in ("methods", "findings", "limitations") for it in card[kind] for p in it["passages"]}
    return {i: {"label": text.paragraphs[i]["label"], "text": text.paragraphs[i]["text"]}
            for i in sorted(wanted) if 0 <= i < len(text.paragraphs)}


def data(lib: Library, run_id: str, folder: Path) -> dict | None:
    """Everything the shared page shows, or None until the review is written."""
    row = lib.run(run_id)
    review = views.review_view(lib, run_id) if row else None
    if review is None:
        return None
    del review["notes"]  # changes made after the revision, which belong to the run report
    detail = views.run_detail(lib, run_id)
    # Every paper read, not just those cited, so each paper in the graph and the papers table opens its card.
    cards = {}
    for c in lib.cards(run_id):
        card = views.card_view(lib, run_id, c["bibcode"])
        if card:
            for kind in ("methods", "findings", "limitations"):
                for it in card[kind]:
                    it["quotes"] = shown_quote(it["quotes"])
            cards[c["bibcode"]] = {**card, "paragraphs": cited_paragraphs(lib, card)}
    # Whether your library has a newer text of a paper only matters for adding papers, which the page can't.
    papers = [{k: v for k, v in p.items() if k not in ("newer_text", "rank")} for p in views.run_papers(lib, run_id)]
    bib = folder / "refs.bib"
    return {
        "run": {"run_id": run_id, "question": row["question"], "created_at": row["created_at"],
                "interpretation": detail["interpretation"], "since": detail["since"], "until": detail["until"],
                "queries": detail["queries"], "screened": len(lib.screening(run_id))},
        "review": review,
        "cards": cards,
        "graph": views.graph_view(lib, run_id),
        "papers": papers,
        "bib": bib.read_text() if bib.exists() else "",
        "exported_at": time.time(),
        "version": VERSION,
    }


def _inline(code: str, tag: str) -> str:
    """Code to inline in a <script> or <style> element, which `</tag` or `<!--` would end early or
    confuse. A backslash before either is harmless inside JS and CSS strings."""
    return re.sub(rf"<(/{tag}|!--)", r"<\\\1", code, flags=re.IGNORECASE)


def page(lib: Library, run_id: str, folder: Path, assets: Path | str | None = None) -> str | None:
    """The shared page as one HTML document, or None until the review is written."""
    assets = Path(assets or ASSETS)
    try:
        js, css = (assets / "share.js").read_text(), (assets / "share.css").read_text()
    except FileNotFoundError:
        raise AssetsMissing("the shareable page isn't built: run `npm run build` in litreview/web/ui") from None
    content = data(lib, run_id, folder)
    if content is None:
        return None
    # Model and paper text only ever reaches the page as JSON, which the bundle renders as text.
    blob = json.dumps(content, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="litreview {VERSION}">
<title>{html.escape(content["run"]["question"])}</title>
<link rel="icon" href="{ICON}">
<style>{_inline(css, "style")}</style>
</head>
<body>
<div id="app"></div>
<script type="application/json" id="review-data">{blob}</script>
<script>{_inline(js, "script")}</script>
</body>
</html>
"""


def filename(run_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", run_id) + ".html"
