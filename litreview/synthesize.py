"""Drafting, the one revision, and rewording what the final cuts leave dangling, from the citable cards only.

Drafting, revision and the claim checks share one prompt prefix (system prompt + the cards block,
cached for an hour), so they must also share the model, effort and thinking settings; anything
that differs between them goes after the cards in the user turn.
"""

from __future__ import annotations

import re

from .library import Paper, normalize_date
from .llm import LLM, Reply
from .plan import DATA_NOTE

CACHE_1H = {"type": "ephemeral", "ttl": "1h"}

BASE_SYSTEM = f"""You help write and check chronological literature reviews in astronomy.

The first block of the conversation holds evidence cards for the only papers that may be cited. Each card is keyed by its ADS bibcode as [@KEY] and lists what the paper did and found that bears on the research question, with its limitations: "stated" ones come from the authors, "inferred" ones from a reader of the paper. Items carry passages quoted from the paper.

{DATA_NOTE}"""

DRAFT_TASK = """Task: write the review draft.

Research question: {question}
Scope: {interpretation}
There are {n} papers, dated {first} to {last} (a card's date is the earlier of its first arXiv posting and its journal date).

Write Markdown with exactly these three sections and headings:

## Overview
One or two paragraphs on where the field stands, citing papers.

## Chronology
{period_rule} Each period has a heading `### YYYY–YYYY: <theme>`. Inside a period, give every paper one entry, in date order. An entry is one paragraph that starts with the paper's key in bold, like `**[@KEY]**`, then says what the paper found on the question and its limitations, then links it to the work around it in a sentence or two. Sentences in an entry are about that paper unless they cite another key.

## Open problems
Limitations that recur across papers, grouped into themes, one paragraph or bullet per theme, citing every paper that raises it.

Rules:
- Cite only with keys from the cards: [@KEY], or [@KEY1; @KEY2] for several. Outside a paper's own entry, every sentence that states a finding, method or limitation cites the papers it comes from.
- Never write author names, "et al.", citation years such as (2015) or 2015a, or "Paper I". Code turns the keys into names and years.
- State only what the cards say. Write stated limitations as "the authors note ..." and inferred ones as "a limitation is ...". For a paper read from its abstract only, say that its limitations were not assessed.
- Paraphrase. Use quotation marks only around text copied exactly from a card's passage.
- Give every card an entry."""

REVISE_TASK = """Task: revise the review draft below. Checks found these problems:

{problems}

Rewrite or remove each flagged sentence so it passes, fix any chronology problems, and leave the rest unchanged. All the rules from drafting still apply: cite only with [@KEY] keys from the cards, write no author names or citation years, and state only what the cards say. Return the complete revised draft with the same three sections, and nothing else.

Draft:

{draft}"""

MEND_TASK = """Task: some sentences of the review draft failed its checks and have been removed. Each sentence below came right after removed text, so it may now point back at something that is gone, as in "It was ...", "These models ..." or "Since then ...".

For each one, if it reads correctly without the removed text, return it unchanged. Otherwise change as few words as possible so that it stands on its own, for example by naming what "it" referred to. Add no finding, method or limitation, keep every [@KEY] citation exactly as written, write no author names or citation years, and keep it one sentence.

Reply with exactly one line per sentence, in this form, and nothing else:
M<number>: <the sentence>

{items}"""


def _fmt_items(label: str, items: list[dict], prefix: str) -> list[str]:
    lines = [f"{label}:"]
    for i, it in enumerate(items, 1):
        tag = f" ({it['source']})" if "source" in it else ""
        lines.append(f"  {prefix}{i}{tag}. {it['text']}")
        for q in it.get("passage_text", [])[:2]:
            q = " ".join(q.split())
            lines.append(f'      passage: "{q[:500]}{"..." if len(q) > 500 else ""}"')
    return lines


def cards_block(cards: list[dict], papers: dict[str, Paper]) -> str:
    """The cached evidence block, oldest paper first. Author names are left out on purpose."""
    parts = []
    for card in sorted(cards, key=lambda c: (papers[c["bibcode"]].sort_date, c["bibcode"])):
        p = papers[card["bibcode"]]
        dates = []
        if p.first_arxiv_date:
            dates.append(f"first arXiv posting {normalize_date(p.first_arxiv_date)[:7]}")
        if p.pubdate:
            dates.append(f"journal {normalize_date(p.pubdate)[:7]}")
        status = "refereed" if p.refereed else "preprint"
        depth = "abstract only" if card["read_depth"] == "abstract_only" else "full text"
        lines = [
            f"=== [@{p.bibcode}] ===",
            f"date: {p.sort_date[:7]} ({'; '.join(dates)}) · {status} · read: {depth}",
            f"title: {p.title}",
        ]
        lines += _fmt_items("methods", card["methods"], "M")
        lines += _fmt_items("findings", card["findings"], "F")
        if card["limitations"]:
            lines += _fmt_items("limitations", card["limitations"], "L")
        else:
            lines.append(f"limitations: {card.get('limitations_note', 'none recorded')}")
        parts.append("\n".join(lines))
    return "EVIDENCE CARDS\n\n" + "\n\n".join(parts)


def shared_prefix(cards_text: str) -> dict:
    return {"type": "text", "text": cards_text, "cache_control": CACHE_1H}


def ask(llm: LLM, stage: str, model: str, effort: str, cards_text: str, task: str,
        max_tokens: int = 64000) -> Reply:
    messages = [{"role": "user", "content": [shared_prefix(cards_text), {"type": "text", "text": task}]}]
    return llm.call(stage, model, effort, messages, system=BASE_SYSTEM, max_tokens=max_tokens)


def period_rule(n: int) -> str:
    if n < 6:
        return "Use one or two periods covering all papers."
    return (
        "Use between 2 and 6 periods that are contiguous (each starts the year after the previous "
        "one ends), cover every paper by the year of its card's date, and hold at least 3 papers each."
    )


def draft(llm: LLM, model: str, effort: str, cards_text: str, question: str, interpretation: str,
          papers: list[Paper]) -> str:
    dates = sorted(p.sort_date for p in papers if p.sort_date)
    task = DRAFT_TASK.format(
        question=question, interpretation=interpretation, n=len(papers),
        first=dates[0][:4] if dates else "?", last=dates[-1][:4] if dates else "?",
        period_rule=period_rule(len(papers)),
    )
    return ask(llm, "synthesize", model, effort, cards_text, task).text.strip()


def revise(llm: LLM, model: str, effort: str, cards_text: str, draft_md: str, problems: list[str]) -> str:
    task = REVISE_TASK.format(problems="\n".join(f"- {p}" for p in problems), draft=draft_md)
    return ask(llm, "revise", model, effort, cards_text, task).text.strip()


def mend(llm: LLM, model: str, effort: str, cards_text: str, targets: list[tuple[str, str, list[str]]]) -> str:
    """targets: (sentence, the kept sentence before the removed text or "", the removed sentences)."""
    items = []
    for i, (text, before, removed) in enumerate(targets, 1):
        items.append(f"M{i}\n  before: {before or '(start of the paragraph)'}\n  removed: {' '.join(removed)}\n"
                     f"  sentence: {text}")
    task = MEND_TASK.format(items="\n\n".join(items))
    return ask(llm, "mend", model, effort, cards_text, task, max_tokens=8000).text


MEND_LINE = re.compile(r"^[ \t>*\-•]*\**M(\d+)\**[ \t]*[:.)\-–—][ \t]*(.+?)[ \t]*$", re.MULTILINE)


def parse_mends(text: str) -> dict[int, str]:
    """M<number> -> sentence, numbered from 1 as in the task."""
    return {int(m.group(1)): m.group(2).strip() for m in MEND_LINE.finditer(text)}
