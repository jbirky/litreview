from litreview.extract import NOT_ASSESSED, build_card, document_block, parse_tagged, verify_card
from litreview.library import Paper, StoredText


def blocks(*parts):
    """parts: str or (str, [(start, end, cited_text)])"""
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append({"text": p, "citations": []})
        else:
            text, cites = p
            out.append({"text": text, "citations": [
                {"start_block_index": s, "end_block_index": e, "cited_text": c, "document_index": 0}
                for s, e, c in cites]})
    return out


REPLY = blocks(
    "RELEVANCE: high - measures the trend directly\nMETHOD: ",
    ("Joint fit of 12 transits", [(14, 16, "We fit twelve transits jointly.")]),
    "\nFINDING: ",
    ("Radius inflation correlates with flux", [(41, 42, "Inflation rises with flux.")]),
    "\nFINDING: A claim with no citation\nLIMITATION (stated): ",
    ("Ages are uncertain by 50%", [(88, 89, "Ages are uncertain.")]),
    "\nLIMITATION (inferred): ",
    ("Only hot Jupiters were sampled", [(9, 10, "Our sample: hot Jupiters.")]),
)

PAPER = Paper(bibcode="2019ApJ...880....1S", pubdate="2019-04-00", title="T", first_arxiv_date="2019-01-15")


def text(n=100, source="arxiv_src"):
    return StoredText(PAPER.bibcode, source, "1901.00001v2", [{"label": f"p{i}", "text": f"t{i}"} for i in range(n)], False)


def test_parse_tagged_attaches_citations_to_their_lines():
    items = parse_tagged(REPLY)
    assert [(i.tag, i.text, i.passages) for i in items] == [
        ("RELEVANCE", "high - measures the trend directly", []),
        ("METHOD", "Joint fit of 12 transits", [14, 15]),
        ("FINDING", "Radius inflation correlates with flux", [41]),
        ("FINDING", "A claim with no citation", []),
        ("LIMITATION (stated)", "Ages are uncertain by 50%", [88]),
        ("LIMITATION (inferred)", "Only hot Jupiters were sampled", [9]),
    ]
    assert items[2].passage_text == ["Inflation rises with flux."]


def test_build_and_verify_card():
    card = build_card(PAPER, text(), parse_tagged(REPLY))
    assert card["relevance"] == "high" and card["sort_date"] == "2019-01-15"
    card, status, notes = verify_card(card, 100)
    assert status == "verified"
    assert [f["text"] for f in card["findings"]] == ["Radius inflation correlates with flux"]
    assert [l["source"] for l in card["limitations"]] == ["stated", "inferred"]
    assert any("no cited passage" in n for n in notes)


def test_passages_outside_the_text_are_dropped():
    card = build_card(PAPER, text(30), parse_tagged(REPLY))
    card, status, _ = verify_card(card, 30)
    assert [m["text"] for m in card["methods"]] == ["Joint fit of 12 transits"]
    assert card["findings"] == [] and status == "no_findings"


def test_abstract_only_drops_inferred_and_marks_not_assessed():
    reply = blocks("RELEVANCE: medium - ok\nFINDING: ", ("X rises", [(0, 1, "X rises.")]),
                   "\nLIMITATION (inferred): ", ("Small sample", [(0, 1, "X rises.")]))
    card = build_card(PAPER, text(1, "abstract"), parse_tagged(reply))
    card, status, notes = verify_card(card, 1)
    assert status == "verified" and card["limitations"] == []
    assert card["limitations_note"] == NOT_ASSESSED
    assert any("inferred limitation" in n for n in notes)


def test_relevance_none_is_excluded():
    card = build_card(PAPER, text(), parse_tagged(blocks("RELEVANCE: none - about something else")))
    _, status, _ = verify_card(card, 100)
    assert status == "excluded"


def test_document_block_is_custom_content_with_labels():
    doc = document_block(PAPER, text(2))
    assert doc["source"]["type"] == "content"
    assert [c["text"] for c in doc["source"]["content"]] == ["[p0] t0", "[p1] t1"]
    assert doc["citations"] == {"enabled": True}


def test_tag_variants_parse_and_unknown_lines_are_dropped():
    reply = blocks(
        "Here are my notes.\n**RELEVANCE**: high - yes\n1. Finding: ",
        ("Radius grows", [(1, 2, "grows")]),
        "\n- **LIMITATION (Inferred)**: ",
        ("Small sample", [(2, 3, "sample")]),
        "\nThis line has no tag at all.\nMETHODS: ",
        ("Transit fits", [(0, 1, "fits")]),
    )
    items = parse_tagged(reply)
    assert [(i.tag, i.text) for i in items] == [
        ("UNRECOGNIZED", "Here are my notes."), ("RELEVANCE", "high - yes"), ("FINDING", "Radius grows"),
        ("LIMITATION (inferred)", "Small sample"), ("UNRECOGNIZED", "This line has no tag at all."),
        ("METHOD", "Transit fits"),
    ]
    card, status, notes = verify_card(build_card(PAPER, text(), items), 100)
    assert status == "verified" and len(card["findings"]) == 1 and card["findings"][0]["passages"] == [1]
    assert sum("no recognised format" in n for n in notes) == 2


def test_abstract_only_inferred_limitation_cannot_hide_in_a_finding():
    reply = blocks("RELEVANCE: medium - ok\nFINDING: ", ("X rises", [(0, 1, "X rises.")]),
                   "\nLimitation (inferred): ", ("Small sample", [(0, 1, "X rises.")]))
    card, _, _ = verify_card(build_card(PAPER, text(1, "abstract"), parse_tagged(reply)), 1)
    assert [f["text"] for f in card["findings"]] == ["X rises"] and card["limitations"] == []


def test_summary_lines_parse_verify_and_stay_apart_from_findings():
    from litreview.extract import summary_of
    reply = REPLY + blocks(
        "\nSUMMARY (aim): ",
        ("Measure inflation across the sample", [(2, 3, "We measure...")]),
        "\n**Summary (Results):** ",
        ("Inflation peaks at intermediate flux", [(41, 42, "peaks")]),
        "\nSUMMARY (method): A method with no citation",
        "\nSUMMARY (limitation): ",
        ("Ages are uncertain", [(88, 89, "Ages are uncertain.")]),
    )
    text = StoredText(PAPER.bibcode, "arxiv_src", "1901.00001v1", [{"label": f"p{i}", "text": "x"} for i in range(100)], False)
    card, status, notes = verify_card(build_card(PAPER, text, parse_tagged(reply)), 100)
    assert status == "verified" and len(card["findings"]) == 1  # summary lines never become findings
    s = summary_of(card)
    assert [i["text"] for i in s["aim"]] == ["Measure inflation across the sample"]
    assert [i["text"] for i in s["results"]] == ["Inflation peaks at intermediate flux"]
    assert s["methods"] == [] and any("summary methods" in n for n in notes)
    assert s["limitations"][0]["passages"] == [88]


def test_summary_needs_a_cited_result():
    from litreview.extract import summary_of
    text = StoredText(PAPER.bibcode, "arxiv_src", "1901.00001v1", [{"label": "p", "text": "x"}] * 100, False)
    card, _, _ = verify_card(build_card(PAPER, text, parse_tagged(blocks("RELEVANCE: none - off topic\nSUMMARY (aim): ",
                                                                         ("An aim", [(1, 2, "a")])))), 100)
    assert summary_of(card) is None


def test_request_asks_for_a_summary_only_of_full_texts():
    from litreview.extract import SUMMARY_OFF, SUMMARY_ON, request_params
    full = StoredText(PAPER.bibcode, "arxiv_src", "v1", [{"label": "p", "text": "x"}], False)
    abstract = StoredText(PAPER.bibcode, "abstract", "ads:1", [{"label": "Abstract", "text": "x"}], False)
    ask = lambda t, on: request_params("q", PAPER, t, summary=on)["messages"][0]["content"][1]["text"]
    assert SUMMARY_ON in ask(full, True)
    assert SUMMARY_OFF in ask(full, False) and SUMMARY_OFF in ask(abstract, True)


def test_books_and_uploaded_pages_are_described_to_the_reader():
    from litreview.extract import BOOK_NOTE, request_params, summary_params

    book = Paper(bibcode="2008gady.book.....B", pubdate="2008-00-00", title="Galactic Dynamics", doctype="book",
                 pub="Galactic Dynamics: Second Edition")
    pages = StoredText(book.bibcode, "upload", "3f9a1c2b7d4e:p120-180", [{"label": "p", "text": "x"}], False)
    assert document_block(book, pages)["context"] == (
        "A book. Published 2008 in Galactic Dynamics: Second Edition. Read depth: pages 120-180 of an uploaded PDF.")
    whole = StoredText(book.bibcode, "upload", "3f9a1c2b7d4e", [{"label": "p", "text": "x"}], False)
    assert document_block(book, whole)["context"].endswith("Read depth: full text.")
    note = BOOK_NOTE.format(kind="book")
    assert note in request_params("Q?", book, pages)["messages"][0]["content"][1]["text"]
    assert note in summary_params(book, pages)["messages"][0]["content"][1]["text"]

    # A paper's request is as it was.
    assert document_block(PAPER, text())["context"] == "Published 2019 in unknown venue. Read depth: full text."
    assert "book" not in request_params("Q?", PAPER, text())["messages"][0]["content"][1]["text"]
