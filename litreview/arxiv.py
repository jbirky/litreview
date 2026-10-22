"""arXiv access: the API for metadata, export.arxiv.org for LaTeX source, arxiv.org for HTML and PDF.

arxiv.org's robots.txt disallows /src and /e-print, and arXiv's bulk-data page asks programmatic
clients to use export.arxiv.org. Every export.arxiv.org request (API or source) shares one limiter
at one request every 3 s, the API's rule. HTML and PDF come from arxiv.org at its 15 s crawl delay.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .config import DATA_DIR, user_agent

try:
    import fcntl
except ImportError:  # not on Linux/macOS: fall back to per-process limiting
    fcntl = None

EXPORT = "https://export.arxiv.org"
MAIN = "https://arxiv.org"
EXPORT_INTERVAL = 3.0
MAIN_INTERVAL = 15.0
MAX_REDIRECTS = 5
THROTTLE_COOLDOWN = 900.0  # seconds before a throttled host is tried again
ATOM = {"a": "http://www.w3.org/2005/Atom"}

_NEW_ID = re.compile(r"\d{4}\.\d{4,5}")
_OLD_ID = re.compile(r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}")
_ID_WITH_VERSION = re.compile(rf"({_NEW_ID.pattern}|{_OLD_ID.pattern})(v\d+)?")


class RateLimiter:
    """Spaces request starts at least `interval` seconds apart."""

    def __init__(self, interval: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = interval
        self.clock = clock
        self.sleep = sleep
        self.next_ok = 0.0

    def wait(self) -> None:
        now = self.clock()
        if now < self.next_ok:
            self.sleep(self.next_ok - now)
            now = self.next_ok
        self.next_ok = now + self.interval


class SharedRateLimiter(RateLimiter):
    """Spaces requests across every litreview process, through a lock file holding the last request time."""

    def __init__(self, interval: float, path: Path, sleep=time.sleep):
        super().__init__(interval, clock=time.time, sleep=sleep)
        self.path = path

    def wait(self) -> None:
        if fcntl is None:
            return super().wait()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read().strip()
                last = float(raw) if raw else 0.0
                now = self.clock()
                if now < last + self.interval:
                    self.sleep(last + self.interval - now)
                f.seek(0)
                f.truncate()
                f.write(repr(self.clock()))
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def normalize_id(raw: str) -> str | None:
    """'arXiv:2101.01234v2', 'https://arxiv.org/abs/astro-ph/0501001' -> id with any version kept."""
    m = _ID_WITH_VERSION.search(raw.strip())
    return m.group(0) if m else None


def split_version(arxiv_id: str) -> tuple[str, str | None]:
    m = _ID_WITH_VERSION.fullmatch(arxiv_id)
    if not m:
        return arxiv_id, None
    return m.group(1), m.group(2)


def posting_month(arxiv_id: str) -> str | None:
    """Month of first posting, which every arXiv id encodes: '1801.06117' -> '2018-01-00',
    'astro-ph/9911436' -> '1999-11-00'. Same precision as ADS pubdates, with no request."""
    base, _ = split_version(arxiv_id)
    if m := re.fullmatch(r"(\d{2})(\d{2})\.\d{4,5}", base):
        year = 2000 + int(m.group(1))  # new-style ids began in April 2007
    elif m := re.search(r"/(\d{2})(\d{2})\d{3}$", base):
        yy = int(m.group(1))
        year = 1900 + yy if yy >= 91 else 2000 + yy  # old-style ids ran from 1991 to March 2007
    else:
        return None
    month = int(m.group(2))
    return f"{year}-{month:02d}-00" if 1 <= month <= 12 else None


class ArxivThrottled(httpx.HTTPError):
    """A host kept answering 429/503 through every retry. The client leaves it alone for
    THROTTLE_COOLDOWN, so later papers go straight to the fallbacks on other hosts."""


@dataclass
class ArxivMeta:
    arxiv_id: str  # without version
    latest_version: str  # e.g. "2101.01234v3"
    published: str  # first posting date (v1), YYYY-MM-DD
    title: str
    abstract: str


class ArxivClient:
    def __init__(
        self,
        http: httpx.Client | None = None,
        export_limiter: RateLimiter | None = None,
        main_limiter: RateLimiter | None = None,
        max_retries: int = 3,
        clock=time.monotonic,
    ):
        self.http = http or httpx.Client(headers={"User-Agent": user_agent()}, timeout=90.0)
        self.export_limiter = export_limiter or SharedRateLimiter(EXPORT_INTERVAL, DATA_DIR / "arxiv-export.lock")
        self.main_limiter = main_limiter or SharedRateLimiter(MAIN_INTERVAL, DATA_DIR / "arxiv-main.lock")
        self.max_retries = max_retries
        self.clock = clock
        self.throttled: dict[str, float] = {}  # host -> when it may be tried again

    def _limiter_for(self, url: str) -> RateLimiter:
        return self.export_limiter if urlparse(url).netloc.startswith("export.") else self.main_limiter

    def _get(self, url: str, limiter: RateLimiter, **kwargs) -> httpx.Response:
        """GET that follows redirects itself, so every hop waits on its host's limiter."""
        for _ in range(MAX_REDIRECTS + 1):
            resp = self._get_once(url, limiter, **kwargs)
            if not resp.is_redirect or "location" not in resp.headers:
                return resp
            url = urljoin(str(resp.url), resp.headers["location"])
            limiter = self._limiter_for(url)
            kwargs.pop("params", None)  # the redirect target carries its own query
        return resp

    def _get_once(self, url: str, limiter: RateLimiter, **kwargs) -> httpx.Response:
        host = urlparse(url).netloc
        if host in self.throttled:
            if self.clock() < self.throttled[host]:
                raise ArxivThrottled(f"{host} is rate limiting this machine; skipped for now")
            del self.throttled[host]
        for attempt in range(self.max_retries + 1):
            limiter.wait()
            resp = self.http.get(url, follow_redirects=False, **kwargs)
            if resp.status_code not in (429, 503):
                return resp
            if attempt == self.max_retries:
                self.throttled[host] = self.clock() + THROTTLE_COOLDOWN
                raise ArxivThrottled(f"{host} answered {resp.status_code} to {attempt + 1} tries")
            retry_after = resp.headers.get("retry-after", "")
            limiter.sleep(float(retry_after) if retry_after.isdigit() else 30.0 * (attempt + 1))
        raise AssertionError("unreachable")

    def metadata(self, ids: list[str]) -> dict[str, ArxivMeta]:
        """First posting date, latest version and abstract for each id, 100 ids per request."""
        bases = sorted({split_version(i)[0] for i in ids})
        out: dict[str, ArxivMeta] = {}
        for start in range(0, len(bases), 100):
            chunk = bases[start : start + 100]
            resp = self._get(
                f"{EXPORT}/api/query",
                self.export_limiter,
                params={"id_list": ",".join(chunk), "max_results": len(chunk)},
            )
            resp.raise_for_status()
            out.update(parse_atom(resp.text))
        return out

    def source(self, versioned_id: str) -> httpx.Response:
        return self._get(f"{EXPORT}/src/{versioned_id}", self.export_limiter)

    def html(self, versioned_id: str) -> httpx.Response:
        return self._get(f"{MAIN}/html/{versioned_id}", self.main_limiter)

    def pdf(self, versioned_id: str) -> httpx.Response:
        return self._get(f"{MAIN}/pdf/{versioned_id}", self.main_limiter)


def parse_atom(xml_text: str) -> dict[str, ArxivMeta]:
    root = ET.fromstring(xml_text)
    out: dict[str, ArxivMeta] = {}
    for entry in root.findall("a:entry", ATOM):
        id_url = (entry.findtext("a:id", default="", namespaces=ATOM) or "").strip()
        versioned = normalize_id(id_url.split("/abs/")[-1]) if "/abs/" in id_url else None
        if not versioned:
            continue  # error entries have no /abs/ id
        base, version = split_version(versioned)
        out[base] = ArxivMeta(
            arxiv_id=base,
            latest_version=versioned if version else f"{base}v1",
            published=(entry.findtext("a:published", default="", namespaces=ATOM) or "")[:10],
            title=" ".join((entry.findtext("a:title", default="", namespaces=ATOM) or "").split()),
            abstract=" ".join((entry.findtext("a:summary", default="", namespaces=ATOM) or "").split()),
        )
    return out
