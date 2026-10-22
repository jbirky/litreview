"""Legal full text beyond arXiv: the scans ADS hosts of older papers, and open-access copies that
Unpaywall finds by DOI (a publisher's open-access version, or an author's manuscript in a
repository). Reviews try these only for papers arXiv can't supply.

ADS's scans come through its link gateway, which needs no API token and doesn't count against the
API quota. Unpaywall asks for an email address with every request (UNPAYWALL_EMAIL in .env, else the address
in LITREVIEW_CONTACT); it is skipped without one. Each host gets at most one request every 2 s from a client, and none at all once
it has refused one (http 403 or 429) or shown a robot check: a run or a worker shares one client.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx

from .arxiv import RateLimiter
from .config import VERSION, unpaywall_email, user_agent

ADS_GATEWAY = "https://ui.adsabs.harvard.edu/link_gateway/{}/ADS_PDF"
UNPAYWALL_API = "https://api.unpaywall.org/v2/{}"
HOST_INTERVAL = 2.0
MAX_PDF_BYTES = 100 * 2**20
MAX_LOCATIONS = 3  # open-access copies tried per paper
RETRY_WAIT = 5.0  # seconds before the one retry of a server error
# arXiv is read through its own client, at the rates arXiv asks for; ADS's scans are tried first.
SKIP_HOSTS = ("arxiv.org", "adsabs.harvard.edu")
REFUSALS = (403, 429)
CAPTCHA = re.compile(rb"captcha", re.I)  # Radware's and others' robot checks say so in the page


@dataclass
class Pdf:
    data: bytes
    url: str  # where it came from, after redirects


class OpenAccessClient:
    def __init__(self, http: httpx.Client | None = None, email: str | None = None,
                 interval: float = HOST_INTERVAL, sleep=time.sleep):
        self.http = http or httpx.Client(timeout=60.0, follow_redirects=True)
        self.email = email if email is not None else unpaywall_email()
        self.interval = interval
        self.sleep = sleep
        self.limiters: dict[str, RateLimiter] = {}
        self.refused: dict[str, str] = {}  # host -> what it did, for hosts that aren't asked again

    def _wait(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        self.limiters.setdefault(host, RateLimiter(self.interval, sleep=self.sleep)).wait()

    def pdf(self, url: str) -> tuple[Pdf | None, str]:
        """(the PDF at url, "") or (None, why not). A server error is retried once."""
        host = urlparse(url).hostname or ""
        if host in self.refused:
            return None, f"skipped: {host} {self.refused[host]} earlier, so it isn't asked again"
        for attempt in range(2):
            self._wait(url)
            try:
                with self.http.stream("GET", url, headers={"User-Agent": _agent(host)}) as resp:
                    if resp.status_code >= 500 and attempt == 0:
                        self.sleep(RETRY_WAIT)
                        continue
                    if resp.status_code in REFUSALS:
                        self.refused[host] = f"refused a download (http {resp.status_code})"
                    if resp.status_code != 200:
                        return None, f"http {resp.status_code}"
                    data = bytearray()
                    for chunk in resp.iter_bytes():
                        data += chunk
                        if len(data) > MAX_PDF_BYTES:
                            return None, f"over {MAX_PDF_BYTES // 2**20} MB"
                    final = str(resp.url)
            except httpx.HTTPError as e:
                return None, f"network error: {type(e).__name__}"
            if b"%PDF" not in data[:1024]:
                if CAPTCHA.search(data[:2**16]):
                    self.refused[host] = "showed a robot check"
                    return None, "a robot check (captcha)"
                return None, "not a PDF (a web page, perhaps a sign-in or a robot check)"
            return Pdf(bytes(data), final), ""
        return None, "http 5xx twice"

    def ads_scan(self, bibcode: str) -> tuple[Pdf | None, str]:
        """The scan ADS hosts of an older paper (one whose ADS record lists ADS_PDF)."""
        return self.pdf(ADS_GATEWAY.format(quote(bibcode, safe="")))

    def unpaywall(self, doi: str) -> tuple[list[str], str]:
        """Links to PDFs of legal open-access copies of a DOI, best first, or ([], why there are none)."""
        if not self.email:
            return [], "no email address for Unpaywall (UNPAYWALL_EMAIL or LITREVIEW_CONTACT)"
        url = UNPAYWALL_API.format(quote(doi, safe="/"))
        self._wait(url)
        try:
            resp = self.http.get(url, params={"email": self.email}, headers={"User-Agent": _agent("api.unpaywall.org")})
        except httpx.HTTPError as e:
            return [], f"network error: {type(e).__name__}"
        if resp.status_code == 404:
            return [], "Unpaywall doesn't know this DOI"
        if resp.status_code != 200:
            return [], f"http {resp.status_code}"
        try:
            record = resp.json()
        except ValueError:
            return [], "Unpaywall's reply wasn't JSON"
        if not record.get("is_oa"):
            return [], "no open-access copy"
        locations = [record.get("best_oa_location"), *(record.get("oa_locations") or [])]
        links = [link for loc in locations if loc and (link := _pdf_link(loc))]
        links = [u for u in dict.fromkeys(links) if not (urlparse(u).hostname or "").endswith(SKIP_HOSTS)]
        return links[:MAX_LOCATIONS], "" if links else "no PDF among its open-access copies, besides arXiv's or ADS's"


def _pdf_link(location: dict) -> str | None:
    """A copy's PDF link. Some repositories' copies have none, but a "landing page" that is the PDF."""
    landing = location.get("url_for_landing_page") or ""
    return location.get("url_for_pdf") or (landing if urlparse(landing).path.lower().endswith(".pdf") else None)


def _agent(host: str) -> str:
    """LITREVIEW_CONTACT goes only to ADS (and to arXiv, through its own client), as the README says."""
    return user_agent() if host.endswith("adsabs.harvard.edu") else f"litreview/{VERSION}"
