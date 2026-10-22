"""Rendering: keys to author–year links, the reference list, report.md and run.json."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from urllib.parse import quote

from .config import VERSION
from .library import Paper
from .verify import KEY, KEY_GROUP, Draft

ADS_ABS = "https://ui.adsabs.harvard.edu/abs/{}/abstract"
PARTIAL_MARK = "†"


def last_name(author: str) -> str:
    return author.split(",")[0].strip() or author.strip()


def short_authors(p: Paper) -> str:
    names = [last_name(a) for a in p.authors] or ["Anonymous"]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} & {names[1]}"
    return f"{names[0]} et al."


def cite_year(p: Paper) -> int | str:
    """Journal year for refereed papers, arXiv year for preprints."""
    return (p.pub_year if p.refereed else p.year) or p.year or "n.d."


def journal_abbrev(p: Paper) -> str:
    j = p.bibcode[4:9].strip(".")
    return j if j and j != "arXiv" else (p.pub or "")


class Labels:
    """Author–year labels with a/b suffixes where two papers would read the same."""

    def __init__(self, papers: dict[str, Paper]):
        groups: dict[str, list[Paper]] = defaultdict(list)
        for p in papers.values():
            groups[f"{short_authors(p)} {cite_year(p)}"].append(p)
        self.labels: dict[str, str] = {}
        for base, group in groups.items():
            if len(group) == 1:
                self.labels[group[0].bibcode] = base
            else:
                for i, p in enumerate(sorted(group, key=lambda p: (p.sort_date, p.bibcode))):
                    self.labels[p.bibcode] = f"{base}{chr(ord('a') + i)}"

    def __getitem__(self, bibcode: str) -> str:
        return self.labels[bibcode]


def link(bibcode: str, text: str) -> str:
    return f"[{text}]({ADS_ABS.format(quote(bibcode, safe=''))})"


STRAY_KEY = re.compile(r"(?<![\w.])@[^\s;,\])]+")


def render_keys(text: str, labels: Labels, citable: set[str]) -> str:
    """Key groups become author-year links; any key outside the citable set, or outside a group,
    is never rendered as a citation."""
    def group(m):
        parts = []
        for k in KEY.findall(m.group(1)):
            parts.append(link(k, labels[k]) if k in citable and k in labels.labels else "invalid citation removed")
        return f"({'; '.join(parts)})"

    return STRAY_KEY.sub("", KEY_GROUP.sub(group, text))


def entry_heading(p: Paper, labels: Labels) -> str:
    label = labels[p.bibcode]
    name = label.rsplit(" ", 1)[0]
    suffix = label[-1] if label[-1].isalpha() and label[-1].islower() else ""
    head = f"**{p.year or 'n.d.'}{suffix} — {link(p.bibcode, name)}**"
    if p.refereed:
        if p.pub_year and p.pub_year != p.year:
            head += f" ({journal_abbrev(p)} {p.pub_year})"
        else:
            head += f" ({journal_abbrev(p)})"
    else:
        head += " (preprint)"
    return head


def reference(p: Paper) -> str:
    names = p.authors[:3]
    authors = ", ".join(names) + (", et al." if len(p.authors) > 3 else "")
    out = f"{authors} {cite_year(p)}, {p.pub or journal_abbrev(p)}. *{p.title}*. {link(p.bibcode, 'ADS')}"
    if p.arxiv_id:
        out += f" · [arXiv:{p.arxiv_id}](https://arxiv.org/abs/{p.arxiv_id})"
    return out


def scope_section(question: str, interpretation: str, since, until, stats: dict) -> str:
    rng = f"{since or 'earliest'}–{until or 'present'}"
    names = {"arxiv_src": "LaTeX source", "arxiv_html": "arXiv HTML", "arxiv_pdf": "arXiv PDF", "ads_scan": "ADS scans",
             "unpaywall": "open-access copies", "upload": "uploaded PDFs", "abstract": "abstracts"}
    sources = ", ".join(f"{n} from {names.get(s, s)}" for s, n in sorted(stats["sources"].items()))
    return "\n\n".join([
        "## Question and scope",
        f"**Question:** {question}",
        *([f"**As interpreted:** {interpretation}"] if interpretation else []),
        f"**Date range searched:** {rng}",
        (f"**Papers:** {stats['cited']} cited, from {stats['read']} read "
         f"({stats['full_text']} in full text, {stats['abstract_only']} from the abstract only); of those cited, {sources}. "
         "Every citation is to a paper retrieved from NASA ADS and read for this review. "
         "Papers are placed by the earlier of their first arXiv posting and journal date; "
         "preprints are marked."),
    ])


def render_review(draft: Draft, papers: dict[str, Paper], citable: set[str], *, question: str,
                  interpretation: str, since, until, stats: dict, title: str) -> str:
    cited = [k for k in sorted(draft.keys()) if k in citable and k in papers]
    labels = Labels({k: papers[k] for k in cited})
    out = [f"# {title}", scope_section(question, interpretation, since, until, stats)]
    any_partial = False
    for b in draft.blocks:
        if b.kind == "h2":
            out.append(f"## {render_keys(b.heading, labels, citable)}")
        elif b.kind == "h3":
            out.append(f"### {render_keys(b.heading, labels, citable)}")
        elif b.key and (b.key not in citable or b.key not in papers):
            continue  # an entry for a paper that can't be cited, and what continues it
        else:
            sentences = []
            for s in b.sentences:
                text = render_keys(s.text, labels, citable)
                if "partial" in s.flags:
                    text += PARTIAL_MARK
                    any_partial = True
                sentences.append(text)
            body = " ".join(sentences)
            if b.kind == "entry":
                body = f"{entry_heading(papers[b.key], labels)}  \n{body}".rstrip()
            elif b.kind == "bullet":
                body = f"{'  ' * b.depth}- {body}"
            if body.strip():
                out.append(body)
    if any_partial:
        out.append(f"{PARTIAL_MARK} The claim check rated this sentence only partly supported by the cited papers' evidence cards.")
    out.append("## References")
    out.extend(f"- {reference(papers[k])}" for k in sorted(cited, key=lambda k: labels[k]))
    return "\n\n".join(out) + "\n"


def slug(text: str, n: int = 6) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"the", "a", "an", "of", "in", "on", "and", "or", "what", "how", "why", "does", "do", "is", "are", "to", "for"}
    return "-".join([w for w in words if w not in stop][:n]) or "review"


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |")
    return "\n".join(out)


def render_report(ctx: dict) -> str:
    """report.md: what was searched, screened, read and cited, and every check failure."""
    out = [f"# Run report: {ctx['run_id']}", f"litreview {VERSION} · question: {ctx['question']}"]

    plan = ctx["plan"]
    out.append("## Queries")
    out.append(f"Interpretation: {plan['interpretation']}")
    rows = [[f"`{q}`", plan["counts"].get(q, "?")] for q in plan["queries"]]
    out.append(_table(["Query", "ADS hits"], rows))
    dropped = [q for q in plan["counts"] if q not in plan["queries"]]
    if dropped:
        out.append("Dropped (no hits): " + "; ".join(f"`{q}`" for q in dropped))
    if ctx.get("expansion"):
        out.append("Citation-graph expansion from seeds: " + ", ".join(ctx["expansion"]["seeds"]))
        out.append(_table(["Operator query", "Hits kept"], [[f"`{q}`", n] for q, n in ctx["expansion"]["queries"].items()]))

    c = ctx["counts"]
    out.append("## Papers")
    out.append(_table(
        ["Found (unique)", "Screened", "Included", "Read", "Full text", "Abstract only", "Citable", "Cited"],
        [[c["found"], c["screened"], c["included"], c["read"], c["full_text"], c["abstract_only"], c["citable"], c["cited"]]],
    ))
    if c.get("screened_on_ads_abstract"):
        out.append(f"Screened on their ADS abstract (no arXiv abstract available): {c['screened_on_ads_abstract']}")

    out.append("### Read")
    out.append(_table(["Paper", "Chosen by", "Text", "Card", "Notes"], ctx["read_rows"]))
    done = [a for a in ctx.get("additions", []) if a.get("added") or a.get("not_found")]
    if done:
        out.append("### Added after the review was first written")
        lines = []
        for a in done:
            when = time.strftime("%Y-%m-%d", time.localtime(a["requested_at"]))
            line = f"- {when}: read {', '.join(a['added']) or 'nothing'}"
            if a.get("reread"):
                line += f" ({', '.join(a['reread'])} again, from a newer text)"
            if a.get("previous"):
                line += f"; the review before this is kept in {a['previous']}/"
            if a.get("not_found"):
                line += f". Not in ADS: {', '.join(a['not_found'])}"
            lines.append(line)
        out.append("\n".join(lines))
    if ctx["not_read"]:
        out.append("### Included but not read (beyond --max-papers)")
        out.append(_table(["Paper", "Score", "Reason"], ctx["not_read"]))
    if ctx["fetch_failures"]:
        out.append("### Full-text fetch failures")
        out.append(_table(["Paper", "Source", "Reason"], ctx["fetch_failures"]))
    out.append("### Excluded at screening")
    out.append(_table(["Paper", "Score", "Reason"], ctx["excluded"]) if ctx["excluded"] else "None.")

    out.append("## Checks")
    for label, issues in ctx["checks"]:
        out.append(f"### {label}")
        out.append("\n".join(f"- {i}" for i in issues) if issues else "No problems found.")
    if ctx["final_notes"]:
        out.append("### Changes made after the revision")
        out.append("\n".join(f"- {n}" for n in ctx["final_notes"]))
    out.append(f"Sentences flagged as partly supported (marked {PARTIAL_MARK} in review.md): {ctx['n_partial']}")
    if ctx["unrated"]:
        out.append(f"Sentences the claim check did not rate: {ctx['unrated']}")

    out.append("## Refusals and fallbacks")
    out.append(_table(["Stage", "Model", "Stop reason", "Fallback", "Request"], ctx["fallbacks"]) if ctx["fallbacks"] else "None.")

    out.append("## Cost")
    out.append(_table(["Stage", "Model", "Effort", "Calls", "Input", "Output", "Cache write", "Cache read", "USD"], ctx["usage"]))
    out.append(f"Total: ${ctx['total_cost']:.2f}")
    return "\n\n".join(out) + "\n"
