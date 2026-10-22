"""Evidence cards: one Citations-enabled request per paper, parsed from tagged lines, then verified.

The same request can also ask for a summary of the whole paper that doesn't depend on the question
(SUMMARY lines). It is written once per stored full text and kept apart from the run's card.

Citations can't be combined with structured outputs, so the model writes tagged lines and code
joins the returned text blocks, splits them at each tag, and gives each line the citations
whose text falls inside it. Citation block indices are positions in the paper's stored paragraphs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .fulltext import UPLOAD, Paragraph, block_text, is_ocr, upload_pages
from .library import Paper, StoredText
from .plan import DATA_NOTE

EXTRACT_SYSTEM = f"""You read one astronomy paper and record what it contributes to a research question, as tagged lines for a literature review.

Write only lines in this format, one item per line, and nothing else:
RELEVANCE: <high|medium|low|none> - <one sentence on why>
METHOD: <a method, dataset or sample the paper uses, as it bears on the question>
FINDING: <a result of this paper that bears on the question, with numbers where the paper gives them>
LIMITATION (stated): <a limitation, caveat or uncertainty the authors themselves state>
LIMITATION (inferred): <a limitation you identify from this paper's own data, methods or assumptions>
SUMMARY (aim): <what the whole paper set out to do>
SUMMARY (method): <a main method, dataset or sample of the paper>
SUMMARY (result): <a main result of the paper, with numbers where the paper gives them>
SUMMARY (limitation): <a main limitation or caveat the authors state>

Rules:
- Start with the RELEVANCE line. If relevance is none, write no METHOD, FINDING or LIMITATION lines.
- Otherwise write 1-4 METHOD lines, 1-6 FINDING lines, 0-4 LIMITATION (stated) lines and 0-3 LIMITATION (inferred) lines.
- The request says whether to write SUMMARY lines. When it asks, write them last, even if relevance is none: 1 aim, 1-3 method, 1-5 result and 0-3 limitation lines describing the whole paper, not only what bears on the question.
- Cite the passages that support every METHOD, FINDING, LIMITATION and SUMMARY line.
- Record this paper's own results. Results the paper attributes to earlier work are not its findings.
- Base inferred limitations on this paper alone, never on later work or on what you know happened later.
- Write each item in your own words, in one sentence. Don't name authors of other papers.

{DATA_NOTE}"""

ABSTRACT_ONLY_NOTE = (
    "Only the abstract is available. Don't write LIMITATION (inferred) lines, and write "
    "LIMITATION (stated) only for a limitation the abstract itself states."
)
BOOK_NOTE = (
    "This is a {kind}. Its findings and results are what it derives, shows or concludes in its own "
    "presentation; results it attributes to specific earlier work are still not its own."
)

TAG = re.compile(
    r"^[ \t>*\-•]*(?:\d+[.)]\s*)?\**\s*"
    r"(RELEVANCE|METHODS?|FINDINGS?|LIMITATIONS?\s*\((?:stated|inferred)\)"
    r"|SUMMARY\s*\((?:aims?|methods?|results?|limitations?)\))\s*\**\s*:\s*\**",
    re.MULTILINE | re.IGNORECASE,
)
UNRECOGNIZED = "UNRECOGNIZED"
RELEVANCE = re.compile(r"^(high|medium|low|none)\b\s*[-–—:]?\s*(.*)$", re.IGNORECASE | re.DOTALL)
NOT_ASSESSED = "not assessed (abstract only)"
SUMMARY_KINDS = {"AIM": "aim", "METHOD": "methods", "RESULT": "results", "LIMITATION": "limitations"}


def paragraphs_of(text: StoredText) -> list[Paragraph]:
    return [Paragraph(p["label"], p["text"]) for p in text.paragraphs]


def read_depth(text: StoredText) -> str:
    if text.source == "abstract":
        return "abstract only"
    pages = upload_pages(text.version) if text.source == UPLOAD else None
    depth = f"pages {pages[0]}-{pages[1]} of an uploaded PDF" if pages else "full text"
    return depth + (", read by OCR from a scan, so expect misread characters" if is_ocr(text.version) else "")


def document_block(paper: Paper, text: StoredText) -> dict:
    context = f"Published {paper.pub_year or 'n.d.'} in {paper.pub or 'unknown venue'}. Read depth: {read_depth(text)}."
    if paper.book_kind:
        context = f"A {paper.book_kind}. {context}"
    return {
        "type": "document",
        "source": {
            "type": "content",
            "content": [{"type": "text", "text": block_text(p)} for p in paragraphs_of(text)],
        },
        "title": paper.title[:250] or paper.bibcode,
        "context": context,
        "citations": {"enabled": True},
    }


SUMMARY_ON = "Also write the SUMMARY lines."
SUMMARY_OFF = "Don't write SUMMARY lines."


def request_params(question: str, paper: Paper, text: StoredText, summary: bool = False) -> dict:
    """system + messages for one paper; the same params go to Batches or an interactive call.
    summary asks for the question-independent SUMMARY lines too (full texts only)."""
    instructions = f"Research question: {question}\n\n"
    if text.source == "abstract":
        instructions += ABSTRACT_ONLY_NOTE + "\n\n"
    if paper.book_kind:
        instructions += BOOK_NOTE.format(kind=paper.book_kind) + "\n\n"
    instructions += (SUMMARY_ON if summary and text.source != "abstract" else SUMMARY_OFF) + "\n\n"
    instructions += "Write the tagged lines now."
    return {
        "system": EXTRACT_SYSTEM,
        "messages": [
            {"role": "user", "content": [document_block(paper, text), {"type": "text", "text": instructions}]}
        ],
    }


@dataclass
class Tagged:
    tag: str
    text: str
    passages: list[int]
    passage_text: list[str]


def canonical_tag(raw: str) -> str:
    raw = raw.upper()
    if raw.startswith("SUMMARY"):
        kind = next(k for k in SUMMARY_KINDS if k in raw)
        return f"SUMMARY ({kind.lower()})"
    if raw.startswith("LIMITATION"):
        return "LIMITATION (stated)" if "STATED" in raw else "LIMITATION (inferred)"
    return raw.rstrip("S") if raw in ("METHODS", "FINDINGS") else raw


def parse_tagged(blocks: list[dict]) -> list[Tagged]:
    """Join text blocks, split at tags, attach each cited span to the line containing its midpoint.

    An item is the rest of its tag's line. Text that isn't on a tagged line comes back as an
    UNRECOGNIZED item, so it is reported and dropped instead of being merged into a neighbour.
    """
    joined, spans = "", []
    for b in blocks:
        start = len(joined)
        joined += b["text"]
        if b.get("citations"):
            spans.append(((start + len(joined)) / 2, b["citations"]))

    def collect(lo: float, hi: float) -> tuple[list[int], list[str]]:
        passages: set[int] = set()
        quoted: list[str] = []
        for mid, cites in spans:
            if not lo <= mid < hi:
                continue
            for c in cites:
                if c.get("document_index", 0) not in (0, None):
                    continue
                s, e = c.get("start_block_index"), c.get("end_block_index")
                if s is None or e is None:
                    continue
                passages.update(range(s, max(e, s + 1)))
                if c.get("cited_text"):
                    quoted.append(c["cited_text"].strip())
        return sorted(passages), quoted

    items: list[Tagged] = []
    matches = list(TAG.finditer(joined))
    lead = joined[: matches[0].start()] if matches else joined
    if lead.strip():
        items.append(Tagged(UNRECOGNIZED, " ".join(lead.split()), [], []))
    for i, m in enumerate(matches):
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(joined)
        newline = joined.find("\n", m.end(), seg_end)
        line_end = seg_end if newline == -1 else newline
        passages, quoted = collect(m.start(), line_end)
        body = " ".join(joined[m.end() : line_end].split()).strip("* ")
        items.append(Tagged(canonical_tag(m.group(1)), body, passages, quoted))
        rest = joined[line_end:seg_end]
        if rest.strip():
            items.append(Tagged(UNRECOGNIZED, " ".join(rest.split()), [], []))
    return items


def build_card(paper: Paper, text: StoredText, items: list[Tagged]) -> dict:
    card = {
        "bibcode": paper.bibcode,
        "pubdate": paper.pubdate,
        "sort_date": paper.sort_date,
        "read_depth": "abstract_only" if text.source == "abstract" else "full_text",
        "text_source": text.source,
        "text_version": text.version,
        "relevance": None,
        "relevance_reason": "",
        "methods": [],
        "findings": [],
        "limitations": [],
        "summary": {kind: [] for kind in SUMMARY_KINDS.values()},
        "unparsed": [],
    }
    for it in items:
        if it.tag == UNRECOGNIZED:
            card["unparsed"].append(it.text)
            continue
        if it.tag.startswith("SUMMARY"):
            kind = SUMMARY_KINDS[it.tag[len("SUMMARY ("):-1].upper()]
            card["summary"][kind].append({"text": it.text, "passages": it.passages, "passage_text": it.passage_text})
            continue
        if it.tag == "RELEVANCE":
            m = RELEVANCE.match(it.text)
            if m and card["relevance"] is None:
                card["relevance"] = m.group(1).lower()
                card["relevance_reason"] = m.group(2).strip()
            continue
        entry = {"text": it.text, "passages": it.passages, "passage_text": it.passage_text}
        if it.tag == "METHOD":
            card["methods"].append(entry)
        elif it.tag == "FINDING":
            card["findings"].append(entry)
        else:
            entry["source"] = "stated" if "stated" in it.tag else "inferred"
            card["limitations"].append(entry)
    return card


def verify_card(card: dict, n_paragraphs: int) -> tuple[dict, str, list[str]]:
    """Drop items without valid passages; returns (card, status, notes on what was dropped).

    This proves each item points at stored text, not that the text supports it (rule 2).
    """
    notes = [f"ignored a line in no recognised format: {t[:80]}" for t in card.get("unparsed", [])]
    for kind, items in card.get("summary", {}).items():
        kept = []
        for item in items:
            if not item["passages"] or any(not 0 <= i < n_paragraphs for i in item["passages"]):
                notes.append(f"dropped summary {kind} without a valid cited passage: {item['text'][:80]}")
                continue
            kept.append(item)
        card["summary"][kind] = kept
    for kind in ("methods", "findings", "limitations"):
        kept = []
        for item in card[kind]:
            if card["read_depth"] == "abstract_only" and item.get("source") == "inferred":
                notes.append(f"dropped inferred limitation on an abstract-only read: {item['text'][:80]}")
                continue
            if not item["passages"]:
                notes.append(f"dropped {kind[:-1]} with no cited passage: {item['text'][:80]}")
                continue
            if any(not 0 <= i < n_paragraphs for i in item["passages"]):
                notes.append(f"dropped {kind[:-1]} citing a passage outside the text: {item['text'][:80]}")
                continue
            kept.append(item)
        card[kind] = kept
    if card["read_depth"] == "abstract_only" and not card["limitations"]:
        card["limitations_note"] = NOT_ASSESSED
    if card["relevance"] is None:
        return card, "no_findings", notes + ["no RELEVANCE line"]
    if card["relevance"] == "none":
        return card, "excluded", notes
    if not card["findings"]:
        return card, "no_findings", notes + ["no finding with a cited passage"]
    return card, "verified", notes


SUMMARY_SYSTEM = f"""You read one astronomy paper and summarise the whole paper, as tagged lines.

Write only lines in this format, one item per line, and nothing else:
SUMMARY (aim): <what the paper set out to do>
SUMMARY (method): <a main method, dataset or sample of the paper>
SUMMARY (result): <a main result of the paper, with numbers where the paper gives them>
SUMMARY (limitation): <a main limitation or caveat the authors state>

Rules:
- Write 1 aim, 1-3 method, 1-5 result and 0-3 limitation lines.
- Cite the passages that support every line.
- Record this paper's own results. Results the paper attributes to earlier work are not its results.
- Write each item in your own words, in one sentence. Don't name authors of other papers.

{DATA_NOTE}"""


def summary_params(paper: Paper, text: StoredText) -> dict:
    """A request for the question-independent summary alone, for a paper no review has summarised."""
    instructions = "Write the SUMMARY lines now."
    if paper.book_kind:
        instructions = BOOK_NOTE.format(kind=paper.book_kind) + "\n\n" + instructions
    return {
        "system": SUMMARY_SYSTEM,
        "messages": [{"role": "user", "content": [document_block(paper, text),
                                                  {"type": "text", "text": instructions}]}],
    }


def summary_of(card: dict) -> dict | None:
    """The verified question-independent summary in a card, or None unless it has a cited result."""
    summary = card.get("summary") or {}
    return summary if summary.get("results") else None
