import gzip
import io
import tarfile

import httpx
import pymupdf
import pytest

from litreview.arxiv import ArxivClient, RateLimiter
from litreview.fulltext import (
    MAX_PARAGRAPHS,
    Paragraph,
    ParseError,
    block_text,
    fetch_fulltext,
    parse_arxiv_html,
    parse_latex_source,
    parse_pdf,
    ADS_SCAN,
    is_ocr,
    read_pdf,
    upload_pages,
)
from litreview.config import tessdata

MAIN_TEX = r"""\documentclass[twocolumn]{aastex631}
\usepackage{amsmath}
\newcommand{\project}{Kepler Survey}
\newcommand{\vect}[1]{\mathbf{#1}}
\def\hj{hot Jupiter}
% SECRET-PREAMBLE-COMMENT
\begin{document}
\title{A test paper}
\author[0000-0001]{A. Author}
\affiliation{University of Nowhere, Department of Tests}
\email{author@example.org}
\begin{abstract}
We study \hj{} planets with the \project. Masses are $1.2\msun$ and 10\% are inflated. % SECRET-ABSTRACT-COMMENT
\end{abstract}
\keywords{planets: test}
\section{Introduction}\label{sec:intro}
\input{sections/intro}
\iffalse
SECRET-IFFALSE this text is switched off
\else
The else branch of a false conditional is typeset, so it stays in the output text.
\fi
\begin{comment}
SECRET-COMMENT-ENV
\end{comment}
\section{Methods}
\subsection{Data}\label{sec:data}
We fit 12 transits~\citep[e.g.,][]{smith2015,jones2016} using a Gaussian process; see Section~\ref{sec:intro} and Figure~\ref{fig:one}.
The mass is $M = 3\,\msun$ and the speed is 10 \kms. A footnote here\footnote{Footnote text with detail.}.
\begin{equation}
  R \propto F^{0.5} \label{eq:r}
\end{equation}
where the exponent follows \citet{doe2019}.

\begin{figure}
\plotone{fig1.pdf}
\caption{Radius versus flux for the sample. \label{fig:one}}
\end{figure}

\begin{deluxetable}{lcc}
\tablecaption{Planet parameters for the twelve targets.\label{tab:p}}
\tablehead{\colhead{Name} & \colhead{Radius}}
\startdata
SECRET-TABLE-ROW & 1 \\
\enddata
\end{deluxetable}

\subsection*{A starred subsection}
Text in a starred subsection that is long enough to keep in the output here.

\section{Discussion}
\paragraph{Run-in head} The discussion paragraph shows that the radius is inflated, unlike in previous work.

\acknowledgments
SECRET-ACK We thank the funding agency for support of this work over many years.

\software{astropy}
\bibliography{refs}

\appendix
\section{Derivation}
The appendix text explains how the derivation proceeds, in some useful detail here.
\end{document}
"""

INTRO_TEX = r"""Hot Jupiters are giant planets on short orbits, and many are inflated relative to models.
Their radii exceed predictions. % SECRET-INPUT-COMMENT
A budget of 100\% was spent, and the \% sign must survive comment stripping.

Second introduction paragraph about the \hj{} inflation problem, described at some length.
"""


def make_tar(files: dict[str, str], gz: bool = True) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def paper_tar() -> bytes:
    return make_tar(
        {
            "ms.tex": MAIN_TEX,
            "sections/intro.tex": INTRO_TEX,
            "../evil.tex": r"\documentclass{article}\begin{document}SECRET-EVIL\end{document}",
            "/abs.tex": r"\documentclass{article}\begin{document}SECRET-ABS\end{document}",
        }
    )


def make_pdf(pages: list[list[tuple[str, float]]]) -> bytes:
    """Each page is a list of (text, fontsize) blocks, laid out top to bottom."""
    doc = pymupdf.open()
    for blocks in pages:
        page = doc.new_page()
        y = 72
        for text, size in blocks:
            spare = page.insert_textbox(pymupdf.Rect(72, y, 540, y + 400), text, fontsize=size)
            y += (400 - spare if spare >= 0 else 400) + 12
    data = doc.tobytes()
    doc.close()
    return data


def all_text(paragraphs: list[Paragraph]) -> str:
    return "\n".join(block_text(p) for p in paragraphs)


# LaTeX


def test_latex_paragraphs_and_labels():
    paras = parse_latex_source(paper_tar())
    labels = [p.label for p in paras]
    assert labels == [
        "Abstract",
        "1 Introduction, para 1",
        "1 Introduction, para 2",
        "1 Introduction, para 3",
        "2.1 Data, para 1",
        "Figure 1 caption",
        "Table 1 caption",
        "A starred subsection, para 1",
        "3 Discussion, para 1",
        "A Derivation, para 1",
    ]
    by_label = {p.label: p.text for p in paras}
    assert by_label["Abstract"].startswith("We study hot Jupiter planets with the Kepler Survey.")
    assert "$1.2M_\\odot$" in by_label["Abstract"]
    assert "10% are inflated" in by_label["Abstract"]
    data = by_label["2.1 Data, para 1"]
    assert "transits [ref] using" in data
    assert "see Section 1 and Figure 1." in data
    assert "$M = 3\\,M_\\odot$" in data
    assert "10 km s⁻¹" in data
    assert "(footnote: Footnote text with detail.)" in data
    assert "$$R \\propto F^{0.5}$$" in data
    assert "follows [ref]." in data
    assert by_label["Figure 1 caption"] == "Radius versus flux for the sample."
    assert by_label["Table 1 caption"] == "Planet parameters for the twelve targets."
    assert by_label["3 Discussion, para 1"].startswith("Run-in head. The discussion")
    assert "the % sign must survive" in by_label["1 Introduction, para 1"]
    assert "else branch" in by_label["1 Introduction, para 3"]


def test_latex_drops_comments_matter_and_unsafe_members():
    text = all_text(parse_latex_source(paper_tar()))
    for secret in ("SECRET", "University of Nowhere", "author@example.org", "astropy", "funding agency",
                   "planets: test", "A test paper"):
        assert secret not in text


def test_single_gzipped_file_and_plain_tar():
    single = gzip.compress(MAIN_TEX.replace("\\input{sections/intro}", INTRO_TEX).encode())
    paras = parse_latex_source(single)
    assert paras[1].label == "1 Introduction, para 1"
    plain = make_tar({"main.tex": MAIN_TEX, "sections/intro.tex": INTRO_TEX}, gz=False)
    assert [p.label for p in parse_latex_source(plain)] == [p.label for p in parse_latex_source(paper_tar())]


def test_pdf_only_and_missing_documentclass():
    pdf = make_pdf([[("Hello", 11)]])
    with pytest.raises(ParseError, match="pdf-only submission"):
        parse_latex_source(pdf)
    with pytest.raises(ParseError, match="pdf-only submission"):
        parse_latex_source(gzip.compress(pdf))
    with pytest.raises(ParseError, match="no \\\\documentclass"):
        parse_latex_source(make_tar({"notes.tex": "just some notes"}))


def test_main_file_choice_and_aa_abstract():
    aa = r"""\documentclass{aa}
\begin{document}
\title{X}
\institute{SECRET-INSTITUTE}
\abstract{\context{Planets are inflated.}\aims{We test why this is so.}\results{It is the flux.}}
\maketitle
\section{Introduction}
A&A papers keep their abstract in the preamble-like front matter, and we must find it here.
\begin{acknowledgements}
SECRET-AA-ACK thanks
\end{acknowledgements}
\end{document}
"""
    tar = make_tar({"aux.tex": r"\documentclass{article} short", "paper.tex": aa})
    paras = parse_latex_source(tar)
    assert paras[0].label == "Abstract"
    assert paras[0].text.startswith("Context: Planets are inflated. Aims: We test why this is so.")
    assert "SECRET" not in all_text(paras)


def test_acknowledgements_section_and_appendix_letters():
    tex = r"""\documentclass{mnras}
\begin{document}
\section{Results}
The results paragraph is long enough to be kept by the parser in this test case.
\section*{Acknowledgements}
SECRET-MNRAS-ACK We thank everyone who helped with this work in any way at all.
\section*{Data Availability}
The data underlying this article will be shared on reasonable request to the author.
\appendix
\section{First appendix}
\subsection{Detail}
Appendix subsection text that is long enough to be kept by the parser here.
\end{document}
"""
    paras = parse_latex_source(make_tar({"ms.tex": tex}))
    assert [p.label for p in paras] == [
        "1 Results, para 1",
        "Data Availability, para 1",
        "A.1 Detail, para 1",
    ]
    assert "SECRET" not in all_text(paras)


# HTML

HTML = """<html><head><title>x</title></head><body>
<header class="desktop_header">SECRET-BANNER</header>
<article class="ltx_document">
<div class="ltx_authors"><span class="ltx_creator">SECRET-AUTHOR, University of Nowhere</span></div>
<div class="ltx_abstract"><h6 class="ltx_title ltx_title_abstract">Abstract</h6>
<p class="ltx_p">We study inflated planets and find that radius tracks flux <math alttext="R\\propto F" display="inline"><mi>R</mi></math> closely.</p></div>
<section class="ltx_section" id="S1"><h2 class="ltx_title ltx_title_section"><span class="ltx_tag ltx_tag_section">1 </span>Introduction</h2>
<div class="ltx_para" id="S1.p1"><p class="ltx_p">Hot Jupiters are inflated <cite class="ltx_cite">(Smith et al. 2015)</cite>.
A footnote<span class="ltx_note ltx_role_footnote"><sup class="ltx_note_mark">1</sup><span class="ltx_note_outer"><span class="ltx_note_content"><sup class="ltx_note_mark">1</sup>Footnote words here.</span></span></span> appears.</p></div>
<figure class="ltx_figure" id="S1.F1"><img src="x.png"/><figcaption class="ltx_caption"><span class="ltx_tag ltx_tag_figure">Figure 1: </span>Radius against flux for the full sample.</figcaption></figure>
<section class="ltx_subsection" id="S1.SS1"><h3 class="ltx_title ltx_title_subsection"><span class="ltx_tag ltx_tag_subsection">1.1 </span>Data</h3>
<div class="ltx_para"><p class="ltx_p">The data paragraph has an equation
<table class="ltx_equation"><tr><td><math alttext="E=mc^{2}" display="block"><mi>E</mi></math></td><td><span class="ltx_tag ltx_tag_equation">(1)</span></td></tr></table>
and continues afterwards with more words.</p></div>
</section></section>
<section class="ltx_section"><h2 class="ltx_title ltx_title_section">Acknowledgements</h2>
<div class="ltx_para"><p class="ltx_p">SECRET-HTML-ACK We thank the agency for the money.</p></div></section>
<section class="ltx_bibliography"><h2 class="ltx_title">References</h2><ul><li>SECRET-BIB Smith 2015</li></ul></section>
</article>
<div class="ltx_page_footer">SECRET-FOOTER</div>
</body></html>"""


def test_html_parse():
    paras = parse_arxiv_html(HTML)
    labels = [p.label for p in paras]
    assert labels == ["Abstract", "1 Introduction, para 1", "Figure 1 caption", "1.1 Data, para 1"]
    by_label = {p.label: p.text for p in paras}
    assert "$R\\propto F$" in by_label["Abstract"]
    assert by_label["1 Introduction, para 1"] == (
        "Hot Jupiters are inflated [ref]. A footnote (footnote: Footnote words here.) appears."
    )
    assert by_label["Figure 1 caption"] == "Radius against flux for the full sample."
    assert "$$E=mc^{2}$$" in by_label["1.1 Data, para 1"]
    assert "(1)" not in by_label["1.1 Data, para 1"]
    assert "SECRET" not in all_text(paras)


# PDF


def body(n: int) -> str:
    return (f"Paragraph {n} of the body text discusses the radius inflation of hot Jupiters in some "
            "detail, so that it is long enough to be kept as its own paragraph.")


def test_pdf_parse_headings_and_references():
    pdf = make_pdf([
        [("1 Introduction", 14), (body(1), 10), (body(2), 10)],
        [("2 Methods", 14), (body(3), 10), ("Figure 1. Radius versus flux for all the planets.", 10)],
        [("Acknowledgements", 14), ("SECRET-PDF-ACK We thank the agency for its support over the years.", 10),
         ("References", 14), ("SECRET-PDF-REF Smith, J. 2015, ApJ, 1, 1 and other references here.", 10)],
    ])
    paras = parse_pdf(pdf)
    assert [p.label for p in paras] == [
        "1 Introduction, para 1",
        "1 Introduction, para 2",
        "2 Methods, para 1",
        "Figure 1 caption",
    ]
    assert "SECRET" not in all_text(paras)


def long_body(n: int) -> str:
    return " ".join([body(n)] * 3)


BOOK = [
    [("Preface", 16), ("SECRET-PREFACE " + long_body(0), 10)],
    [("3 Orbits", 16), (long_body(1), 10), (long_body(2), 10),
     ("Figure 3.2 The orbit of a star in a spherical potential.", 10), (long_body(3), 10)],
    [("3.1 Spherical potentials", 14), (long_body(4), 10), (long_body(5), 10),
     ("References", 14), ("SECRET-REF Smith, J. 2015, ApJ, 1, 1 and other references here.", 10)],
    [("4 Disks", 16), (long_body(6), 10), (long_body(7), 10), (long_body(8), 10)],
    [("Index", 14), ("SECRET-INDEX accretion, 12, 40; bars, 101, 230; disks, 4, 77, 90 and more", 10)],
]


def test_an_uploaded_book_is_read_from_the_chosen_pages_with_page_numbers_in_its_labels():
    pdf = make_pdf(BOOK)
    text = read_pdf(pdf, (2, 5))
    assert text.source == "upload" and upload_pages(text.version) == (2, 5)
    assert [p.label for p in text.paragraphs] == [
        "3 Orbits, para 1, p. 2",
        "3 Orbits, para 2, p. 2",
        "Figure 3.2 caption, p. 2",
        "3 Orbits, para 3, p. 2",
        "3.1 Spherical potentials, para 1, p. 3",
        "3.1 Spherical potentials, para 2, p. 3",
        # A chapter's references are skipped, not the end of the text; so is the index.
        "4 Disks, para 1, p. 4",
        "4 Disks, para 2, p. 4",
        "4 Disks, para 3, p. 4",
    ]
    assert "SECRET" not in all_text(text.paragraphs)

    whole = read_pdf(pdf)
    assert upload_pages(whole.version) is None and whole.version == text.version.split(":")[0]
    assert whole.paragraphs[0].label == "Preface, para 1, p. 1"


def test_an_uploaded_pdf_must_have_the_pages_asked_for_and_enough_text():
    pdf = make_pdf(BOOK)
    with pytest.raises(ParseError, match="pages 4-9 aren't in the PDF, which has 5 pages"):
        read_pdf(pdf, (4, 9))
    with pytest.raises(ParseError, match="too little text"):
        read_pdf(pdf, (5, 5))
    blank = pymupdf.open()
    blank.new_page()
    with pytest.raises(ParseError, match="no text layer"):
        read_pdf(blank.tobytes())
    with pytest.raises(ParseError, match="OCR isn't set up"):
        read_pdf(blank.tobytes(), ocr=False)


def close(read: list[str], printed: list[str]) -> bool:
    """Whether OCR read each paragraph, allowing a misread character or space here and there."""
    import difflib

    return len(read) == len(printed) and all(difflib.SequenceMatcher(None, a, b).ratio() > 0.97
                                             for a, b in zip(read, printed))


def scanned(pdf: bytes) -> bytes:
    """The same pages as images only, like a scan."""
    src, out = pymupdf.open(stream=pdf, filetype="pdf"), pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=200)
        out.new_page(width=page.rect.width, height=page.rect.height).insert_image(page.rect, pixmap=pix)
    return out.tobytes()


@pytest.mark.skipif(tessdata() is None, reason="OCR needs Tesseract's English data (see the README)")
def test_scanned_pages_are_read_by_ocr():
    scan = scanned(make_pdf([[("3 Orbits", 16), (long_body(i), 10), (long_body(i + 1), 10)] for i in (1, 3, 5, 7, 9)]))
    with pytest.raises(ParseError, match="OCR isn't set up"):
        read_pdf(scan, ocr=False)
    text = read_pdf(scan, source=ADS_SCAN, book=False)
    assert text.source == "ads_scan" and is_ocr(text.version) and not upload_pages(text.version)
    assert close([p.text for p in text.paragraphs], [long_body(i) for i in range(1, 11)])
    assert text.paragraphs[0].label == "3 Orbits, para 1"
    pages = read_pdf(scan, (2, 5))
    assert is_ocr(pages.version) and upload_pages(pages.version) == (2, 5)
    assert pages.paragraphs[0].label == "3 Orbits, para 1, p. 2"


def test_a_books_running_heads_are_dropped_even_when_each_spans_few_pages():
    doc = pymupdf.open()
    for i in range(1, 11):
        page = doc.new_page()
        head = (f"CHAPTER 3. ORBITS {120 + i}" if i % 2 == 0 else
                f"3.1 SPHERICAL POTENTIALS {120 + i}" if i < 6 else f"3.2 DISK POTENTIALS {120 + i}")
        page.insert_text((72, 40), head, fontsize=9)
        y = 72
        if i == 1:
            page.insert_textbox(pymupdf.Rect(72, y, 540, y + 40), "3.1 Spherical potentials", fontsize=14)
            y += 40
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 400), long_body(i), fontsize=10)
    text = read_pdf(doc.tobytes())
    assert {p.label.split(",")[0] for p in text.paragraphs} == {"3.1 Spherical potentials"}
    assert not any(w in all_text(text.paragraphs) for w in ("CHAPTER", "SPHERICAL", "DISK POTENTIALS"))


def test_words_in_a_books_figures_and_equations_are_not_headings():
    doc = pymupdf.open()
    for i in range(1, 5):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 540, 120), f"{i} Chapter {i}", fontsize=16)
        page.insert_textbox(pymupdf.Rect(72, 130, 540, 330), long_body(i), fontsize=10)
        page.insert_textbox(pymupdf.Rect(72, 340, 540, 360), "Earth", fontsize=11.3)  # a figure's labels
        page.insert_textbox(pymupdf.Rect(72, 370, 540, 390), "timelike", fontsize=14)
        page.insert_text((72, 410), "00", fontsize=6)  # a prime, then the line before an equation
        page.insert_text((80, 410), ". From this we get", fontsize=10)
        page.insert_textbox(pymupdf.Rect(72, 420, 540, 620), long_body(i + 3), fontsize=10)
    labels = [p.label for p in read_pdf(doc.tobytes()).paragraphs]
    assert labels == [f"{i} Chapter {i}, para {n}, p. {i}" for i in range(1, 5) for n in (1, 2)]


def test_pdf_hyphenation_join():
    from litreview.fulltext import _join_lines

    assert _join_lines(["the infla-", "tion of planets", "Next-", "Generation"]) == (
        "the inflation of planets Next- Generation"
    )


# fetch_fulltext


def client_for(routes: dict[str, httpx.Response], seen: list[str]) -> ArxivClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        for prefix, resp in routes.items():
            if request.url.path == prefix:
                return httpx.Response(resp.status_code, headers=resp.headers, content=resp.content,
                                      request=request)
        return httpx.Response(404, request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return ArxivClient(http=http, export_limiter=RateLimiter(0), main_limiter=RateLimiter(0))


def long_tar() -> bytes:
    paras = "\n\n".join(body(i) for i in range(1, 40))
    tex = rf"""\documentclass{{aastex631}}
\begin{{document}}
\section{{Results}}
{paras}
\end{{document}}
"""
    return make_tar({"ms.tex": tex})


def test_fetch_prefers_source_and_reads_version_from_content_disposition():
    seen: list[str] = []
    client = client_for(
        {"/src/2101.01234": httpx.Response(
            200, content=long_tar(),
            headers={"content-disposition": 'attachment; filename="arXiv-2101.01234v3.tar.gz"'})},
        seen,
    )
    text, attempts = fetch_fulltext(client, "2101.01234")
    assert text is not None and text.source == "arxiv_src"
    assert text.version == "2101.01234v3"
    assert [a.source for a in attempts] == ["arxiv_src"] and attempts[0].ok
    assert seen == ["/src/2101.01234"]
    assert not text.truncated


def test_fetch_falls_through_404_pdf_only_and_short_text():
    long_html = HTML.replace(
        '<section class="ltx_bibliography">',
        '<section class="ltx_section"><h2 class="ltx_title">'
        '<span class="ltx_tag">2 </span>More</h2>'
        + "".join(f'<div class="ltx_para"><p class="ltx_p">{body(i)}</p></div>' for i in range(30))
        + "</section><section class=\"ltx_bibliography\">",
    )
    pdf_body = make_pdf([[("Hello world", 11)]])

    seen: list[str] = []
    client = client_for(
        {
            "/src/2101.01234v2": httpx.Response(200, content=gzip.compress(pdf_body)),
            "/html/2101.01234v2": httpx.Response(200, content=long_html.encode(),
                                                 headers={"content-type": "text/html"}),
        },
        seen,
    )
    text, attempts = fetch_fulltext(client, "2101.01234", version="2101.01234v2")
    assert text is not None and text.source == "arxiv_html" and text.version == "2101.01234v2"
    assert [(a.source, a.ok, a.reason) for a in attempts] == [
        ("arxiv_src", False, "pdf-only submission"),
        ("arxiv_html", True, ""),
    ]

    seen = []
    short_tar = make_tar({"ms.tex": r"\documentclass{x}\begin{document}\section{A}" + body(1) +
                          r"\end{document}"})
    client = client_for({"/src/2101.01234": httpx.Response(200, content=short_tar)}, seen)
    text, attempts = fetch_fulltext(client, "2101.01234")
    assert text is None
    assert attempts[0].reason.startswith("too little text (1 paragraphs")
    assert [(a.source, a.reason) for a in attempts[1:]] == [("arxiv_html", "http 404"), ("arxiv_pdf", "http 404")]
    assert seen == ["/src/2101.01234", "/html/2101.01234", "/pdf/2101.01234"]


def test_fetch_html_version_from_final_url_and_truncation():
    many = "".join(f'<div class="ltx_para"><p class="ltx_p">{body(i)}</p></div>' for i in range(MAX_PARAGRAPHS + 20))
    html = f'<html><body><article class="ltx_document"><section class="ltx_section"><h2 class="ltx_title">' \
           f'<span class="ltx_tag">1 </span>Body</h2>{many}</section></article></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/html/2101.01234":
            return httpx.Response(301, headers={"location": "https://arxiv.org/html/2101.01234v4"}, request=request)
        if request.url.path == "/html/2101.01234v4":
            return httpx.Response(200, content=html.encode(), headers={"content-type": "text/html"}, request=request)
        return httpx.Response(404, request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    client = ArxivClient(http=http, export_limiter=RateLimiter(0), main_limiter=RateLimiter(0))
    text, attempts = fetch_fulltext(client, "2101.01234")
    assert text.source == "arxiv_html" and text.version == "2101.01234v4"
    assert text.truncated and len(text.paragraphs) == MAX_PARAGRAPHS
    assert attempts[0].reason == "http 404"


def test_fetch_pdf_version_and_old_style_id():
    pages = [[("1 Introduction", 14)] + [(body(i), 10) for i in range(1, 3)]] + [
        [(body(i), 10) for i in range(k, k + 3)] for k in range(3, 30, 3)
    ]
    pdf = make_pdf(pages)
    seen: list[str] = []
    client = client_for(
        {"/pdf/astro-ph/9911436": httpx.Response(
            200, content=pdf, headers={"content-disposition": 'inline; filename="astro-ph9911436v1.pdf"'})},
        seen,
    )
    text, attempts = fetch_fulltext(client, "astro-ph/9911436")
    assert text is not None and text.source == "arxiv_pdf"
    assert text.version == "astro-ph/9911436v1"
    assert [a.ok for a in attempts] == [False, False, True]


def test_block_text():
    assert block_text(Paragraph("4.2 Discussion, para 3", "Text.")) == "[4.2 Discussion, para 3] Text."


def test_archive_limits_and_links(monkeypatch):
    import litreview.fulltext as ft

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        link = tarfile.TarInfo("link.tex")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
        for name, text in {"ms.tex": MAIN_TEX, "sections/intro.tex": INTRO_TEX}.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    assert parse_latex_source(buf.getvalue())[0].label == "Abstract"

    monkeypatch.setattr(ft, "MAX_TOTAL_BYTES", 1000)
    with pytest.raises(ParseError, match="100 MB"):
        parse_latex_source(paper_tar())
    monkeypatch.setattr(ft, "MAX_TOTAL_BYTES", 100 * 2**20)
    monkeypatch.setattr(ft, "MAX_MEMBER_BYTES", 1000)
    # the main file is now too big to read, so there is no \documentclass left
    with pytest.raises(ParseError):
        parse_latex_source(paper_tar())
