"""Draft parsing and checks: citation keys (rule 4), name-drops and quotes (rule 5), claim support
(rule 6, by Claude), and the chronology. Also the mechanical fixes applied after the one revision.

Headings are checked like sentences, because they are rendered too.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .library import Paper

# A citation group is any [...] holding an @key, so "[see @A]" and "[-@A]" count; the renderer
# turns the whole group into links.
KEY_GROUP = re.compile(r"\[([^\[\]]*?@[^\[\]]*?)\]")
KEY = re.compile(r"@([^\s;,\]]+)")
ENTRY = re.compile(r"^\*\*\[@([^\s;\]]+)\]\*\*\s*")
PERIOD = re.compile(r"^(\d{4})\s*[–—-]\s*(\d{4})\s*:?\s*(.*)$")
MASK = "\x00"
MASKED_GROUP = re.compile(MASK + r"\d+" + MASK)

ABBREVIATIONS = {
    "al", "e.g", "i.e", "fig", "figs", "eq", "eqs", "sect", "sec", "secs", "approx", "vs", "cf",
    "no", "vol", "ca", "resp", "ref", "refs", "etc", "tab", "dr", "st", "jr",
}
_BOUNDARY = re.compile(r"(?<=[.!?])[\"”’)]*\s+(?=[A-Z0-9\"“‘(\[$" + MASK + r"*_])")

UPPER = "A-ZÀ-ÖØ-ÞĀĂĄĆĈĊČĎĐĒĔĖĘĚĜĞĠĢĤĦĨĪĬĮİĲĴĶĹĻĽĿŁŃŅŇŊŌŎŐŒŔŖŘŚŜŞŠŢŤŦŨŪŬŮŰŲŴŶŸŹŻŽ"
NAME = rf"[{UPPER}][\w'’\-]+"
YEAR = r"(?:19|20)\d{2}[a-z]?"
# Capitalized words that commonly precede a year without naming an author.
NOT_NAMES = {
    "in", "by", "since", "from", "after", "before", "during", "until", "till", "through", "between",
    "around", "circa", "early", "late", "mid", "of", "and", "or", "as", "at", "on", "for", "to", "with",
    "within", "the", "a", "an", "this", "that", "these", "those", "each", "every", "up", "over", "under",
    "spring", "summer", "autumn", "fall", "winter", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december", "jan", "feb", "mar",
    "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "year", "years", "season",
    "cycle", "epoch", "sector", "quarter", "campaign", "run", "release", "version", "about", "only",
    "also", "even", "both", "all", "some", "most", "than", "then", "until", "where", "when", "while",
}
NAME_DROPS: list[tuple[re.Pattern, str, bool]] = [
    # (pattern, label, whether group 1 must not be a common word)
    (re.compile(r"\bet\s+al\b", re.I), "'et al.'", False),
    (re.compile(rf"\b{NAME}\s*(?:&|and)\s*{NAME},?\s*\(?\s*{YEAR}\b"), "author names with a year", False),
    (re.compile(rf"\b({NAME})\s+(?:and|&)\s+(?:collaborators|colleagues|co-?workers)\b"), "author with collaborators", True),
    (re.compile(rf"\b({NAME})(?:['’]s)?,?\s*\(\s*{YEAR}\s*\)"), "name followed by a year in parentheses", True),
    (re.compile(rf"\(\s*({NAME}),?\s+{YEAR}\s*[;)]"), "parenthetical name-year citation", True),
    (re.compile(rf"\b({NAME})(?:['’]s)?,?\s+{YEAR}\b"), "name followed by a year", True),
    (re.compile(rf"\b{YEAR}\s+(?:study|paper|work|analysis|survey|review|result|measurement|model)s?\s+by\s+({NAME})"),
     "a year's study by a named author", True),
    (re.compile(r"\b(?:19|20)\d{2}[a-rt-z]\b"), "year with a letter suffix", False),  # not "2010s"
    (re.compile(r"\bPapers?\s+(?:I{1,3}|IV|V|VI{1,3})\b"), "'Paper I'-style reference", False),
]
# Citations written some other way than a [@KEY] group.
STRAY_CITATIONS = [
    (re.compile(r"(?<![\w.])@[A-Za-z0-9]"), "an @key outside a [@KEY] group"),
    (re.compile(r"\b\d{4}[A-Za-z&][A-Za-z&.]{4}[\w.]{4}[\w.][\w.]{4}[A-Za-z.]"), "a bare bibcode"),
    (re.compile(r"\barXiv\s*:?\s*(?:\d{4}\.\d{4,5}|[a-z\-]+/\d{7})", re.I), "an arXiv identifier"),
    (re.compile(r"\b[a-z\-]+(?:\.[A-Z]{2})?/\d{7}\b"), "an arXiv identifier"),
    (re.compile(r"https?://|\bdoi\.org/|\b10\.\d{4,9}/\S", re.I), "a URL or DOI"),
]
QUOTES = [re.compile(r"[\"“]([^\"”]{12,}?)[\"”]"), re.compile(r"‘([^’]{12,}?)’")]


@dataclass
class Sentence:
    sid: int
    text: str
    keys: list[str]
    flags: list[str] = field(default_factory=list)


@dataclass
class Block:
    kind: str  # h2 | h3 | entry | para | bullet
    heading: str = ""
    # An entry's paper. On a bullet or paragraph below an entry, before the next heading or entry,
    # the paper whose entry it continues: drafts sometimes give each paper a key line and bullets.
    key: str | None = None
    sentences: list[Sentence] = field(default_factory=list)
    depth: int = 0  # a bullet's nesting level: 0 at the top, 1 under the bullet above, ...
    # On an entry, the blocks that continue it, as periods_of found them; they move with the entry.
    tail: list[Block] = field(default_factory=list, repr=False, compare=False)

    def to_markdown(self) -> str:
        if self.kind == "h2":
            return f"## {self.heading}"
        if self.kind == "h3":
            return f"### {self.heading}"
        body = " ".join(s.text for s in self.sentences)
        if self.kind == "entry":
            return f"**[@{self.key}]** {body}".rstrip()
        if self.kind == "bullet":
            return f"{'  ' * self.depth}- {body}"
        return body


@dataclass
class Draft:
    blocks: list[Block]

    def __post_init__(self):
        # A bullet nests at most one level below the bullet before it, and sub-items whose parent
        # was cut move up together, so no bullet is left nested under nothing or under a sibling.
        levels: list[int] = []  # the depths as given of the open levels
        for b in self.blocks:
            if b.kind != "bullet":
                levels = []
                continue
            while levels and levels[-1] >= b.depth:
                levels.pop()
            levels.append(b.depth)
            b.depth = len(levels) - 1

    def to_markdown(self) -> str:
        keep = [b for b in self.blocks if b.kind in ("h2", "h3", "entry") or b.sentences]
        return "\n\n".join(b.to_markdown() for b in keep) + "\n"

    def sentences(self):
        """(sentence, block, section heading, period heading) for every sentence."""
        section = period = ""
        for b in self.blocks:
            if b.kind == "h2":
                section, period = b.heading, ""
            elif b.kind == "h3":
                period = b.heading
            for s in b.sentences:
                yield s, b, section, period

    def headings(self) -> list[Block]:
        return [b for b in self.blocks if b.kind in ("h2", "h3")]

    def section(self, name: str) -> list[Block]:
        out, inside = [], False
        for b in self.blocks:
            if b.kind == "h2":
                inside = b.heading.strip().lower() == name.lower()
                continue
            if inside:
                out.append(b)
        return out

    def keys(self) -> set[str]:
        keys = {b.key for b in self.blocks if b.kind == "entry" and b.key}
        for s, *_ in self.sentences():
            keys.update(s.keys)
        for b in self.headings():
            keys.update(keys_in(b.heading))
        return keys


def keys_in(text: str) -> list[str]:
    out = []
    for group in KEY_GROUP.findall(text):
        out.extend(KEY.findall(group))
    return out


def mask_keys(text: str) -> tuple[str, list[str]]:
    groups: list[str] = []

    def sub(m):
        groups.append(m.group(0))
        return f"{MASK}{len(groups) - 1}{MASK}"

    return KEY_GROUP.sub(sub, text), groups


def unmask(text: str, groups: list[str]) -> str:
    return re.sub(MASK + r"(\d+)" + MASK, lambda m: groups[int(m.group(1))], text)


def split_sentences(text: str) -> list[str]:
    masked, groups = mask_keys(" ".join(text.split()))
    pieces, start = [], 0
    for m in _BOUNDARY.finditer(masked):
        before = masked[start : m.start()].rstrip("\"”’)")
        word = re.split(r"[\s(]", before)[-1].rstrip(".!?").lower() if before else ""
        if word in ABBREVIATIONS or re.fullmatch(r"[a-z]", word):
            continue
        pieces.append(masked[start : m.start()].strip())
        start = m.end()
    pieces.append(masked[start:].strip())

    # "Radii grow with flux. [@A] Later surveys..." -> the key belongs to the uncited sentence before it.
    out: list[str] = []
    for piece in pieces:
        lead = re.match(rf"^((?:{MASKED_GROUP.pattern}[\s.;,]*)+)(.*)$", piece)
        if lead and out and not MASKED_GROUP.search(out[-1]):
            out[-1] = f"{out[-1]} {lead.group(1).strip()}"
            piece = lead.group(2).strip()
        if piece:
            out.append(piece)
    return [unmask(s, groups) for s in out]


def parse_draft(md: str) -> Draft:
    blocks: list[Block] = []
    sid = 0

    def add_text(kind: str, text: str, key: str | None = None, depth: int = 0):
        nonlocal sid
        sentences = []
        for s in split_sentences(text):
            sid += 1
            sentences.append(Sentence(sid, s, keys_in(s)))
        blocks.append(Block(kind, key=key, sentences=sentences, depth=depth))

    lines: list[str] = []
    bullet = False
    entry = None  # the key of the entry that the blocks below it continue
    indents: list[int] = []  # the indent of each open level of bullets
    depth = 0

    def flush():
        nonlocal bullet, entry
        if lines:
            text = " ".join(lines)
            m = ENTRY.match(text)
            if bullet:
                add_text("bullet", text, key=entry, depth=depth)
            elif m:
                entry = m.group(1)
                add_text("entry", text[m.end() :], key=entry)
            else:
                add_text("para", text, key=entry)
            lines.clear()
        bullet = False

    for line in md.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            entry = None
            indents.clear()
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped.lstrip("#").strip()
            if level == 2:
                blocks.append(Block("h2", heading=heading))
            elif level >= 3:
                blocks.append(Block("h3", heading=heading))
            # a level-1 title is dropped; code writes the title
        elif re.match(r"^[-*•]\s+", stripped) and not stripped.startswith("**"):
            flush()
            bullet = True
            indent = len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())
            while indents and indents[-1] > indent:
                indents.pop()
            if not indents or indents[-1] < indent:
                indents.append(indent)
            depth = len(indents) - 1
            lines.append(re.sub(r"^[-*•]\s+", "", stripped))
        else:
            if not lines:
                indents.clear()  # a paragraph ends the list
            lines.append(stripped)  # continues the paragraph, or the bullet above (hard-wrapped)
    flush()
    return Draft(blocks)


# --- issues ---------------------------------------------------------------------------------


@dataclass
class Issue:
    rule: str  # rule4 | rule5 | rule6 | chronology
    detail: str
    sid: int | None = None
    key: str | None = None  # an entry heading key, for entry-level problems
    heading: str | None = None  # a section or period heading

    def describe(self, draft: Draft) -> str:
        if self.sid is not None:
            text = next((s.text for s, *_ in draft.sentences() if s.sid == self.sid), "")
            return f"[{self.rule}] S{self.sid}: {self.detail} — “{text}”"
        if self.key:
            return f"[{self.rule}] entry [@{self.key}]: {self.detail}"
        if self.heading is not None:
            return f"[{self.rule}] heading “{self.heading}”: {self.detail}"
        return f"[{self.rule}] {self.detail}"

    def to_dict(self) -> dict:
        return asdict(self)


def citation_problems(text: str) -> list[str]:
    masked, _ = mask_keys(text)
    return [f"{label}: '{m.group(0)}'" for pattern, label in STRAY_CITATIONS if (m := pattern.search(masked))]


def name_problems(text: str) -> list[str]:
    masked, _ = mask_keys(text)
    for pattern, label, needs_name in NAME_DROPS:
        for m in pattern.finditer(masked):
            if needs_name and m.group(1).lower() in NOT_NAMES:
                continue
            return [f"{label}: '{m.group(0)}'"]
    return []


def text_problems(text: str) -> list[str]:
    """Rule 4 and 5 problems in free text that has no citable keys of its own (e.g. the scope note)."""
    return citation_problems(text) + name_problems(text) + [f"citation key: {k}" for k in keys_in(text)]


def check_keys(draft: Draft, citable: set[str]) -> list[Issue]:
    issues = []
    for b in draft.blocks:
        if b.kind == "entry" and b.key not in citable:
            issues.append(Issue("rule4", f"heading key not in the citable set: {b.key}", key=b.key))
    for b in draft.headings():
        for k in keys_in(b.heading):
            issues.append(Issue("rule4", f"citation in a heading: {k}", heading=b.heading))
        for p in citation_problems(b.heading):
            issues.append(Issue("rule4", f"citation written outside a [@KEY] group, {p}", heading=b.heading))
    for s, *_ in draft.sentences():
        for k in s.keys:
            if k not in citable:
                issues.append(Issue("rule4", f"key not in the citable set: {k}", sid=s.sid))
        for p in citation_problems(s.text):
            issues.append(Issue("rule4", f"citation written outside a [@KEY] group, {p}", sid=s.sid))
    return issues


def _norm(text: str) -> str:
    text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    text = text.replace("–", "-").replace("—", "-")
    return " ".join(text.split())


def check_names_and_quotes(draft: Draft, passages_for) -> list[Issue]:
    """passages_for(keys) -> list of stored passage strings for those papers (all if keys is empty)."""
    issues = []
    for b in draft.headings():
        for p in name_problems(b.heading):
            issues.append(Issue("rule5", p, heading=b.heading))
    for s, b, *_ in draft.sentences():
        for p in name_problems(s.text):
            issues.append(Issue("rule5", p, sid=s.sid))
        masked, _ = mask_keys(s.text)
        keys = s.keys or ([b.key] if b.key else [])
        for pattern in QUOTES:
            for q in pattern.findall(masked):
                needle = _norm(q)
                if not any(needle in _norm(p) for p in passages_for(keys)):
                    issues.append(Issue("rule5", f"quoted text not found in the cited papers: “{q[:60]}”", sid=s.sid))
    return issues


def strip_heading_problems(draft: Draft, headings: set[str]) -> tuple[Draft, list[str]]:
    """Period headings lose their theme; other flagged headings are removed."""
    notes, blocks = [], []
    for b in draft.blocks:
        if b.kind in ("h2", "h3") and b.heading in headings:
            m = PERIOD.match(b.heading)
            if b.kind == "h3" and m:
                notes.append(f"removed the theme of heading “{b.heading}”")
                b = Block("h3", heading=f"{m.group(1)}–{m.group(2)}")
            else:
                notes.append(f"removed heading “{b.heading}”")
                continue
        blocks.append(b)
    return Draft(blocks), notes


@dataclass
class Period:
    start: int
    end: int
    theme: str
    intro: list[Block]
    entries: list[Block]


def periods_of(draft: Draft, strict: bool = True) -> list[Period] | None:
    """Periods in the Chronology section. With strict, None if any h3 isn't 'YYYY–YYYY: theme';
    otherwise such headings are dropped and their content joins the period above."""
    periods: list[Period] = []
    loose: list[Block] = []
    entry = None
    for b in draft.section("Chronology"):
        if b.kind == "h3":
            entry = None
            m = PERIOD.match(b.heading)
            if m:
                periods.append(Period(int(m.group(1)), int(m.group(2)), m.group(3).strip(), [], []))
                continue
            if strict:
                return None
            continue
        if b.kind == "entry":
            entry = b
            entry.tail = []
        elif entry is not None and b.key == entry.key:
            entry.tail.append(b)
            continue
        if periods:
            (periods[-1].entries if b.kind == "entry" else periods[-1].intro).append(b)
        else:
            loose.append(b)
    if periods and loose:
        periods[0].intro[:0] = [b for b in loose if b.kind != "entry"]
        periods[0].entries[:0] = [b for b in loose if b.kind == "entry"]
    return periods


def check_chronology(draft: Draft, papers: dict[str, Paper], citable: set[str]) -> list[Issue]:
    issues = []
    periods = periods_of(draft)
    if periods is None:
        return [Issue("chronology", "every period heading must read '### YYYY–YYYY: theme'")]
    if not periods:
        return [Issue("chronology", "the Chronology section has no periods")]
    n_entries = sum(len(p.entries) for p in periods)
    many = n_entries >= 6
    if many and not 2 <= len(periods) <= 6:
        issues.append(Issue("chronology", f"{len(periods)} periods; use between 2 and 6"))
    for prev, cur in zip(periods, periods[1:]):
        if cur.start != prev.end + 1:
            issues.append(Issue("chronology", f"periods {prev.start}–{prev.end} and {cur.start}–{cur.end} are not contiguous"))
    seen: dict[str, int] = {}
    for p in periods:
        if p.end < p.start:
            issues.append(Issue("chronology", f"period {p.start}–{p.end} ends before it starts"))
        if many and len(p.entries) < 3:
            issues.append(Issue("chronology", f"period {p.start}–{p.end} has {len(p.entries)} papers; each needs at least 3"))
        last = ""
        for e in p.entries:
            seen[e.key] = seen.get(e.key, 0) + 1
            paper = papers.get(e.key)
            if paper is None:
                continue
            if paper.year is not None and not p.start <= paper.year <= p.end:
                issues.append(Issue("chronology", f"dated {paper.year}, outside its period {p.start}–{p.end}", key=e.key))
            if paper.sort_date < last:
                issues.append(Issue("chronology", "out of date order within its period", key=e.key))
            last = max(last, paper.sort_date)
    for k, n in seen.items():
        if n > 1:
            issues.append(Issue("chronology", f"has {n} entries; give each paper one", key=k))
    for k in sorted(citable - set(seen)):
        issues.append(Issue("chronology", "citable paper has no entry in the Chronology", key=k))
    return issues


def fix_chronology(draft: Draft, papers: dict[str, Paper]) -> tuple[Draft, list[str]]:
    """Deterministic repair after the revision: place, order, merge and re-bound the periods.

    Afterwards every dated entry sits inside its period, periods are contiguous and in date order,
    and, with 6 or more entries, there are 2-6 periods of at least 3 entries each.
    """
    periods = periods_of(draft, strict=False)
    if not periods:
        return draft, []
    notes: list[str] = []

    def year(e: Block) -> int | None:
        p = papers.get(e.key)
        return p.year if p else None

    def sort_key(e: Block) -> str:
        p = papers.get(e.key)
        return (p.sort_date if p else "") or "9999"

    # 1. one entry per paper, each in the period that contains its year (else the nearest one)
    seen, entries = set(), []
    for p in periods:
        for e in p.entries:
            if e.key in seen:
                notes.append(f"removed a duplicate entry for [@{e.key}]")
                continue
            seen.add(e.key)
            entries.append((p, e))
        p.entries = []
    for home, e in entries:
        y, target = year(e), home
        if y is not None and not home.start <= y <= home.end:
            inside = [p for p in periods if p.start <= y <= p.end]
            target = inside[0] if inside else min(periods, key=lambda p: min(abs(y - p.start), abs(y - p.end)))
            if target is not home:
                notes.append(f"moved [@{e.key}] ({y}) from {home.start}–{home.end} to {target.start}–{target.end}")
        target.entries.append(e)

    def years(p: Period) -> list[int]:
        return [y for y in (year(e) for e in p.entries) if y is not None]

    # 2. fold periods without dated entries into a neighbour, keeping their text
    periods.sort(key=lambda p: p.start)
    i = 0
    while i < len(periods) and len(periods) > 1:
        if years(periods[i]):
            i += 1
            continue
        empty = periods.pop(i)
        if i < len(periods):
            into = periods[i]  # the next period
            into.intro = empty.intro + into.intro
        else:
            into = periods[i - 1]  # it was the last period
            into.intro = into.intro + empty.intro
        into.entries += empty.entries
        notes.append(f"folded period {empty.start}–{empty.end}, which had no dated papers, into {into.start}–{into.end}")

    def merge(i: int) -> None:
        a, b = periods[i], periods[i + 1]
        notes.append(f"merged periods {a.start}–{a.end} and {b.start}–{b.end}")
        theme = a.theme if a.theme == b.theme else "; ".join(t for t in (a.theme, b.theme) if t)
        periods[i : i + 2] = [Period(min(a.start, b.start), max(a.end, b.end), theme, a.intro + b.intro,
                                     a.entries + b.entries)]

    # 3. merge until periods don't overlap, each has enough papers, and there are at most 6
    many = len(entries) >= 6
    while len(periods) > 1:
        periods.sort(key=lambda p: min(years(p) or [p.start]))
        overlap = next((i for i in range(len(periods) - 1)
                        if years(periods[i]) and years(periods[i + 1])
                        and max(years(periods[i])) >= min(years(periods[i + 1]))), None)
        if overlap is not None:
            merge(overlap)
            continue
        small = [i for i, p in enumerate(periods) if len(p.entries) < 3] if many else []
        if small:
            i = small[0]
            if i == len(periods) - 1 or (i > 0 and len(periods[i - 1].entries) <= len(periods[i + 1].entries)):
                i -= 1
            merge(i)
            continue
        if len(periods) > 6:
            sizes = [len(a.entries) + len(b.entries) for a, b in zip(periods, periods[1:])]
            merge(sizes.index(min(sizes)))
            continue
        break

    # 4. date order within periods, then contiguous bounds that contain every entry
    for p in periods:
        order = sorted(p.entries, key=sort_key)
        if order != p.entries:
            notes.append(f"put period {p.start}–{p.end} in date order")
        p.entries = order
    for i, p in enumerate(periods):
        ys = years(p)
        start = min([p.start, *ys]) if i == 0 else periods[i - 1].end + 1
        if i + 1 < len(periods) and years(periods[i + 1]):
            end = min(years(periods[i + 1])) - 1
        else:
            end = max([p.end, *ys])
        end = max(end, start)
        if (start, end) != (p.start, p.end):
            notes.append(f"re-bounded period {p.start}–{p.end} to {start}–{end}")
        p.start, p.end = start, end

    rebuilt: list[Block] = []
    in_chron = False
    for b in draft.blocks:
        if b.kind == "h2":
            in_chron = False
            if b.heading.strip().lower() == "chronology":
                rebuilt.append(b)
                for p in periods:
                    rebuilt.append(Block("h3", heading=f"{p.start}–{p.end}: {p.theme}".rstrip(": ")))
                    rebuilt.extend(p.intro)
                    for e in p.entries:
                        rebuilt += [e, *e.tail]
                in_chron = True
                continue
        if not in_chron:
            rebuilt.append(b)
    return Draft(rebuilt), notes


def remove_sentences(draft: Draft, sids: set[int], entry_keys: set[str]) -> Draft:
    """Without the sentences `sids`, and without the entries for `entry_keys` and what continues them."""
    blocks = []
    for b in draft.blocks:
        if b.key in entry_keys:
            continue
        if b.sentences:
            b.sentences = [s for s in b.sentences if s.sid not in sids]
            if not b.sentences and b.kind != "entry":
                continue
        blocks.append(b)
    return Draft(blocks)


def mend_targets(draft: Draft, cut: set[int], cut_entries: set[str]) -> list[tuple[Sentence, str, list[str]]]:
    """Each kept sentence that comes right after removed ones in its paragraph or bullet, and so may
    point back at them ("It was ..."): (the sentence, the kept sentence before the removed ones or "",
    the removed sentences)."""
    out = []
    for b in draft.blocks:
        if b.key in cut_entries:
            continue
        before, removed = "", []
        for s in b.sentences:
            if s.sid in cut:
                removed.append(s.text)
                continue
            if removed:
                out.append((s, before, removed))
            before, removed = s.text, []
    return out


# --- claim check (rule 6) ---------------------------------------------------------------------

CHECK_TASK = """Task: check each numbered sentence of a review draft against the evidence cards above.

Label each sentence:
- supported: every claim in it is backed by the cards of the papers it cites
- partial: part of it is backed, or it overstates or generalizes beyond the cards
- unsupported: the cited cards don't back it, or it states a finding, method or limitation without citing any paper
- no_claim: a transition or framing sentence that states no finding, method or limitation

A sentence inside a paper's entry cites that paper even without a key. Judge only against the cards, not outside knowledge.

Reply with exactly one line per sentence, in this form, and nothing else:
S<number>: <label> - <short reason>

Sentences:
"""

LABEL_LINE = re.compile(  # [ \t], not \s, so one line's match can't run into the next
    r"^[ \t>*\-•]*\**[ \t]*S(\d+)\**[ \t]*(?:\[[^\]\n]*\])?[ \t]*\**[ \t]*[:.)\-–—]*[ \t]*\**[ \t]*"
    r"(supported|partial|unsupported|no[ _-]?claim)\**(?!\w)[ \t]*[-–—:]?[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
CHECK_CHUNK = 40


def claim_check_prompts(draft: Draft, only: set[int] | None = None) -> list[tuple[list[int], str]]:
    rows = []
    for s, b, section, period in draft.sentences():
        if only is not None and s.sid not in only:
            continue
        where = f"entry @{b.key}" if b.key else (period or section)
        rows.append((s.sid, f"S{s.sid} [{where}]: {s.text}"))
    out = []
    for i in range(0, len(rows), CHECK_CHUNK):
        chunk = rows[i : i + CHECK_CHUNK]
        out.append(([sid for sid, _ in chunk], CHECK_TASK + "\n".join(line for _, line in chunk)))
    return out


def parse_labels(text: str) -> dict[int, tuple[str, str]]:
    out = {}
    for m in LABEL_LINE.finditer(text):
        label = m.group(2).lower()
        label = "no_claim" if label.startswith("no") else label
        out[int(m.group(1))] = (label, m.group(3).strip().strip("*").strip())
    return out
