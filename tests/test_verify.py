from litreview.library import Paper
from litreview.verify import (check_chronology, check_keys, check_names_and_quotes, claim_check_prompts,
                              fix_chronology, keys_in, mend_targets, parse_draft, parse_labels, periods_of,
                              remove_sentences, split_sentences)

A, B = "2012ApJ...750....1A", "2016A&A...595A..12B"


def paper(bibcode, date, refereed=True):
    return Paper(bibcode=bibcode, pubdate=date, authors=["Doe, J."], refereed=refereed)


def test_keys_with_dots_and_ampersands_stay_in_one_sentence():
    text = f"Radius rises with flux [@{A}]. It also falls with mass [@{A}; @{B}]. Done."
    assert split_sentences(text) == [
        f"Radius rises with flux [@{A}].",
        f"It also falls with mass [@{A}; @{B}].",
        "Done.",
    ]
    assert keys_in(text) == [A, A, B]


def test_abbreviations_do_not_end_sentences():
    text = "See Fig. 3 for details, e.g. the fit. Values near approx. 5 percent hold. Next one."
    assert split_sentences(text) == [
        "See Fig. 3 for details, e.g. the fit.",
        "Values near approx. 5 percent hold.",
        "Next one.",
    ]


DRAFT = f"""# Title

## Overview

The field grew quickly [@{A}]. Two camps formed [@{A}; @{B}].

## Chronology

### 2010–2014: First fits

**[@{A}]** Fit 40 light curves. The authors note that ages are uncertain.

### 2015–2020: Populations

**[@{B}]** Extended the trend [@{B}].

## Open problems

- Ages remain uncertain [@{A}; @{B}].
"""


def test_parse_draft_structure_and_roundtrip():
    d = parse_draft(DRAFT)
    kinds = [b.kind for b in d.blocks]
    assert kinds == ["h2", "para", "h2", "h3", "entry", "h3", "entry", "h2", "bullet"]
    assert d.blocks[4].key == A
    assert [s.text for s in d.blocks[4].sentences] == ["Fit 40 light curves.", "The authors note that ages are uncertain."]
    assert d.keys() == {A, B}
    again = parse_draft(d.to_markdown())
    assert [s.text for s, *_ in again.sentences()] == [s.text for s, *_ in d.sentences()]


def test_check_keys_flags_unknown_inline_and_heading_keys():
    d = parse_draft(DRAFT + "\n**[@2099Fake....1....1Z]** Made up [@2098Fake....1....1Y].\n")
    issues = check_keys(d, {A, B})
    details = sorted(i.detail for i in issues)
    assert details == ["heading key not in the citable set: 2099Fake....1....1Z",
                       "key not in the citable set: 2098Fake....1....1Y"]


def test_name_drops_flagged_and_plain_years_allowed():
    md = (
        "## Overview\n\n"
        f"Smith et al. found it [@{A}]. Smith & Jones 2015 agreed [@{A}]. Earlier work (Lee 2009) did too [@{A}]. "
        f"Doe (2011) disagreed [@{A}]. A 2015a result [@{A}]. Paper II extends it [@{A}]. "
        f"In 2015, surveys in the 2010s grew [@{A}]. Between 2009 and 2016 the sample tripled [@{B}]."
    )
    d = parse_draft(md)
    issues = check_names_and_quotes(d, lambda keys: [])
    flagged = {i.sid for i in issues}
    sentences = {s.sid: s.text for s, *_ in d.sentences()}
    assert [sentences[s][:12] for s in sorted(flagged)] == [
        "Smith et al.", "Smith & Jone", "Earlier work", "Doe (2011) d", "A 2015a resu", "Paper II ext",
    ]


def test_quotes_must_match_a_stored_passage():
    md = f'## Overview\n\nThey call it "a robust correlation with flux" [@{A}]. They say "nothing like this at all" [@{A}].'
    d = parse_draft(md)
    passages = {A: ["We find a robust   correlation with flux above the threshold."]}
    issues = check_names_and_quotes(d, lambda keys: [p for k in keys for p in passages.get(k, [])])
    assert len(issues) == 1 and "nothing like this" in issues[0].detail


def make_papers(dates):
    return {f"P{i}": paper(f"P{i}", d) for i, d in enumerate(dates)}


def chron(periods):
    lines = ["## Chronology", ""]
    for head, keys in periods:
        lines += [f"### {head}", ""]
        lines += [f"**[@{k}]** Text about {k}." for k in keys]
        lines.append("")
    return parse_draft("\n\n".join(lines))


def test_chronology_check_finds_each_kind_of_problem():
    papers = make_papers(["2001-01", "2002-01", "2003-01", "2009-01", "2010-01", "2011-01", "2004-06"])
    d = chron([("2000–2004: A", ["P0", "P2", "P1"]), ("2006–2012: B", ["P3", "P4", "P5", "P6"])])
    issues = [i.detail for i in check_chronology(d, papers, set(papers))]
    assert "periods 2000–2004 and 2006–2012 are not contiguous" in issues
    assert "out of date order within its period" in issues
    assert "dated 2004, outside its period 2006–2012" in issues


def test_fix_chronology_moves_orders_and_rebounds():
    papers = make_papers(["2001-01", "2002-01", "2003-01", "2009-01", "2010-01", "2011-01", "2004-06"])
    d = chron([("2000–2004: A", ["P0", "P2", "P1"]), ("2006–2012: B", ["P3", "P4", "P5", "P6"])])
    fixed, notes = fix_chronology(d, papers)
    periods = periods_of(fixed)
    assert [(p.start, p.end) for p in periods] == [(2000, 2008), (2009, 2012)]
    assert [e.key for e in periods[0].entries] == ["P0", "P1", "P2", "P6"]
    assert check_chronology(fixed, papers, set(papers)) == []
    assert any("moved [@P6]" in n for n in notes)


def test_fix_chronology_merges_small_periods():
    papers = make_papers(["2001-01", "2002-01", "2003-01", "2005-01", "2010-01", "2011-01", "2012-01"])
    d = chron([("2000–2003: A", ["P0", "P1", "P2"]), ("2004–2006: B", ["P3"]), ("2007–2012: C", ["P4", "P5", "P6"])])
    fixed, _ = fix_chronology(d, papers)
    sizes = [len(p.entries) for p in periods_of(fixed)]
    assert sizes in ([4, 3], [3, 4])
    assert check_chronology(fixed, papers, set(papers)) == []


def test_bullets_under_an_entry_key_stay_with_that_paper():
    # A draft that gives each paper a key line and bullets, with P2 in the wrong period and P1 out of order.
    papers = make_papers(["2001-01", "2002-01", "2009-01", "2010-01", "2011-01", "2003-06"])
    md = "## Chronology\n\n"
    for head, keys in [("2000–2004: A", ["P1", "P0", "P2"]), ("2005–2012: B", ["P3", "P4", "P5"])]:
        md += f"### {head}\n\nFraming for {head[-1]}.\n\n"
        md += "".join(f"**[@{k}]**\n- **Method.** {k} method.\n- **Findings.** {k} result.\n\n" for k in keys)
    md += "Closing remark.\n\n## Open problems\n\nSome.\n"
    d = parse_draft(md)
    assert [(b.kind, b.key) for b in d.blocks[3:6]] == [("entry", "P1"), ("bullet", "P1"), ("bullet", "P1")]
    assert parse_draft(d.to_markdown()).to_markdown() == d.to_markdown()

    fixed, _ = fix_chronology(d, papers)
    assert check_chronology(fixed, papers, set(papers)) == []
    lines = [ln for ln in fixed.to_markdown().splitlines() if ln and not ln.startswith("#")]
    owner = None
    for ln in lines:
        if ln.startswith("**[@"):
            owner = ln[4:6]
        elif ln.startswith("- "):
            assert ln.split()[2] == owner, (ln, owner)
    assert [ln[4:6] for ln in lines if ln.startswith("**[@")] == ["P0", "P1", "P5", "P2", "P3", "P4"]
    assert lines.index("Framing for A.") < lines.index("**[@P0]**")
    assert "Closing remark." in lines  # the last entry's paragraph moves with it
    assert "Some." in lines

    # The claim check is told a bullet belongs to its paper's entry; removing an entry removes its bullets.
    prompt = "\n".join(p for _, p in claim_check_prompts(fixed))
    assert "[entry @P2]: **Method.** P2 method." in prompt
    out = remove_sentences(fixed, set(), {"P2"}).to_markdown()
    assert "P2" not in out and "P3 method." in out


def test_nested_bullets_keep_their_level():
    md = ("## Chronology\n\n### 2010–2014: A\n\n**[@P0]**\n- **Limitations.** The authors note two issues:\n"
          "  - short light curves;\n  - sparse sampling.\n- **Context.** Later work agrees.\n\n"
          "## Open problems\n\n  - An indented first item.\n      - Two levels in.\n")
    d = parse_draft(md)
    assert [b.depth for b in d.blocks if b.kind == "bullet"] == [0, 1, 1, 0, 0, 1]
    assert "issues:\n\n  - short light curves;\n\n  - sparse sampling.\n\n- **Context.**" in d.to_markdown()
    assert parse_draft(d.to_markdown()).to_markdown() == d.to_markdown()

    # Cutting the bullet that introduced the sub-items moves them up together.
    sid = next(s.sid for s, *_ in d.sentences() if "two issues" in s.text)
    assert [b.depth for b in remove_sentences(d, {sid}, set()).blocks if b.kind == "bullet"] == [0, 0, 0, 0, 1]


def test_mend_targets_are_the_sentences_right_after_cuts():
    d = parse_draft(f"## Overview\n\nA one [@{A}]. B two [@{A}]. C three [@{A}].\n\nD four [@{A}]. E five [@{A}].\n\n"
                    f"## Chronology\n\n### 2010–2014: X\n\n**[@{B}]** F six. G seven.\n")
    sid = {s.text[0]: s.sid for s, *_ in d.sentences()}
    targets = mend_targets(d, {sid[c] for c in "ABEF"}, set())
    assert [(s.text[0], before, removed) for s, before, removed in targets] == [
        ("C", "", [f"A one [@{A}].", f"B two [@{A}]."]), ("G", "", ["F six."])]
    assert [s.text[0] for s, *_ in mend_targets(d, {sid[c] for c in "BF"}, {B})] == ["C"]


def test_remove_sentences_and_entries():
    d = parse_draft(DRAFT)
    first = next(s for s, *_ in d.sentences())
    out = remove_sentences(d, {first.sid}, {B})
    md = out.to_markdown()
    assert "The field grew quickly" not in md and "Two camps formed" in md
    assert f"**[@{B}]**" not in md


def test_parse_labels():
    text = "S1: supported - fine\nS2: Partial – overstates\nS3 : unsupported: no card says so\nS4: no_claim"
    assert parse_labels(text) == {
        1: ("supported", "fine"), 2: ("partial", "overstates"),
        3: ("unsupported", "no card says so"), 4: ("no_claim", ""),
    }


# --- regressions from the code review ---------------------------------------------------------

import random

from litreview.verify import name_problems, strip_heading_problems, text_problems


def test_headings_are_checked_and_cleaned():
    md = (f"## Overview\n\nFine [@{A}].\n\n## Chronology\n\n"
          f"### 1995–2010: After Mayor & Queloz (1995) and [@2099ApJ...999....9Z]\n\n**[@{A}]** Text.\n")
    d = parse_draft(md)
    keys = check_keys(d, {A})
    names = check_names_and_quotes(d, lambda keys: [])
    assert any(i.heading and "2099ApJ" in i.detail for i in keys)
    assert any(i.heading and "author names with a year" in i.detail for i in names)
    cleaned, notes = strip_heading_problems(d, {i.heading for i in keys + names if i.heading})
    assert "### 1995–2010\n" in cleaned.to_markdown() and notes


def test_citations_outside_key_groups_are_rule4():
    md = ("## Overview\n\nRadii grow [see @2099ApJ...999....9Z]. Also 2012ApJ...750....1S says so. "
          "See arXiv:1801.06117 too. Or https://ui.adsabs.harvard.edu/abs/x. Old astro-ph/0501001 work.\n\n"
          f"- A wrapped bullet [@{A};\n  @2099Fake....1....1Z].\n")
    d = parse_draft(md)
    details = [i.detail for i in check_keys(d, {A})]
    assert "key not in the citable set: 2099ApJ...999....9Z" in details
    assert "key not in the citable set: 2099Fake....1....1Z" in details
    assert sum("outside a [@KEY] group" in x for x in details) == 4
    assert [b.kind for b in d.blocks] == ["h2", "para", "bullet"]


NAME_DROP_SENTENCES = [
    "As Smith 2019 showed, radii grow.", "Smith's 2019 analysis found it.", "The 2019 study by Smith found it.",
    "Smith and collaborators (2019) found it.", "Ünal et al. found it.", "Øyvind and Ødegaard (2015) agree.",
    "Smith et al. found it.", "Gaia DR2 (2018) gives distances.",
]
CLEAN_SENTENCES = [
    "In 2015, surveys grew.", "Between 2009 and 2016 the sample tripled.", "Surveys in the 2010s grew.",
    "Since 2012 the sample grew.", "In March 2015 the satellite failed.", "By 2010 most were known.",
    "The authors note that ages are uncertain.",
]


def test_name_patterns_catch_more_forms_without_flagging_plain_years():
    for s in NAME_DROP_SENTENCES:
        assert name_problems(s), s
    for s in CLEAN_SENTENCES:
        assert not name_problems(s), (s, name_problems(s))
    assert text_problems("Scope since the discovery by Mayor & Queloz (1995).")


def test_single_curly_quotes_are_checked():
    d = parse_draft(f"## Overview\n\nThey call it ‘a correlation nobody ever wrote’ [@{A}].")
    assert check_names_and_quotes(d, lambda keys: ["Something else entirely."])


def test_key_after_the_period_stays_with_its_sentence():
    assert split_sentences(f"Radii grow with flux. [@{A}] Later surveys disagree [@{B}].") == [
        f"Radii grow with flux. [@{A}]", f"Later surveys disagree [@{B}]."]
    assert split_sentences(f"Radii grow [@{A}]. [@{B}] measured it.") == [f"Radii grow [@{A}].", f"[@{B}] measured it."]


def test_parse_labels_tolerates_format_drift():
    text = "\n".join([
        "S12 [Overview]: unsupported - x", "**S1**: supported", "- S2: partial - y", "S3 — supported",
        "S4: no claim", "S5: **unsupported**", "S6. Supported.",
    ])
    labels = {k: v[0] for k, v in parse_labels(text).items()}
    assert labels == {12: "unsupported", 1: "supported", 2: "partial", 3: "supported", 4: "no_claim",
                      5: "unsupported", 6: "supported"}


def test_fix_chronology_handles_intro_only_periods():
    papers = make_papers(["2010-01", "2011-01", "2012-01", "2015-01", "2016-01", "2017-01"])
    md = ("## Chronology\n\n### 2010–2013: A\n\n**[@P0]** a.\n\n**[@P1]** b.\n\n**[@P3]** d.\n\n"
          "### 2014–2014: Interlude\n\nJust framing text.\n\n### 2014–2020: B\n\n**[@P2]** c.\n\n**[@P4]** e.\n\n**[@P5]** f.\n")
    fixed, _ = fix_chronology(parse_draft(md), papers)
    assert check_chronology(fixed, papers, set(papers)) == []
    assert "Just framing text." in fixed.to_markdown()


def test_fix_chronology_always_produces_valid_periods():
    rng = random.Random(1)
    for trial in range(300):
        n = rng.randint(1, 14)
        years = [rng.randint(2000, 2024) for _ in range(n)]
        papers = {f"P{i}": paper(f"P{i}", f"{y}-0{rng.randint(1, 9)}") for i, y in enumerate(years)}
        cuts = sorted(rng.sample(range(2000, 2025), rng.randint(1, 5)))
        periods, keys = [], list(papers)
        rng.shuffle(keys)
        for j, start in enumerate(cuts):
            end = cuts[j + 1] - 1 if j + 1 < len(cuts) else 2024
            periods.append((f"{start}–{end}: T{j}", []))
        for k in keys:
            periods[rng.randrange(len(periods))][1].append(k)
        fixed, _ = fix_chronology(chron(periods), papers)
        issues = [i.detail for i in check_chronology(fixed, papers, set(papers))]
        allowed = [x for x in issues if x == "1 periods; use between 2 and 6"]
        assert issues == allowed, (trial, issues)
