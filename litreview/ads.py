"""NASA ADS API client: search, BibTeX export, the resolver, and daily-quota tracking."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, unquote

import httpx

from .arxiv import fcntl, normalize_id, split_version
from .config import DATA_DIR, user_agent
from .library import Paper

API = "https://api.adsabs.harvard.edu/v1"
FIELDS = [
    "bibcode", "alternate_bibcode", "title", "author", "pubdate", "pub", "abstract", "doi",
    "identifier", "doctype", "property", "esources", "citation_count", "reference",
]
# Refereed papers, arXiv preprints, and books and their chapters (which ADS seldom marks refereed);
# no meeting abstracts or errata.
DEFAULT_FILTER = "(property:refereed OR doctype:(eprint OR book OR inbook)) NOT doctype:(abstract OR erratum)"
REFEREED_FILTER = "property:refereed NOT doctype:(abstract OR erratum)"
MAX_Q = 1000  # ADS rejects longer q strings
QUOTA_FLOOR = 50  # pause the run when fewer search requests than this remain today
# The last rate-limit headers each ADS service returned, shared by every litreview process so the
# web UI can show how much of today's quota is used.
QUOTA_FILE = DATA_DIR / "ads-quota.json"

_ARXIV_BIBCODE = re.compile(r"^\d{4}arXiv(\d{4})\.?(\d{4,5})[A-Z.]$")


class ADSError(Exception):
    pass


class ADSQuotaLow(ADSError):
    """Today's request quota is nearly used up; the run pauses and `litreview resume` continues."""

    def __init__(self, remaining: int | None, reset: str | None):
        super().__init__(f"ADS quota low ({remaining} requests left; resets at {reset})")
        self.remaining = remaining
        self.reset = reset


def build_query(q: str, since: int | None, until: int | None, refereed_only: bool) -> str:
    parts = [f"({q})"]
    if since or until:
        parts.append(f"year:{since or 1000}-{until or 3000}")
    parts.append(f"({REFEREED_FILTER if refereed_only else DEFAULT_FILTER})")
    return " AND ".join(parts)


def arxiv_id_from_record(doc: dict) -> str | None:
    """arXiv id from ADS `identifier`, DOIs or arXiv-style bibcodes, without version."""
    candidates = list(doc.get("identifier") or []) + list(doc.get("alternate_bibcode") or [])
    candidates += list(doc.get("doi") or []) + [doc.get("bibcode", "")]
    for c in candidates:
        if c.lower().startswith("arxiv:") or c.lower().startswith("10.48550/arxiv."):
            found = normalize_id(c.split(":", 1)[-1] if c.lower().startswith("arxiv:") else c[15:])
            if found:
                return split_version(found)[0]
        m = _ARXIV_BIBCODE.match(c)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return None


def paper_from_record(doc: dict) -> Paper:
    props = doc.get("property") or []
    return Paper(
        bibcode=doc["bibcode"],
        title=(doc.get("title") or [""])[0],
        authors=list(doc.get("author") or []),
        pubdate=doc.get("pubdate") or "",
        pub=doc.get("pub") or "",
        doi=(doc.get("doi") or [None])[0],
        arxiv_id=arxiv_id_from_record(doc),
        doctype=doc.get("doctype") or "",
        refereed="REFEREED" in props,
        citation_count=int(doc.get("citation_count") or 0),
        ads_abstract=doc.get("abstract"),
        esources=list(doc.get("esources") or []),
        alternate_bibcodes=[b for b in doc.get("alternate_bibcode") or [] if b != doc["bibcode"]],
        references=list(doc.get("reference") or []),  # FIELDS asks for it, and ADS leaves it out when empty
    )


def identifier_queries(bibcodes: list[str]) -> list[str]:
    """`identifier:(...)` queries for these bibcodes, each within MAX_Q. An identifier query also
    finds a record by an alternate bibcode, such as a preprint's after it is published."""
    queries, terms = [], []
    for b in bibcodes:
        if terms and len("identifier:(" + " OR ".join([*terms, f'"{b}"']) + ")") > MAX_Q:
            queries.append("identifier:(" + " OR ".join(terms) + ")")
            terms = []
        terms.append(f'"{b}"')
    if terms:
        queries.append("identifier:(" + " OR ".join(terms) + ")")
    return queries


_BIBCODE = re.compile(r"\d{4}[A-Za-z&.]\S{14}")
_DOI = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:)?(10\.\d{4,9}/\S+)", re.I)
_ADS_LINK = re.compile(r"adsabs\.harvard\.edu/(?:#)?abs/([^/?#]+)", re.I)
_ARXIV_PREFIX = re.compile(r"^(?:(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/|arxiv:)", re.I)
_NAME = r"[^\W\d_][\w'’-]*"
# "Kelly et al. 2009", "Shakura & Sunyaev (1973)", "Zu 2013b lags": first author, a second one, year, other words
_AUTHOR_YEAR = re.compile(rf"({_NAME})(?:\s+et\s+al\.?|\s+(?:and|&)\s+({_NAME}))?,?\s+\(?((?:18|19|20)\d\d)[a-z]?\)?(.*)", re.S)


def _identifier(token: str) -> str | None:
    """The ADS identifier in a bibcode, an arXiv id, a DOI, or a link to any of them."""
    t = unquote(token)
    if m := _ADS_LINK.search(t):
        t = m.group(1)
    if _BIBCODE.fullmatch(t):
        return t
    if m := _DOI.fullmatch(t):
        return m.group(1)
    aid = _ARXIV_PREFIX.sub("", t).removesuffix(".pdf")
    if aid and normalize_id(aid) == aid:
        return "arXiv:" + split_version(aid)[0]
    return None


def lookup_queries(text: str) -> tuple[list[str], list[str]]:
    """ADS queries for what someone typed to find a paper, and the identifiers in it. Bibcodes, arXiv
    ids, DOIs and links to them (any mix, separated by spaces or commas) become identifier queries;
    "Kelly et al. 2009 quasars" becomes first author, year and words; anything else goes to ADS as a
    query. Meeting abstracts and errata are left out of searches."""
    text = text.strip()
    tokens = [t for t in re.split(r"[\s,;]+", text) if t]
    ids = [_identifier(t) for t in tokens]
    if tokens and all(ids):
        ids = list(dict.fromkeys(ids))
        return identifier_queries(ids), ids
    if m := _AUTHOR_YEAR.fullmatch(text):
        first, second, year, rest = m.groups()
        q = f'author:"^{first}"' + (f' author:"{second}"' if second else "") + f" year:{year} {rest.strip()}"
    else:
        q = text
    return [f"({q.strip()}) NOT doctype:(abstract OR erratum)"], []


class ADSClient:
    def __init__(self, token: str | None = None, http: httpx.Client | None = None, base: str = API,
                 quota_floor: int = QUOTA_FLOOR, quota_file: Path | None = QUOTA_FILE):
        token = token or os.environ.get("ADS_API_TOKEN")
        if not token:
            raise ADSError(
                "ADS_API_TOKEN is not set. Create a token at https://ui.adsabs.harvard.edu/user/settings/token "
                "and put ADS_API_TOKEN=... in litreview/.env"
            )
        self.http = http or httpx.Client(timeout=60.0)
        self.headers = {"Authorization": f"Bearer {token}", "User-Agent": user_agent()}
        self.base = base
        self.quota_floor = quota_floor
        self.quota_file = quota_file
        self.remaining: int | None = None
        self.reset: str | None = None

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self.remaining is not None and self.remaining < self.quota_floor:
            raise ADSQuotaLow(self.remaining, self.reset)
        resp = self.http.request(method, f"{self.base}{path}", headers=self.headers, **kwargs)
        if "x-ratelimit-remaining" in resp.headers:
            self.remaining = int(resp.headers["x-ratelimit-remaining"])
            self.reset = resp.headers.get("x-ratelimit-reset")
            if self.quota_file:
                record_quota(self.quota_file, path, resp.headers)
        if resp.status_code == 401:
            raise ADSError("ADS rejected the token (401). Check ADS_API_TOKEN.")
        if resp.status_code == 429:
            raise ADSQuotaLow(self.remaining, self.reset)
        if resp.status_code >= 400:
            raise ADSError(f"ADS {method} {path} returned {resp.status_code}: {resp.text[:300]}")
        return resp

    def search(self, q: str, rows: int = 200, start: int = 0, sort: str = "score desc",
               fl: list[str] = FIELDS) -> tuple[int, list[dict]]:
        if len(q) > MAX_Q:
            raise ADSError(f"query is {len(q)} characters; ADS allows {MAX_Q}")
        data = self._request(
            "GET", "/search/query",
            params={"q": q, "fl": ",".join(fl), "rows": rows, "start": start, "sort": sort},
        ).json()
        response = data.get("response", {})
        return int(response.get("numFound", 0)), list(response.get("docs", []))

    def count(self, q: str) -> int:
        return self.search(q, rows=0, fl=["bibcode"])[0]

    def references(self, bibcodes: list[str]) -> dict[str, list[str]]:
        """What each paper cites, from ADS `reference`, keyed by the bibcode asked for (canonical or
        alternate). Bibcodes ADS has no record for are left out."""
        wanted, out = set(bibcodes), {}
        for q in identifier_queries(list(dict.fromkeys(bibcodes))):
            _, docs = self.search(q, rows=200, fl=["bibcode", "alternate_bibcode", "reference"])
            for doc in docs:
                for b in {doc["bibcode"], *(doc.get("alternate_bibcode") or [])} & wanted:
                    out[b] = list(doc.get("reference") or [])
        return out

    def export_bibtex(self, bibcodes: list[str]) -> str:
        if not bibcodes:
            return ""
        data = self._request("POST", "/export/bibtex", json={"bibcode": list(bibcodes)}).json()
        return data.get("export", "")

    def resolver_arxiv_id(self, bibcode: str) -> str | None:
        """Look for an arXiv link among the paper's full-text sources."""
        try:
            data = self._request("GET", f"/resolver/{quote(bibcode, safe='')}/esource").json()
        except ADSError as e:
            if isinstance(e, ADSQuotaLow):
                raise
            return None
        for s in _strings(data):
            if "arxiv.org/" in s:
                found = normalize_id(s.split("arxiv.org/", 1)[1])
                if found:
                    return split_version(found)[0]
        return None


def _service(path: str) -> str:
    """ADS limits each service separately: '/search/query' -> 'search', '/export/bibtex' -> 'export'."""
    return path.strip("/").split("/", 1)[0]


def record_quota(file: Path, path: str, headers) -> None:
    """Save the rate-limit headers of a response from `path` to `file`. Responses from parallel
    processes can arrive out of order, so a lower remaining count for the same reset time is kept."""
    try:
        entry = {"limit": int(headers["x-ratelimit-limit"]) if "x-ratelimit-limit" in headers else None,
                 "remaining": int(headers["x-ratelimit-remaining"]),
                 "reset": float(headers["x-ratelimit-reset"]) if "x-ratelimit-reset" in headers else None,
                 "at": time.time()}
        file.parent.mkdir(parents=True, exist_ok=True)
        with open(file, "a+") as f:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                saved = json.loads(f.read() or "{}")
            except ValueError:
                saved = {}
            old = saved.get(_service(path))
            if not (old and old.get("reset") == entry["reset"] and old["remaining"] < entry["remaining"]):
                saved[_service(path)] = entry
            f.seek(0)
            f.truncate()
            f.write(json.dumps(saved))
    except (OSError, ValueError):
        pass  # the quota display isn't worth failing a request over


def quota_used(file: Path = QUOTA_FILE, service: str = "search") -> dict | None:
    """How many of today's requests to an ADS service are used, from the last response any litreview
    process saw: {"used", "limit", "reset"}, or None if none has been seen. After the reset time the
    count starts again from zero."""
    try:
        entry = json.loads(Path(file).read_text())[service]
        limit, remaining, reset = entry["limit"], entry["remaining"], entry["reset"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if limit is None:
        return None
    if reset is not None and reset <= time.time():
        return {"used": 0, "limit": limit, "reset": None}
    return {"used": max(limit - remaining, 0), "limit": limit, "reset": reset}


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
