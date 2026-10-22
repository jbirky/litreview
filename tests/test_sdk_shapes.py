"""The real Anthropic SDK against a mock HTTP transport: request bodies are what the API expects,
and responses (streamed citations, fallback blocks, batch results) parse into what llm.py reads.
"""

import json

import anthropic
import httpx2
import pytest

from litreview.extract import parse_tagged, request_params
from litreview.library import Library, Paper, StoredText
from litreview.llm import FALLBACK_BETA, LLM

MSG = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
       "stop_reason": None, "stop_sequence": None,
       "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
CITE = {"type": "content_block_location", "cited_text": "We fit twelve transits.", "document_index": 0,
        "document_title": "T", "start_block_index": 1, "end_block_index": 2}


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


STREAM = sse([
    {"type": "message_start", "message": {**MSG, "content": []}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "RELEVANCE: high - yes\nMETHOD: "}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "", "citations": []}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Joint fit of 12 transits"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "citations_delta", "citation": CITE}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 40}},
    {"type": "message_stop"},
])

PAPER = Paper(bibcode="2019ApJ...880....1S", title="T", pubdate="2019-04-00", pub="ApJ")
TEXT = StoredText(PAPER.bibcode, "arxiv_src", "1901.00001v2",
                  [{"label": "Abstract", "text": "a"}, {"label": "2 Methods, para 1", "text": "We fit twelve transits."}], False)


@pytest.fixture
def client_and_requests():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, dict(request.headers), body))
        path = request.url.path
        if path == "/v1/messages":
            return httpx2.Response(200, text=STREAM, headers={"content-type": "text/event-stream", "request-id": "req_1"})
        batch = {"id": "msgbatch_1", "type": "message_batch", "processing_status": "ended",
                 "request_counts": {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0},
                 "created_at": "2026-10-15T00:00:00Z", "expires_at": "2026-10-16T00:00:00Z",
                 "ended_at": "2026-10-15T00:10:00Z", "cancel_initiated_at": None, "archived_at": None,
                 "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_1/results"}
        if path == "/v1/messages/batches" or path == "/v1/messages/batches/msgbatch_1":
            return httpx2.Response(200, json=batch)
        if path == "/v1/messages/batches/msgbatch_1/results":
            done = {**MSG, "stop_reason": "end_turn", "usage": {**MSG["usage"], "output_tokens": 40},
                    "content": [{"type": "text", "text": "RELEVANCE: high - yes\nMETHOD: "},
                                {"type": "text", "text": "Joint fit of 12 transits", "citations": [CITE]}]}
            line = json.dumps({"custom_id": "p0", "result": {"type": "succeeded", "message": done}})
            return httpx2.Response(200, text=line + "\n", headers={"content-type": "application/binary"})
        return httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": path}})

    client = anthropic.Anthropic(api_key="test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return client, seen


def make_llm(tmp_path, client):
    lib = Library(tmp_path / "lib.sqlite")
    lib.create_run("r", "q", {}, str(tmp_path))
    return LLM(lib, "r", 10.0, client=client)


def test_streamed_extraction_request_and_citations(tmp_path, client_and_requests):
    client, seen = client_and_requests
    llm = make_llm(tmp_path, client)
    params = request_params("What drives inflation?", PAPER, TEXT)
    reply = llm.call("extract", "claude-sonnet-5-5", "medium", params["messages"], system=params["system"], max_tokens=24000)

    method, path, headers, body = seen[0]
    assert (method, path) == ("POST", "/v1/messages")
    assert FALLBACK_BETA in headers["anthropic-beta"]
    assert body["fallbacks"] == "default" and body["stream"] is True
    assert body["output_config"] == {"effort": "medium"} and body["thinking"] == {"type": "adaptive"}
    doc = body["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["type"] == "content" and doc["citations"] == {"enabled": True}
    assert doc["source"]["content"][1] == {"type": "text", "text": "[2 Methods, para 1] We fit twelve transits."}

    items = parse_tagged(reply.blocks)
    assert [(i.tag, i.text, i.passages) for i in items] == [
        ("RELEVANCE", "high - yes", []), ("METHOD", "Joint fit of 12 transits", [1])]
    assert reply.usage["output_tokens"] == 40 and reply.request_id


def test_structured_output_and_cache_control_serialize(tmp_path, client_and_requests):
    client, seen = client_and_requests
    llm = make_llm(tmp_path, client)
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
    llm.call("plan", "claude-opus-5-5", "high",
             [{"role": "user", "content": [{"type": "text", "text": "cards", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                                           {"type": "text", "text": "task"}]}],
             system="s", output_format={"type": "json_schema", "schema": schema})
    body = seen[0][3]
    assert body["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": schema}}
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_batch_request_and_results(tmp_path, client_and_requests):
    client, seen = client_and_requests
    llm = make_llm(tmp_path, client)
    params = request_params("What drives inflation?", PAPER, TEXT)
    batch_id = llm.batch_submit("extract", "claude-sonnet-5-5", "medium", [("p0", params)], max_tokens=24000)
    assert batch_id == "msgbatch_1"
    body = seen[0][3]
    req = body["requests"][0]
    assert req["custom_id"] == "p0" and "fallbacks" not in req["params"]
    assert req["params"]["output_config"] == {"effort": "medium"} and req["params"]["model"] == "claude-sonnet-5-5"

    status, counts = llm.batch_status(batch_id)
    assert status == "ended" and counts["succeeded"] == 1
    results = llm.batch_results("extract", "claude-sonnet-5-5", "medium", batch_id)
    items = parse_tagged(results["p0"].blocks)
    assert items[1].passages == [1]
    assert llm.lib.usage_by_stage("r")[0]["calls"] == 1


def test_haiku_screening_request_uses_the_plain_endpoint(tmp_path, client_and_requests):
    client, seen = client_and_requests
    llm = make_llm(tmp_path, client)
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
    llm.call("screen", "claude-haiku-5-5", "medium", [{"role": "user", "content": "papers"}],
             system="s", output_format={"type": "json_schema", "schema": schema})
    method, path, headers, body = seen[0]
    assert (method, path) == ("POST", "/v1/messages")
    assert "fallbacks" not in body and FALLBACK_BETA not in headers.get("anthropic-beta", "")
    assert body["model"] == "claude-haiku-5-5" and body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": schema}}
