"""Stand-ins for the Anthropic SDK client, the ADS API and arXiv, for offline tests."""

from __future__ import annotations

import io
import json
import tarfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx

from litreview.ads import ADSClient
from litreview.arxiv import ArxivClient, RateLimiter
from litreview.openaccess import OpenAccessClient


# --- Anthropic SDK --------------------------------------------------------------------------


def text_block(text: str, citations: list[dict] | None = None):
    cites = [SimpleNamespace(type="content_block_location", document_index=0, **c) for c in citations or []]
    return SimpleNamespace(type="text", text=text, citations=cites or None)


def message(blocks, *, model="claude-opus-5-5", stop_reason="end_turn", input_tokens=1000,
            output_tokens=200, cache_write=0, cache_read=0, extra_content=()):
    usage = SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_creation_input_tokens=cache_write, cache_read_input_tokens=cache_read,
        cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=cache_write, ephemeral_5m_input_tokens=0),
        iterations=None,
    )
    if isinstance(blocks, str):
        blocks = [text_block(blocks)]
    return SimpleNamespace(content=[*extra_content, *blocks], usage=usage, stop_reason=stop_reason,
                           model=model, id="msg_fake", stop_details=None)


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


class _Batches:
    def __init__(self, owner):
        self.owner = owner
        self.store: dict[str, list] = {}

    def create(self, requests):
        batch_id = f"batch_{len(self.store) + 1}"
        results = []
        for r in requests:
            params = dict(r["params"])
            self.owner.batch_params.append(params)
            results.append((r["custom_id"], self.owner.responder(params)))
        self.store[batch_id] = results
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    def retrieve(self, batch_id):
        counts = SimpleNamespace(processing=0, succeeded=len(self.store[batch_id]), errored=0, canceled=0, expired=0)
        return SimpleNamespace(id=batch_id, processing_status="ended", request_counts=counts)

    def results(self, batch_id):
        for cid, msg in self.store[batch_id]:
            yield SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="succeeded", message=msg))


class FakeClient:
    """responder(params) -> message; records every request's params."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []
        self.batch_params: list[dict] = []
        messages = SimpleNamespace(stream=self._stream, batches=_Batches(self))
        self.messages = messages
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.calls.append(params)
        return _Stream(self.responder(params))


# --- ADS -------------------------------------------------------------------------------------


def ads_doc(bibcode, year, title, authors, *, arxiv=None, abstract="An abstract.", refereed=True,
            alternates=(), doi=None, citations=10, month="03", refs=()):
    ident = [bibcode, *alternates]
    if arxiv:
        ident.append(f"arXiv:{arxiv}")
    doc = {
        "bibcode": bibcode, "alternate_bibcode": list(alternates), "title": [title], "author": authors,
        "pubdate": f"{year}-{month}-00", "pub": "The Astrophysical Journal" if refereed else "arXiv e-prints",
        "abstract": abstract, "doi": [doi] if doi else [], "identifier": ident,
        "doctype": "article" if refereed else "eprint", "property": ["REFEREED"] if refereed else ["NOT REFEREED"],
        "esources": ["EPRINT_HTML", "EPRINT_PDF"] if arxiv else [], "citation_count": citations,
    }
    if refs:  # ADS leaves the field out when a paper cites nothing it knows
        doc["reference"] = list(refs)
    return doc


def fake_ads(route) -> ADSClient:
    """route(path, params, json_body) -> (status, json, headers)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = json.loads(request.content) if request.content else None
        status, data, headers = route(url.path.removeprefix("/v1"), params, body)
        headers = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "1700000000", **(headers or {})}
        return httpx.Response(status, json=data, headers=headers)

    return ADSClient(token="test-token", http=httpx.Client(transport=httpx.MockTransport(handler)), quota_file=None)


# --- arXiv -----------------------------------------------------------------------------------


def atom(entries: list[tuple[str, str, str, str]]) -> str:
    """entries: (versioned id, published date, title, abstract)."""
    body = "".join(
        f"<entry><id>http://arxiv.org/abs/{i}</id><published>{d}T00:00:00Z</published>"
        f"<title>{t}</title><summary>{a}</summary></entry>"
        for i, d, t, a in entries
    )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'


def latex_tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def fake_oa(route, email: str | None = "test@example.org") -> OpenAccessClient:
    """route(host, path, params) -> httpx.Response, for ADS's scans, Unpaywall and the copies it finds."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        return route(url.netloc, url.path, {k: v[0] for k, v in parse_qs(url.query).items()})

    return OpenAccessClient(http=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
                            email=email or "", interval=0, sleep=lambda seconds: None)


def no_oa(host, path, params):
    raise AssertionError(f"unexpected open-access request: {host}{path}")


def fake_arxiv(route) -> ArxivClient:
    """route(host, path, params) -> httpx.Response."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        return route(url.netloc, url.path, params)

    no_wait = lambda seconds: None  # retry backoff too, so a 429 route costs no test time
    return ArxivClient(http=httpx.Client(transport=httpx.MockTransport(handler)),
                       export_limiter=RateLimiter(0, sleep=no_wait), main_limiter=RateLimiter(0, sleep=no_wait))
