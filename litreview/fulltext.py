"""Full text from arXiv (LaTeX source first, then HTML, then PDF), or from a PDF (an upload, a scan
ADS hosts, or an open-access copy), as labelled paragraphs. Pages of a PDF that are only a scanned
image are read by OCR, when Tesseract's language data is installed (config.tessdata).

Each paragraph's label ("4.2 Discussion, para 3", "Figure 3 caption") becomes part of the content
block sent to Claude, so the reference list, acknowledgements and affiliations are dropped here.
LaTeX comments never reach the output, because they are not part of the published paper.

Source archives are untrusted: they are read in memory (nothing is written to disk), only .tex and
.sty members are decoded, and TeX is never run.
"""

from __future__ import annotations

import hashlib
import io
import logging
import multiprocessing
import os
import posixpath
import re
import tarfile
import zlib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import httpx

from .arxiv import ArxivClient, ArxivThrottled, split_version
from .config import tessdata

RATE_LIMITED = "rate limited"
UPLOAD = "upload"  # the source of a text read from an uploaded PDF
ADS_SCAN = "ads_scan"  # a scan of an older paper that ADS hosts
UNPAYWALL = "unpaywall"  # an open-access copy that Unpaywall found by DOI

logging.getLogger("pylatexenc").setLevel(logging.ERROR)

MIN_PARAGRAPHS = 5
MIN_CHARS = 3000
MAX_PARAGRAPHS = 400
UPLOAD_MAX_PARAGRAPHS = 1000  # a book's displayed equations and short lines are paragraphs of their own
MAX_CHARS = 240_000
MIN_PARAGRAPH_CHARS = 40

MAX_MEMBER_BYTES = 20 * 2**20  # per file read from an archive
MAX_TOTAL_BYTES = 100 * 2**20  # all files read from one archive
MAX_ARCHIVE_BYTES = 2 * 2**30  # everything a tar stream may walk through, read or skipped


@dataclass
class Paragraph:
    label: str
    text: str


@dataclass
class FullText:
    source: str  # "arxiv_src" | "arxiv_html" | "arxiv_pdf" | "upload" | "ads_scan" | "unpaywall"
    version: str  # versioned id actually read, e.g. "2101.01234v3"; for any other PDF, see read_pdf
    paragraphs: list[Paragraph]
    truncated: bool


@dataclass
class Attempt:
    source: str
    ok: bool
    reason: str


class ParseError(Exception):
    pass


def block_text(p: Paragraph) -> str:
    """The exact string sent to Claude as one content block."""
    return f"[{p.label}] {p.text}"


# ---------------------------------------------------------------------------
# Fetching


def fetch_fulltext(
    client: ArxivClient, arxiv_id: str, version: str | None = None
) -> tuple[FullText | None, list[Attempt]]:
    """Try source, HTML, then PDF; return the first that yields enough text, plus every attempt."""
    base, own_version = split_version(arxiv_id)
    if version and version.startswith("v"):
        version = base + version
    requested = version or (arxiv_id if own_version else base)

    attempts: list[Attempt] = []
    readers = (
        ("arxiv_src", client.source, lambda r: parse_latex_source(r.content)),
        ("arxiv_html", client.html, lambda r: parse_arxiv_html(r.text)),
        ("arxiv_pdf", client.pdf, lambda r: parse_pdf(r.content)),
    )
    for source, get, parse in readers:
        try:
            resp = get(requested)
        except ArxivThrottled:
            attempts.append(Attempt(source, False, RATE_LIMITED))
            continue
        except httpx.HTTPError as e:
            attempts.append(Attempt(source, False, f"network error: {type(e).__name__}"))
            continue
        if resp.status_code != 200:
            attempts.append(Attempt(source, False, f"http {resp.status_code}"))
            continue
        try:
            paragraphs = parse(resp)
        except ParseError as e:
            attempts.append(Attempt(source, False, str(e)))
            continue
        except Exception as e:  # a parser bug must not stop the fallbacks
            attempts.append(Attempt(source, False, f"parse error: {type(e).__name__}: {e}"[:200]))
            continue
        n, chars = len(paragraphs), sum(len(p.text) for p in paragraphs)
        if n < MIN_PARAGRAPHS or chars < MIN_CHARS:
            attempts.append(Attempt(source, False, f"too little text ({n} paragraphs, {chars} chars)"))
            continue
        paragraphs, truncated = _cap(paragraphs)
        read_version = (requested if split_version(requested)[1] else None) or _version_from_response(
            resp, base
        )
        attempts.append(Attempt(source, True, ""))
        return FullText(source, read_version or base, paragraphs, truncated), attempts
    return None, attempts


def _cap(paragraphs: list[Paragraph], max_paragraphs: int = MAX_PARAGRAPHS) -> tuple[list[Paragraph], bool]:
    kept: list[Paragraph] = []
    chars = 0
    for p in paragraphs:
        if len(kept) >= max_paragraphs or chars + len(p.text) > MAX_CHARS:
            return kept, True
        kept.append(p)
        chars += len(p.text)
    return kept, False


def _version_from_response(resp: httpx.Response, base: str) -> str | None:
    """Version from content-disposition (arXiv-1706.03762v7.tar.gz) or the final URL (/html/<id>v3)."""
    flat = re.escape(base.replace("/", ""))
    slashed = re.escape(base)
    pattern = re.compile(rf"(?:{slashed}|{flat})(v\d+)")
    for text in (resp.headers.get("content-disposition", ""), str(resp.url)):
        m = pattern.search(text)
        if m:
            return base + m.group(1)
    if "html" in resp.headers.get("content-type", ""):
        # arXiv's HTML pages name their version in links and metadata
        found = Counter(m.group(1) for m in pattern.finditer(resp.text[:500_000]))
        if found:
            return base + found.most_common(1)[0][0]
    return None


# ---------------------------------------------------------------------------
# LaTeX source


def parse_latex_source(data: bytes) -> list[Paragraph]:
    files = _read_archive(data)
    tex = {name: _strip_comments(text) for name, text in files.items() if not name.endswith(".sty")}
    main = _pick_main(tex)
    document = _inline_inputs(tex[main], posixpath.dirname(main), tex, depth=0, seen={main})
    document = _strip_conditionals(document)

    macros = {}
    for name, text in files.items():
        if name.endswith(".sty"):
            macros.update(_collect_macros(_strip_conditionals(_strip_comments(text))))
    macros.update(_collect_macros(document))
    document = _remove_macro_definitions(document)
    document = _expand_macros(document, macros)

    abstract, document = _extract_abstract(document)
    begin = re.search(r"\\begin\s*\{document\}", document)
    end = re.search(r"\\end\s*\{document\}", document)
    body = document[begin.end() if begin else 0 : end.start() if end else len(document)]
    body = _drop_front_and_back_matter(body)

    paragraphs: list[Paragraph] = []
    if abstract:
        parts = [p for p in (_latex_to_text(a, {}) for a in _split_paragraphs(abstract)) if p]
        for k, text in enumerate(parts, 1):
            paragraphs.append(Paragraph("Abstract" if len(parts) == 1 else f"Abstract, para {k}", text))
    paragraphs.extend(_structure(body))
    return paragraphs


# Archive reading


def _read_archive(data: bytes) -> dict[str, str]:
    head = data[:8]
    if head.startswith(b"%PDF"):
        raise ParseError("pdf-only submission")
    if head.startswith(b"\x1f\x8b"):
        peek = zlib.decompressobj(wbits=31).decompress(data, 1024)
        if peek.startswith(b"%PDF"):
            raise ParseError("pdf-only submission")
        if _looks_like_tar(peek):
            return _read_tar(data, "r|gz")
        if peek.startswith(b"%!PS"):
            raise ParseError("postscript-only submission")
        d = zlib.decompressobj(wbits=31)
        raw = d.decompress(data, MAX_MEMBER_BYTES + 1)
        if len(raw) > MAX_MEMBER_BYTES:
            raise ParseError("parse error: single file larger than 20 MB")
        return {"main.tex": _decode(raw)}
    if _looks_like_tar(data[:1024]):
        return _read_tar(data, "r|")
    if head.startswith(b"%!PS"):
        raise ParseError("postscript-only submission")
    if len(data) > MAX_MEMBER_BYTES:
        raise ParseError("parse error: single file larger than 20 MB")
    return {"main.tex": _decode(data)}


def _looks_like_tar(head: bytes) -> bool:
    if len(head) >= 262 and head[257:262] == b"ustar":
        return True
    if len(head) < 512:
        return False
    try:
        tarfile.TarInfo.frombuf(head[:512], "utf-8", "surrogateescape")
        return True
    except tarfile.HeaderError:
        return False


def _read_tar(data: bytes, mode: str) -> dict[str, str]:
    files: dict[str, str] = {}
    total_read = 0
    walked = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tar:
            for member in tar:
                walked += max(member.size, 0)
                if walked > MAX_ARCHIVE_BYTES:
                    raise ParseError("parse error: archive larger than 2 GB unpacked")
                if not member.isfile():
                    continue
                name = member.name
                norm = posixpath.normpath(name)
                if name.startswith("/") or norm == ".." or norm.startswith("../"):
                    continue
                ext = posixpath.splitext(norm)[1].lower()
                if ext not in (".tex", ".sty", "") or member.size > MAX_MEMBER_BYTES:
                    continue
                total_read += member.size
                if total_read > MAX_TOTAL_BYTES:
                    raise ParseError("parse error: text files larger than 100 MB in total")
                f = tar.extractfile(member)
                if f is None:
                    continue
                raw = f.read(MAX_MEMBER_BYTES + 1)
                if ext == "" and b"\\" not in raw[:65536]:
                    continue  # extensionless binaries and READMEs
                files[norm] = _decode(raw)
    except (tarfile.TarError, EOFError, zlib.error, OSError) as e:
        if files:
            return files
        raise ParseError(f"parse error: bad archive ({type(e).__name__})") from e
    if not files:
        raise ParseError("parse error: no .tex files in archive")
    return files


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _pick_main(files: dict[str, str]) -> str:
    candidates = [n for n, t in files.items() if re.search(r"\\document(class|style)\b", t)]
    if not candidates:
        raise ParseError("parse error: no \\documentclass")

    def rank(name: str) -> tuple:
        text = files[name]
        stem = posixpath.splitext(posixpath.basename(name))[0].lower()
        return (
            bool(re.search(r"\\begin\s*\{document\}", text)),
            stem in ("ms", "main", "paper", "manuscript", "article"),
            len(text),
        )

    return max(candidates, key=rank)


# Comments, conditionals, \input


def _comment_start(line: str) -> int | None:
    for m in re.finditer("%", line):
        k, slashes = m.start() - 1, 0
        while k >= 0 and line[k] == "\\":
            slashes += 1
            k -= 1
        if slashes % 2 == 0:
            return m.start()
    return None


def _strip_comments(text: str) -> str:
    """Drop % comments as TeX does: the comment eats its newline and the next line's indent."""
    out: list[str] = []
    joined = False
    for line in text.split("\n"):
        if joined:
            if not line.strip():
                out.append("\n\n")
                joined = False
                continue
            line = line.lstrip()
        i = _comment_start(line)
        if i is None:
            out.append(line + "\n")
            joined = False
        else:
            out.append(line[:i])
            joined = True
    return "".join(out)


_IF_TOKEN = re.compile(r"\\(if[A-Za-z@]*|fi|else)(?![A-Za-z@])")


def _strip_conditionals(text: str) -> str:
    r"""Remove \iffalse...\fi (keeping an \else branch) and comment environments."""
    text = re.sub(r"\\begin\s*\{comment\}.*?\\end\s*\{comment\}", "", text, flags=re.S)
    out: list[str] = []
    i = 0
    start = re.compile(r"\\iffalse(?![A-Za-z@])")
    while True:
        m = start.search(text, i)
        if not m:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i : m.start()])
        depth, j, keep_from = 1, m.end(), None
        while depth:
            t = _IF_TOKEN.search(text, j)
            if not t:
                return "".join(out)  # unterminated: everything after \iffalse is dead
            name = t.group(1)
            if name == "fi":
                depth -= 1
            elif name == "else":
                if depth == 1:
                    keep_from = t.end()
            elif name != "ifthenelse":
                depth += 1
            j = t.end()
            if depth == 0 and keep_from is not None:
                out.append(text[keep_from : t.start()])
        i = j


_INPUT = re.compile(r"\\(?:input|include|subfile)(?![A-Za-z@])\s*(?:\{([^}]*)\}|([^\s{}\\%]+))")


def _inline_inputs(text: str, here: str, files: dict[str, str], depth: int, seen: set[str]) -> str:
    def resolve(m: re.Match) -> str:
        name = (m.group(1) or m.group(2) or "").strip().strip('"')
        if depth >= 10 or not name:
            return ""
        for cand in (name, name + ".tex"):
            for path in (posixpath.normpath(posixpath.join(here, cand)), posixpath.normpath(cand)):
                if path in files and path not in seen:
                    return "\n" + _inline_inputs(files[path], here, files, depth + 1, seen | {path}) + "\n"
        return ""

    return _INPUT.sub(resolve, text)


# Macros

_DEF_HEAD = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand)\*?\s*"
    r"(?:\{\s*\\([A-Za-z@]+)\s*\}|\\([A-Za-z@]+))"
)
_TEXDEF_HEAD = re.compile(r"\\(?:def|gdef|edef|xdef)\s*\\([A-Za-z@]+)")

# Common astronomy macros, as text outside math and as LaTeX inside it.
TEXT_MACROS = {
    "msun": "M☉", "Msun": "M☉", "rsun": "R☉", "Rsun": "R☉", "lsun": "L☉", "Lsun": "L☉",
    "mearth": "M⊕", "Mearth": "M⊕", "rearth": "R⊕", "Rearth": "R⊕",
    "mjup": "M_Jup", "Mjup": "M_Jup", "rjup": "R_Jup", "Rjup": "R_Jup",
    "kms": "km s⁻¹", "etal": "et al.", "ie": "i.e.", "eg": "e.g.",
    "sim": "∼", "lesssim": "≲", "gtrsim": "≳", "lapprox": "≲", "gapprox": "≳",
    "micron": "μm", "farcs": ".″", "farcm": ".′", "arcsec": "″", "arcmin": "′",
    "arcsecond": "″", "degr": "°", "sun": "☉", "earth": "⊕", "teff": "T_eff", "Teff": "T_eff",
    "logg": "log g", "feh": "[Fe/H]", "nodata": "...", "phn": "", "phd": "", "nl": " ",
    "S": "§", "xspace": " ", "la": "≲", "ga": "≳",
}
MATH_MACROS = {
    "msun": r"M_\odot", "Msun": r"M_\odot", "rsun": r"R_\odot", "Rsun": r"R_\odot",
    "lsun": r"L_\odot", "Lsun": r"L_\odot", "mearth": r"M_\oplus", "Mearth": r"M_\oplus",
    "rearth": r"R_\oplus", "Rearth": r"R_\oplus", "mjup": r"M_{\rm Jup}", "Mjup": r"M_{\rm Jup}",
    "rjup": r"R_{\rm Jup}", "Rjup": r"R_{\rm Jup}", "kms": r"\,{\rm km\,s^{-1}}",
    "micron": r"\mu{\rm m}", "farcs": r".''", "arcsec": r"''", "arcmin": r"'", "degr": r"^\circ",
    "sun": r"\odot", "earth": r"\oplus", "teff": r"T_{\rm eff}", "Teff": r"T_{\rm eff}",
    "etal": r"\text{et al.}", "ie": r"\text{i.e.}", "eg": r"\text{e.g.}",
    "la": r"\lesssim", "ga": r"\gtrsim",
}


def _brace_end(s: str, i: int) -> int:
    """s[i] is '{': index just past its matching '}', or len(s)."""
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return len(s)


def _bracket_end(s: str, i: int) -> int:
    """s[i] is '[': index just past the matching ']' at brace depth 0."""
    depth = 0
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "]" and depth <= 0:
            return j + 1
        j += 1
    return len(s)


def _skip_space(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _parse_args(s: str, i: int, n_req: int, max_opt: int = 2) -> tuple[list[str], list[str], int]:
    """Optional [..] and required {..} arguments starting at s[i]: (optional, required, end)."""
    opts: list[str] = []
    reqs: list[str] = []
    j = i
    if j < len(s) and s[j] == "*":
        j += 1
    while len(opts) < max_opt:
        k = _skip_space(s, j)
        if k < len(s) and s[k] == "[":
            e = _bracket_end(s, k)
            opts.append(s[k + 1 : e - 1])
            j = e
        else:
            break
    while len(reqs) < n_req:
        k = _skip_space(s, j)
        if k < len(s) and s[k] == "{":
            e = _brace_end(s, k)
            reqs.append(s[k + 1 : e - 1])
            j = e
        elif k < len(s) and s[k] == "\\":  # \foo\bar: a single-token argument
            m = re.match(r"\\([A-Za-z@]+|.)", s[k:])
            reqs.append(m.group(0) if m else "")
            j = k + (len(m.group(0)) if m else 1)
        else:
            break
        while len(opts) < max_opt:  # optional args between required ones (\caption[..]{..} etc.)
            k = _skip_space(s, j)
            if len(reqs) < n_req and k < len(s) and s[k] == "[":
                e = _bracket_end(s, k)
                opts.append(s[k + 1 : e - 1])
                j = e
            else:
                break
    return opts, reqs, j


def _sub_command(text: str, names: str, n_req: int, repl, max_opt: int = 2) -> str:
    """Replace \\name[..]{..}... (names is a regex alternation) with repl(name, opts, args)."""
    pat = re.compile(rf"\\({names})(?![A-Za-z@])")
    out: list[str] = []
    i = 0
    while True:
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i : m.start()])
        opts, args, end = _parse_args(text, m.end(), n_req, max_opt)
        out.append(repl(m.group(1), opts, args))
        i = end


def _collect_macros(text: str) -> dict[str, str]:
    """Zero-argument macro definitions: \\newcommand{\\foo}{...}, \\def\\foo{...}."""
    macros: dict[str, str] = {}
    for m in _DEF_HEAD.finditer(text):
        name = m.group(1) or m.group(2)
        opts, args, _ = _parse_args(text, m.end(), 1, max_opt=2)
        if opts and opts[0].strip() not in ("", "0"):
            continue
        if args and "#" not in args[0]:
            macros[name] = args[0]
    for m in _TEXDEF_HEAD.finditer(text):
        k = _skip_space(text, m.end())
        if k < len(text) and text[k] == "{":
            body = text[k + 1 : _brace_end(text, k) - 1]
            if "#" not in body:
                macros[m.group(1)] = body
    return macros


def _remove_macro_definitions(text: str) -> str:
    out: list[str] = []
    i = 0
    heads = re.compile(
        r"\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand|DeclareMathOperator|"
        r"newenvironment|renewenvironment|def|gdef|edef|xdef|let|newif|newlength|setlength|"
        r"newcounter|setcounter|usepackage|RequirePackage)(?![A-Za-z@])"
    )
    while True:
        m = heads.search(text, i)
        if not m:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i : m.start()])
        word = m.group(0)[1:]
        j = m.end()
        if word in ("def", "gdef", "edef", "xdef"):
            j = _skip_space(text, j)
            mm = re.match(r"\\[A-Za-z@]+|\\.", text[j:])
            j += len(mm.group(0)) if mm else 0
            while j < len(text) and text[j] not in "{\n":  # parameter text such as #1#2
                j += 1
            if j < len(text) and text[j] == "{":
                j = _brace_end(text, j)
        elif word == "let":
            mm = re.match(r"\s*\\[A-Za-z@]+\s*=?\s*(\\[A-Za-z@]+|\\.|.)", text[j:])
            j += len(mm.group(0)) if mm else 0
        elif word in ("newif",):
            mm = re.match(r"\s*\\[A-Za-z@]+", text[j:])
            j += len(mm.group(0)) if mm else 0
        else:
            n = {"newenvironment": 3, "renewenvironment": 3, "setlength": 2, "setcounter": 2,
                 "DeclareMathOperator": 2}.get(word, 1 if word in ("newlength", "newcounter",
                                                                   "usepackage", "RequirePackage") else 2)
            _, _, j = _parse_args(text, j, n, max_opt=2)
        i = j


def _expand_macros(text: str, macros: dict[str, str]) -> str:
    if not macros:
        return text
    names = sorted((n for n, body in macros.items() if f"\\{n}" not in body), key=len, reverse=True)
    if not names:
        return text
    pat = re.compile(r"\\(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z@])(?:\{\})?")
    for _ in range(3):
        new = pat.sub(lambda m: macros[m.group(1)], text)
        if new == text:
            break
        text = new
    return text


# Front and back matter

_DROP_ENVS = (
    "thebibliography|references|acknowledgements|acknowledgments|acknowledgement|acknowledgment|"
    "keywords|contribution|authorcontributions"
)
_DROP_COMMANDS_1 = (
    "title|author|affiliation|affil|altaffiliation|altaffilmark|institute|email|correspondingauthor|"
    "keywords|orcid|orcidlink|thanks|date|shorttitle|shortauthors|received|accepted|revised|published|"
    "submitjournal|software|facilities|facility|bibliography|bibliographystyle|pubyear|volume|"
    "pagerange|titlerunning|authorrunning|offprints|mail|collaboration|nocollaboration|"
    "AuthorCollaborationLimit|journal|subjectheadings|slugcomment|journalinfo|submitted|doi|"
    "nocite|vspace|hspace|addtocounter|pagestyle|thispagestyle|bibpunct|graphicspath"
)
_DROP_COMMANDS_2 = "altaffiltext|collaboration"
_DROP_COMMANDS_0 = (
    "maketitle|tableofcontents|newpage|clearpage|linenumbers|nolinenumbers|noindent|centering|"
    "raggedright|bigskip|medskip|smallskip|footnotemark|tableofcontents|listoffigures|onecolumn|"
    "twocolumn|FloatBarrier|allauthors"
)


def _extract_abstract(document: str) -> tuple[str, str]:
    m = re.search(r"\\begin\s*\{abstract\}(.*?)\\end\s*\{abstract\}", document, re.S)
    if m:
        return m.group(1), document[: m.start()] + document[m.end() :]
    m = re.search(r"\\abstract(?![A-Za-z@])\s*\{", document)
    if m:
        start = m.end() - 1
        end = _brace_end(document, start)
        inner = document[start + 1 : end - 1]
        inner = _sub_command(
            inner, "context|aims|methods|results|conclusions", 1,
            lambda name, o, a: f" {name.capitalize()}: {a[0] if a else ''} ",
        )
        return inner, document[: m.start()] + document[end:]
    return "", document


def _drop_front_and_back_matter(body: str) -> str:
    body = re.sub(rf"\\begin\s*\{{({_DROP_ENVS})\*?\}}.*?\\end\s*\{{\1\*?\}}", "", body, flags=re.S | re.I)
    # AASTeX's \acknowledgments command runs to the next section or the appendix.
    m = re.search(r"\\(?:acknowledgments|acknowledgements)(?![A-Za-z@])", body)
    if m:
        rest = re.search(r"\\(?:section|appendix)(?![A-Za-z@])|\\begin\s*\{appendix\}", body[m.end() :])
        body = body[: m.start()] + (body[m.end() + rest.start() :] if rest else "")
    body = _sub_command(body, _DROP_COMMANDS_2, 2, lambda *a: "")
    body = _sub_command(body, _DROP_COMMANDS_1, 1, lambda *a: "")
    body = re.sub(rf"\\({_DROP_COMMANDS_0})(?![A-Za-z@])\*?", "", body)
    return body


# Structure: sections, floats and paragraphs

_STRUCT = re.compile(
    r"\\(?P<sec>section|subsection|subsubsection)(?![A-Za-z@])"
    r"|\\(?P<appendix>appendix)(?![A-Za-z@])|\\begin\s*\{(?P<appenv>appendix|appendices)\}"
    r"|\\end\s*\{(?:appendix|appendices)\}"
    r"|\\begin\s*\{(?P<float>(?:figure|table|deluxetable|sidewaystable|sidewaysfigure|wrapfigure|"
    r"wraptable|SCfigure|longtable|supertabular)\*?)\}"
    r"|\\(?P<para>paragraph|subparagraph)(?![A-Za-z@])"
)
_MATH_ENVS = (
    r"equation|align|alignat|eqnarray|gather|multline|displaymath|math|flalign|split|dmath"
)
_DISPLAY_MATH = re.compile(rf"\\begin\s*\{{({_MATH_ENVS})(\*?)\}}(.*?)\\end\s*\{{\1\2\}}", re.S)


@dataclass
class _Section:
    level: int
    number: str
    title: str


@dataclass
class _State:
    counters: list[int] = field(default_factory=lambda: [0, 0, 0])
    appendix: bool = False
    current: _Section | None = None
    unit: int = 0  # bumps at every heading, so paragraph numbers restart
    skip_level: int | None = None
    figures: int = 0
    tables: int = 0
    labels: dict[str, tuple[str, str]] = field(default_factory=dict)  # label -> (kind, number)


def _structure(body: str) -> list[Paragraph]:
    st = _State()
    # (unit, unit name, latex, caption label or None); converted once every \label is known
    raw: list[tuple[int, str, str, str | None]] = []
    pending_prefix = ""
    pos = 0

    def emit_text(chunk: str) -> None:
        nonlocal pending_prefix
        if st.skip_level is not None:
            return
        for lab in _labels_outside_math(chunk):
            if st.current and st.current.number:
                st.labels.setdefault(lab, ("section", st.current.number))
        for para in _split_paragraphs(chunk):
            if pending_prefix:
                para = pending_prefix + para
                pending_prefix = ""
            if not _has_words(para):
                if raw and raw[-1][3] is None and _strip_math(para).strip() == "" and para.strip():
                    unit, name, prev, _ = raw[-1]
                    raw[-1] = (unit, name, prev + "\n" + para, None)
                continue
            raw.append((st.unit, _unit_name(st), para, None))

    while True:
        m = _STRUCT.search(body, pos)
        if not m:
            emit_text(body[pos:])
            break
        emit_text(body[pos : m.start()])
        if m.group("sec"):
            level = ("section", "subsection", "subsubsection").index(m.group("sec"))
            opts, args, end = _parse_args(body, m.end(), 1, max_opt=1)
            starred = body[m.end() : m.end() + 1] == "*"
            title = _latex_to_text(args[0] if args else (opts[0] if opts else ""), {}).strip()
            number = ""
            if not starred:
                st.counters[level] += 1
                for k in range(level + 1, 3):
                    st.counters[k] = 0
                parts = [str(c) for c in st.counters[: level + 1]]
                if st.appendix:
                    parts[0] = _letters(st.counters[0])
                number = ".".join(parts)
            if st.skip_level is not None and level <= st.skip_level:
                st.skip_level = None
            if re.match(r"acknowledg", title, re.I):
                st.skip_level = level
            st.current = _Section(level, number, title)
            st.unit += 1
            pending_prefix = ""
            pos = end
        elif m.group("appendix") or m.group("appenv"):
            st.appendix = True
            st.counters = [0, 0, 0]
            st.skip_level = None
            pos = m.end()
        elif m.group("float"):
            env = m.group("float")
            end_m = re.compile(rf"\\end\s*\{{{re.escape(env)}\}}").search(body, m.end())
            inner = body[m.end() : end_m.start() if end_m else len(body)]
            pos = end_m.end() if end_m else len(body)
            if st.skip_level is not None:
                continue
            captions: list[str] = []
            _sub_command(inner, "caption|tablecaption", 1,
                         lambda n, o, a: captions.append(a[0]) or "" if a else "")
            if not captions:
                continue  # LaTeX numbers floats by \caption, so a caption-less float takes no number
            if "table" in env:
                st.tables += 1
                kind, number = "table", str(st.tables)
            else:
                st.figures += 1
                kind, number = "figure", str(st.figures)
            for lab in re.findall(r"\\label\s*\{([^}]*)\}", inner):
                st.labels.setdefault(lab.strip(), (kind, number))
            raw.append((st.unit, "", " ".join(captions), f"{kind.capitalize()} {number} caption"))
        elif m.group("para"):
            _, args, end = _parse_args(body, m.end(), 1, max_opt=1)
            title = (args[0] if args else "").strip()
            pending_prefix = f"{title.rstrip('.')}. " if title else ""
            pos = end
        else:  # \end{appendix}
            pos = m.end()

    out: list[Paragraph] = []
    counts: Counter = Counter()
    for unit, name, latex, caption in raw:
        text = _latex_to_text(latex, st.labels)
        if not text or (caption is None and len(text) < MIN_PARAGRAPH_CHARS):
            continue
        if caption:
            out.append(Paragraph(caption, text))
        else:
            counts[unit] += 1
            out.append(Paragraph(f"{name}, para {counts[unit]}", text))
    return out


def _letters(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s or "A"


def _unit_name(st: _State) -> str:
    if st.current is None:
        return "Main text"
    return " ".join(x for x in (st.current.number, st.current.title) if x) or "Untitled section"


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n[ \t]*\n|\\par(?![A-Za-z@])", text)
    return [p.strip() for p in parts if p.strip()]


def _has_words(latex: str) -> bool:
    return bool(re.search(r"[A-Za-z]{3,}", _strip_math(re.sub(r"\\[A-Za-z@]+", "", latex))))


def _strip_math(latex: str) -> str:
    return "".join(seg for is_math, seg in _split_math(latex) if not is_math)


def _labels_outside_math(chunk: str) -> list[str]:
    labels: list[str] = []
    for is_math, seg in _split_math(chunk):
        if not is_math:
            labels += [x.strip() for x in re.findall(r"\\label\s*\{([^}]*)\}", seg)]
    return labels


# Math-aware conversion of one paragraph to text


def _find_unescaped(s: str, needle: str, start: int) -> int:
    i = start
    while True:
        i = s.find(needle, i)
        if i < 0:
            return -1
        k, slashes = i - 1, 0
        while k >= 0 and s[k] == "\\":
            slashes += 1
            k -= 1
        if slashes % 2 == 0:
            return i
        i += 1


def _split_math(s: str) -> list[tuple[bool, str]]:
    """Split into (is_math, segment); math segments keep their delimiters."""
    out: list[tuple[bool, str]] = []
    i = 0
    text_start = 0
    env_begin = re.compile(rf"\\begin\s*\{{({_MATH_ENVS})(\*?)\}}")
    while i < len(s):
        c = s[i]
        end = -1
        if c == "\\":
            nxt = s[i + 1 : i + 2]
            if nxt in ("[", "("):
                close = "\\]" if nxt == "[" else "\\)"
                j = s.find(close, i + 2)
                end = len(s) if j < 0 else j + 2
            else:
                m = env_begin.match(s, i)
                if m:
                    em = re.compile(rf"\\end\s*\{{{m.group(1)}{re.escape(m.group(2))}\}}").search(s, m.end())
                    end = len(s) if not em else em.end()
                else:
                    i += 2
                    continue
        elif c == "$":
            if s.startswith("$$", i):
                j = _find_unescaped(s, "$$", i + 2)
                end = len(s) if j < 0 else j + 2
            else:
                j = _find_unescaped(s, "$", i + 1)
                end = len(s) if j < 0 else j + 1
        else:
            i += 1
            continue
        if text_start < i:
            out.append((False, s[text_start:i]))
        out.append((True, s[i:end]))
        i = text_start = end
    if text_start < len(s):
        out.append((False, s[text_start:]))
    return out


_CITE = (
    r"[Cc]ite[a-zA-Z]*|[Cc]itep|[Cc]itet|citealp|citealt|citeauthor|citeyear|citeyearpar|citenum|"
    r"parencite|textcite|autocite|footcite|Citeauthor|Citealt|Citealp"
)
_REF = r"ref|eqref|autoref|Autoref|cref|Cref|pageref|nameref|vref|Vref|subref"


def _math_to_text(seg: str) -> str:
    """Normalize a math segment to $...$ (inline) or $$...$$ (display), LaTeX kept verbatim."""
    seg = re.sub(r"\\(label|tag)\s*\{[^}]*\}|\\(nonumber|notag)(?![A-Za-z@])", "", seg)
    for name, value in MATH_MACROS.items():
        seg = re.sub(rf"\\{name}(?![A-Za-z@])(?:\{{\}})?", lambda _m, v=value: v, seg)
    if seg.startswith("$$"):
        inner, display = seg[2:-2] if seg.endswith("$$") else seg[2:], True
    elif seg.startswith("$"):
        inner, display = seg[1:-1] if seg.endswith("$") and len(seg) > 1 else seg[1:], False
    elif seg.startswith("\\["):
        inner, display = seg[2:-2], True
    elif seg.startswith("\\("):
        inner, display = seg[2:-2], False
    else:
        m = _DISPLAY_MATH.match(seg)
        inner = m.group(3) if m else seg
        if m and m.group(1) in ("align", "alignat", "eqnarray", "gather", "multline", "flalign", "split"):
            inner = f"\\begin{{{m.group(1)}{m.group(2)}}}{inner}\\end{{{m.group(1)}{m.group(2)}}}"
        display = True
    inner = " ".join(inner.split())
    return f"$${inner}$$" if display else f"${inner}$"


def _latex_to_text(latex: str, labels: dict[str, tuple[str, str]]) -> str:
    def ref(name: str, _opts, args) -> str:
        arg = (args[0] if args else "").strip()
        # Only the cleveref family takes comma lists; a plain \ref label may itself contain commas.
        keys = [k.strip() for k in arg.split(",") if k.strip()] if name.lower() in ("cref", "vref") else [arg]
        rendered = []
        for k in keys:
            kind, number = labels.get(k, ("", "#"))
            if name.lower() in ("autoref", "cref", "vref") and kind:
                rendered.append(f"{kind.capitalize()} {number}")
            else:
                rendered.append(number)
        joined = ", ".join(rendered) or "#"
        return f"({joined})" if name == "eqref" else joined

    s = _sub_command(latex, _CITE, 1, lambda *a: "[ref]")
    s = _sub_command(s, _REF, 1, ref, max_opt=0)
    s = _sub_command(s, "footnote|footnotetext", 1,
                     lambda n, o, a: f" (footnote: {a[0].strip()})" if a else "")
    s = _sub_command(s, "href", 2, lambda n, o, a: a[1] if len(a) > 1 else "")
    s = _sub_command(s, "url", 1, lambda n, o, a: a[0] if a else "", max_opt=0)
    s = _sub_command(s, "ion", 2, lambda n, o, a: " ".join(a))
    s = _sub_command(s, "label", 1, lambda *a: "", max_opt=0)

    pieces: list[str] = []
    text_parts: list[str] = []
    def math_piece(seg: str) -> str:
        seg = _sub_command(seg, "ensuremath", 1, lambda n, o, a: a[0] if a else "", max_opt=0)
        pieces.append(_math_to_text(seg))
        return f"\ue000{len(pieces) - 1}\ue001"

    for is_math, seg in _split_math(s):
        if is_math:
            text_parts.append(math_piece(seg))
        else:
            seg = _sub_command(seg, "ensuremath", 1,
                               lambda n, o, a: math_piece(f"${a[0]}$") if a else "", max_opt=0)
            seg = re.sub(r"\\\\(\[[^\]]*\])?|\\newline(?![A-Za-z@])", " ", seg)
            for name, value in TEXT_MACROS.items():
                seg = re.sub(rf"\\{name}(?![A-Za-z@])(?:\{{\}})?", lambda _m, v=value: v, seg)
            text_parts.append(seg)
    plain = _to_plain("".join(text_parts))
    plain = plain.replace("\xa0", " ")
    plain = " ".join(plain.split())
    plain = re.sub(r"\s+([,.;:)\]])", r"\1", plain)
    plain = re.sub(r"([(\[])\s+", r"\1", plain)
    return re.sub("\ue000(\\d+)\ue001", lambda m: pieces[int(m.group(1))], plain).strip()


_CONVERTER = None


def _to_plain(latex: str) -> str:
    global _CONVERTER
    from pylatexenc.latex2text import LatexNodes2Text

    if _CONVERTER is None:
        _CONVERTER = LatexNodes2Text(math_mode="verbatim", strict_latex_spaces=False)
    try:
        return _CONVERTER.latex_to_text(latex)
    except Exception:
        # pylatexenc chokes on some inputs; fall back to stripping commands and keeping arguments
        s = re.sub(r"\\begin\s*\{[^}]*\}|\\end\s*\{[^}]*\}", " ", latex)
        s = re.sub(r"\\[A-Za-z@]+\*?", "", s)
        return s.replace("{", "").replace("}", "").replace("~", " ")


# ---------------------------------------------------------------------------
# HTML (arXiv's LaTeXML conversion)

_HTML_SECTIONS = ("ltx_section", "ltx_subsection", "ltx_subsubsection", "ltx_appendix")
_HTML_SKIP = (
    "ltx_bibliography", "ltx_acknowledgements", "ltx_authors", "ltx_page_footer",
    "ltx_page_navbar", "ltx_TOC", "ltx_dates", "ltx_keywords", "ltx_classification",
)


def parse_arxiv_html(html: str) -> list[Paragraph]:
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html, "lxml")
    root = soup.find("article", class_="ltx_document") or soup.find(class_="ltx_page_content") or soup.body
    if root is None:
        raise ParseError("parse error: no document body in HTML")

    def classes(t: Tag) -> list[str]:
        return t.get("class") or []

    def render(el: Tag) -> str:
        el = BeautifulSoup(str(el), "lxml")
        for note in el.find_all(class_="ltx_note"):
            if "ltx_role_footnote" in classes(note):
                content = note.find(class_="ltx_note_content")
                if content:
                    for mark in content.find_all(class_=("ltx_note_mark", "ltx_tag")):
                        mark.decompose()
                    note.replace_with(NavigableString(f" (footnote: {content.get_text(' ', strip=True)})"))
                    continue
            note.decompose()
        for tag in el.find_all(class_=("ltx_tag_equation", "ltx_ERROR")):
            tag.decompose()
        for math in el.find_all("math"):
            alt = " ".join((math.get("alttext") or math.get_text(" ")).split())
            display = math.get("display") == "block"
            math.replace_with(NavigableString(f"$${alt}$$" if display else f"${alt}$"))
        for cite in el.find_all("cite"):
            cite.replace_with(NavigableString("[ref]"))
        for block in el.find_all(["p", "div", "table", "tr", "li", "br"]):
            block.append(NavigableString(" "))
        text = " ".join(el.get_text("").split())
        text = re.sub(r"\s+([,.;:)\]])", r"\1", text)
        return re.sub(r"([(\[])\s+", r"\1", text).strip()

    def title_of(sec: Tag) -> tuple[str, str]:
        h = sec.find(re.compile(r"^h[1-6]$"), class_="ltx_title", recursive=False)
        if h is None:
            h = sec.find(class_="ltx_title")
        if h is None:
            return "", ""
        h = BeautifulSoup(str(h), "lxml")
        tag = h.find(class_="ltx_tag")
        number = ""
        if tag is not None:
            number = tag.get_text(" ", strip=True)
            number = re.sub(r"^(Appendix|Section)\s+", "", number).rstrip(".:) ").strip()
            tag.decompose()
        for math in h.find_all("math"):
            math.replace_with(NavigableString(math.get("alttext") or ""))
        return number, " ".join(h.get_text(" ").split())

    out: list[Paragraph] = []
    stack: list[str] = []
    counts: dict[str, int] = {}
    figures = {"figure": 0, "table": 0}

    def add_para(text: str, prefix: str = "") -> None:
        if len(text) < MIN_PARAGRAPH_CHARS:
            return
        name = stack[-1] if stack else "Main text"
        counts[name] = counts.get(name, 0) + 1
        out.append(Paragraph(f"{name}, para {counts[name]}", prefix + text))

    def walk(node: Tag, prefix_holder: list[str]) -> None:
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            cls = classes(child)
            if any(c in cls for c in _HTML_SKIP) or child.name in ("nav", "header", "footer", "button"):
                continue
            if "ltx_abstract" in cls:
                paras = child.find_all(class_="ltx_p") or [child]
                texts = [render(p) for p in paras]
                texts = [t for t in texts if t and t.lower() != "abstract"]
                for k, t in enumerate(texts, 1):
                    out.append(Paragraph("Abstract" if len(texts) == 1 else f"Abstract, para {k}", t))
                continue
            if child.name == "section" and any(c in cls for c in _HTML_SECTIONS):
                number, title = title_of(child)
                if re.match(r"acknowledg", title, re.I):
                    continue
                stack.append(" ".join(x for x in (number, title) if x) or "Untitled section")
                walk(child, [""])
                stack.pop()
                continue
            if child.name == "section" and ("ltx_paragraph" in cls or "ltx_subparagraph" in cls):
                _, title = title_of(child)
                walk(child, [f"{title.rstrip('.')}. " if title else ""])
                continue
            if child.name == "figure" and ("ltx_figure" in cls or "ltx_table" in cls):
                kind = "table" if "ltx_table" in cls else "figure"
                cap = child.find("figcaption")
                if cap is None:
                    continue
                cap = BeautifulSoup(str(cap), "lxml")
                tag = cap.find(class_="ltx_tag")
                figures[kind] += 1
                number = str(figures[kind])
                if tag is not None:
                    tag_text = re.sub(r"^(Figure|Fig\.|Table)\s*", "", tag.get_text(" ", strip=True), flags=re.I)
                    m = re.match(r"[A-Z]?\.?\d+(?:\.\d+)?", tag_text)
                    number = m.group(0) if m else number
                    tag.decompose()
                text = render(cap.body or cap)
                if text:
                    out.append(Paragraph(f"{kind.capitalize()} {number} caption", text))
                continue
            if "ltx_para" in cls:
                inner_figures = child.find_all("figure", recursive=True)
                for f in inner_figures:
                    f.extract()
                text = render(child)
                add_para(text, prefix_holder[0])
                prefix_holder[0] = ""
                if inner_figures:
                    holder = BeautifulSoup("<div></div>", "lxml").div
                    for f in inner_figures:
                        holder.append(f)
                    walk(holder, prefix_holder)
                continue
            walk(child, prefix_holder)

    walk(root, [""])
    return out


# ---------------------------------------------------------------------------
# PDF

_NUMBERED_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.(?:\d+(?:\.\d+)*\.?)?)\s+[A-Z][^.!?]{0,100}$")
_CAPTION_START = re.compile(r"^(Figure|Fig\.|FIGURE|Table|TABLE)\s+(\d+|[A-Z]\d+)[.:]?\s")
# Books number figures by chapter: "Figure 3.12".
_BOOK_CAPTION_START = re.compile(r"^(Figure|Fig\.|FIGURE|Table|TABLE)\s+(\d+(?:[.-]\d+)*|[A-Z][\d.]*\d)[.:]?\s")
_PDF_VERSION = re.compile(r"^[0-9a-f]{12}(?::p(\d+)-(\d+))?(:ocr)?$")
OCR_DPI = 300
OCR_MIN_CHARS = 50  # a page with less text than this and an image on it is a scan
OCR_WORKERS = 8  # pages read by OCR at once, each in its own process: Tesseract takes seconds a page


def read_pdf(data: bytes, pages: tuple[int, int] | None = None, *, source: str = UPLOAD, book: bool = True,
             ocr: bool = True) -> FullText:
    """A PDF from page pages[0] to pages[1] (the file's own page numbers, from 1), or all of it.
    book: it may be a book, as an upload may (see parse_pdf). ocr: read scanned pages by OCR, if
    it is set up. The version is the file's checksum, then the pages read when they were chosen,
    then whether any page was read by OCR: "3f9a1c2b7d4e", "3f9a1c2b7d4e:p120-180:ocr". Raises
    ParseError when there is too little text, such as a scan that can't be read by OCR."""
    paragraphs, ocr_pages = read_pdf_paragraphs(data, pages, book, tessdata() if ocr else None, published=True)
    n, chars = len(paragraphs), sum(len(p.text) for p in paragraphs)
    if n < MIN_PARAGRAPHS or chars < MIN_CHARS:
        raise ParseError(f"too little text ({n} paragraphs, {chars} chars)")
    paragraphs, truncated = _cap(paragraphs, UPLOAD_MAX_PARAGRAPHS if book else MAX_PARAGRAPHS)
    version = hashlib.sha256(data).hexdigest()[:12] + (f":p{pages[0]}-{pages[1]}" if pages else "")
    return FullText(source, version + (":ocr" if ocr_pages else ""), paragraphs, truncated)


def upload_pages(version: str) -> tuple[int, int] | None:
    """The pages a PDF's text was read from, when they were chosen (see read_pdf)."""
    m = _PDF_VERSION.match(version)
    return (int(m.group(1)), int(m.group(2))) if m and m.group(1) else None


def is_ocr(version: str) -> bool:
    """Whether a PDF's text was read, at least in part, by OCR (see read_pdf)."""
    m = _PDF_VERSION.match(version)
    return bool(m and m.group(3))


def parse_pdf(data: bytes, pages: tuple[int, int] | None = None, uploaded: bool = False) -> list[Paragraph]:
    """pages: the first and last page to read, counting the file's first page as 1. uploaded: a PDF
    someone supplied, which may be a book. Its labels then give the page, a reference list is
    skipped rather than ending the text (each chapter can have one), and so is an index."""
    return read_pdf_paragraphs(data, pages, uploaded, published=uploaded)[0]


_ocr_doc = None  # the PDF a process reads pages of by OCR, and where Tesseract's data is
_ocr_tessdata = None


def _ocr_start(data: bytes, tessdata_dir: str) -> None:
    import pymupdf

    global _ocr_doc, _ocr_tessdata
    _ocr_doc, _ocr_tessdata = pymupdf.open(stream=data, filetype="pdf"), tessdata_dir


def _ocr_page(pno: int) -> list[dict]:
    page = _ocr_doc[pno - 1]
    tp = page.get_textpage_ocr(language="eng", dpi=OCR_DPI, full=True, tessdata=_ocr_tessdata)
    return _ocr_paragraphs(_ocr_lines(page.get_text("words", textpage=tp), page.rect.width))


def _ocr_lines(words: list[tuple], width: float) -> list[tuple]:
    """OCR's words as lines in reading order: (text, x0, y0, x1, y1). Words are put in rows by their
    vertical middles, since OCR's own lines overlap and can break a justified line into several, or
    run one on across the gutter between two columns. A page has two columns when almost no word
    crosses some place near its middle; then the rows that span it (a title, an abstract) are read
    first, then the left column, then the right one."""
    if not words:
        return []
    height = sorted(w[3] - w[1] for w in words)[len(words) // 2]
    rows: list[tuple[float, list[tuple]]] = []
    for w in sorted(words, key=lambda w: (w[1] + w[3]) / 2):
        middle = (w[1] + w[3]) / 2
        if rows and middle - rows[-1][0] < 0.4 * height:
            rows[-1][1].append(w)
        else:
            rows.append((middle, [w]))

    def crossing(x: float) -> int:
        return sum(1 for w in words if w[0] < x < w[2])

    gutter = min(range(int(0.35 * width), int(0.65 * width) + 1, 2), key=crossing)
    sides = (sum(1 for w in words if w[2] <= gutter), sum(1 for w in words if w[0] >= gutter))
    columns = crossing(gutter) <= 0.02 * len(words) and min(sides) >= 0.2 * len(words)

    def line(ws: list[tuple], middle: float) -> tuple:
        h = sorted(w[3] - w[1] for w in ws)[len(ws) // 2]
        return " ".join(w[4] for w in ws), ws[0][0], middle - h / 2, max(w[2] for w in ws), middle + h / 2

    wide, left, right = [], [], []
    for middle, row in rows:
        row.sort(key=lambda w: w[0])
        if not columns or any(w[0] < gutter < w[2] for w in row):
            wide.append(line(row, middle))
            continue
        for side, ws in ((left, [w for w in row if w[2] <= gutter]), (right, [w for w in row if w[0] >= gutter])):
            if ws:
                side.append(line(ws, middle))
    if not columns:
        return wide
    top = min(item[2] for item in left + right)
    return [item for item in wide if item[2] < top] + left + right + [item for item in wide if item[2] >= top]


def _ocr_paragraphs(lines: list[tuple]) -> list[dict]:
    """OCR'd lines grouped into paragraphs, as text blocks like those of a PDF's own text, marked
    "ocr". OCR sizes each word differently, so a line's size is the height of its middling word. A
    paragraph starts after a gap wider than the line spacing, at an indented first line (journals
    mark paragraphs by indenting them), or after a jump down the page or across it; it runs on
    into the next column."""
    if not lines:
        return []
    height = sorted(y1 - y0 for _, _, y0, _, y1 in lines)[len(lines) // 2]
    paragraphs: list[list[tuple]] = []
    for line in lines:
        x0, step = line[1], None
        if paragraphs:
            prev = paragraphs[-1][-1]
            step = (line[2] + line[4]) / 2 - (prev[2] + prev[4]) / 2
        if step is None:
            new = True
        elif step <= 0.5 * height:  # the same row, or up to the next column
            new = False
        elif step < 2.2 * height and abs(x0 - prev[1]) < 4 * height:  # the next line of a column
            new = step > 1.6 * height or x0 - prev[1] > 0.6 * height
        else:
            new = True
        if new:
            paragraphs.append([line])
        else:
            paragraphs[-1].append(line)
    return [{"type": 0, "ocr": True,
             "bbox": (min(x0 for _, x0, *_ in p), p[0][2], max(x1 for *_, x1, _ in p), p[-1][4]),
             "lines": [{"spans": [{"text": t, "size": round(y1 - y0, 1), "flags": 0}]} for t, _, y0, _, y1 in p]}
            for p in paragraphs]


def _ocr(data: bytes, pnos: list[int], tessdata_dir: str) -> dict[int, list[dict]]:
    """The text blocks OCR finds on these pages, read several at once in new processes, never in
    this one. (Spawned, not forked: forking a process with threads, such as Tesseract's own, can
    leave the copy stuck.)"""
    workers = min(OCR_WORKERS, os.cpu_count() or 1, len(pnos))
    with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"),
                             initializer=_ocr_start, initargs=(data, tessdata_dir)) as pool:
        return dict(zip(pnos, pool.map(_ocr_page, pnos)))


def read_pdf_paragraphs(data: bytes, pages: tuple[int, int] | None = None, uploaded: bool = False,
                        tessdata_dir: str | None = None, published: bool = False) -> tuple[list[Paragraph], int]:
    """parse_pdf, reading scanned pages by OCR when tessdata_dir is given. published: any PDF but an
    arXiv preprint (an upload, a scan, a publisher's copy), which has running heads that change from
    page to page, and may have words in its figures and equations that look like headings. Returns
    (paragraphs, the number of pages read by OCR)."""
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ParseError(f"parse error: unreadable pdf ({type(e).__name__})") from e

    found: dict[int, tuple[float, list[dict]]] = {}  # page -> (its height, its text blocks)
    scans: list[int] = []
    with doc:
        first, last = pages or (1, doc.page_count)
        if not 1 <= first <= last <= doc.page_count:
            raise ParseError(f"pages {first}-{last} aren't in the PDF, which has {doc.page_count} pages")
        n_pages = last - first + 1
        for pno in range(first, last + 1):
            page = doc[pno - 1]
            text_blocks = [b for b in page.get_text("dict").get("blocks", []) if b.get("type") == 0]
            chars = sum(len(s.get("text", "").strip()) for b in text_blocks for line in b.get("lines", [])
                        for s in line.get("spans", []))
            if tessdata_dir and chars < OCR_MIN_CHARS and page.get_images():
                scans.append(pno)
            found[pno] = (page.rect.height or 1.0, text_blocks)
    if scans:
        for pno, text_blocks in _ocr(data, scans, tessdata_dir).items():
            found[pno] = (found[pno][0], text_blocks)

    blocks: list[dict] = []
    sizes: Counter = Counter()
    for pno, (height, text_blocks) in found.items():
        for b in text_blocks:
            lines: list[str] = []
            max_size = 0.0
            first_size = None
            all_bold = True
            for line in b.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                if not spans:
                    continue
                first_size = first_size or spans[0]["size"]
                lines.append("".join(s["text"] for s in line["spans"]))
                for s in spans:
                    sizes[round(s["size"], 1)] += len(s["text"].strip())
                    max_size = max(max_size, s["size"])
                    all_bold = all_bold and bool(s.get("flags", 0) & 16)
            if not lines:
                continue
            y0, y1 = b["bbox"][1], b["bbox"][3]
            blocks.append({
                "page": pno, "lines": lines, "size": max_size, "first_size": first_size, "bold": all_bold,
                "ocr": b.get("ocr", False),
                "edge": y1 < 0.08 * height or y0 > 0.92 * height,
            })
    if not blocks:
        raise ParseError("parse error: OCR found no text in the scanned pages" if scans else
                         "parse error: no text layer in pdf" if tessdata_dir else
                         "parse error: no text layer in pdf, and OCR isn't set up (see the README)")
    body_size = sizes.most_common(1)[0][0]

    # Running headers and footers: the same edge text on most pages. A journal's or a book's
    # alternate between left and right pages and change with each chapter or section, so in a
    # published copy any edge text on two pages goes, and so does a single edge line with a number.
    def edge_key(b: dict) -> str:
        return re.sub(r"\d+", "#", " ".join(b["lines"]).strip().lower())

    edge_counts = Counter(edge_key(b) for b in blocks if b["edge"])
    least = 2 if published else 0.5 * n_pages
    repeated = {k for k, c in edge_counts.items() if n_pages >= 3 and c >= least}

    def running(b: dict) -> bool:
        if not b["edge"]:
            return False
        if edge_key(b) in repeated:
            return True
        line = " ".join(b["lines"]).strip()
        return published and len(b["lines"]) == 1 and len(line) < 120 and bool(re.search(r"(?:^|\s)\d{1,4}(?:\s|$)", line))

    caption_start = _BOOK_CAPTION_START if uploaded else _CAPTION_START

    out: list[Paragraph] = []
    heading: str | None = None
    skipping = False
    counts: dict[str, int] = {}
    for b in blocks:
        text = _join_lines(b["lines"])
        if not text or re.fullmatch(r"\d{1,4}", text) or running(b):
            continue
        short = len(text) < 120 and len(b["lines"]) <= 3
        # In a published copy, with labelled figures and displayed equations, a heading starts with
        # a capital or its number, a number in small print is an exponent or a prime, and a
        # figure's labels can be a little larger than the text. OCR's sizes are rough.
        bigger = 1.4 if b["ocr"] else 1.15 if published else 1.1
        numbered = bool(_NUMBERED_HEADING.match(text)) and not (published and b["first_size"] < 0.9 * body_size)
        large = b["size"] >= bigger * body_size and not (b["ocr"] and (len(text.split()) > 10 or text.endswith("-")))
        is_heading = short and (
            large
            or numbered
            or (b["bold"] and len(text) < 80 and not text.endswith((".", ":", ",")))
        )
        if published and not (re.search(r"[^\W\d_]{3}", text) and (text[0].isupper() or text[0].isdigit())
                              and sum(c.isalpha() for c in text) >= 0.75 * len(text.replace(" ", ""))):
            is_heading = False  # words in a figure ("N", "ϕ ψ", "timelike"), or a plot's axes read by OCR
        if is_heading and not caption_start.match(text):
            name = re.sub(r"^(\d+(?:\.\d+)*|[A-Z](?:\.\d+)*)\.\s", r"\1 ", text)
            bare = re.sub(r"^[\dA-Z.]+\s+", "", name) if re.match(r"^[\dA-Z.]+\s", name) else name
            references = re.match(r"(references|bibliography|literature cited)\b", bare, re.I) or re.match(
                r"(references|bibliography)$", text, re.I
            )
            if references and not uploaded:
                break
            skipping = bool(references or re.match(r"acknowledg", bare, re.I)
                            or (uploaded and re.match(r"index$", bare, re.I)))
            heading = name
            continue
        if skipping:
            continue
        on_page = f", p. {b['page']}" if uploaded else ""
        cap = caption_start.match(text)
        if cap:
            kind = "Table" if cap.group(1).lower().startswith("tab") else "Figure"
            out.append(Paragraph(f"{kind} {cap.group(2)} caption{on_page}", text))
            continue
        if len(text) < MIN_PARAGRAPH_CHARS:
            continue
        key = heading or f"Page {b['page']}"
        counts[key] = counts.get(key, 0) + 1
        out.append(Paragraph(f"{key}, para {counts[key]}{on_page if heading else ''}", text))
    return out, len(scans)


def _join_lines(lines: list[str]) -> str:
    text = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if text.endswith("-") and line[:1].islower():
            text = text[:-1] + line
        elif text:
            text += " " + line
        else:
            text = line
    return " ".join(text.split())
