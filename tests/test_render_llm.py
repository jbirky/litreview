import pytest

from fakes import FakeClient, message, text_block
from types import SimpleNamespace
from litreview.library import Library, Paper
from litreview.llm import LLM, CostCapReached, Refused, cost_of
from litreview.render import Labels, entry_heading, render_keys

A = Paper(bibcode="2012ApJ...750....1S", authors=["Smith, J.", "Lee, K.", "Wu, M."], pubdate="2012-05-00",
          first_arxiv_date="2011-11-02", refereed=True)
B = Paper(bibcode="2012MNRAS.420....2S", authors=["Smith, J.", "Ode, P.", "Ray, T."], pubdate="2012-02-00", refereed=True)
C = Paper(bibcode="2015arXiv150100001J", authors=["Jones, A.", "Kim, B."], pubdate="2015-01-00",
          first_arxiv_date="2015-01-02", refereed=False)


def test_labels_disambiguate_same_author_year():
    labels = Labels({p.bibcode: p for p in (A, B, C)})
    # A was posted to arXiv in 2011, so it sorts before B although both carry journal year 2012.
    assert labels[A.bibcode] == "Smith et al. 2012a"
    assert labels[B.bibcode] == "Smith et al. 2012b"
    assert labels[C.bibcode] == "Jones & Kim 2015"


def test_render_keys_links_and_drops_invalid():
    labels = Labels({p.bibcode: p for p in (A, C)})
    out = render_keys(f"Shown [@{A.bibcode}; @{C.bibcode}]. Also [@2099Fake].", labels, {A.bibcode, C.bibcode})
    assert "[Smith et al. 2012](https://ui.adsabs.harvard.edu/abs/2012ApJ...750....1S/abstract)" in out
    assert "[Jones & Kim 2015]" in out and "[@" not in out
    assert "(invalid citation removed)" in out


def test_entry_heading_shows_sort_year_and_journal_year():
    labels = Labels({A.bibcode: A, C.bibcode: C})
    assert entry_heading(A, labels).startswith("**2011 — [Smith et al.](")
    assert entry_heading(A, labels).endswith("(ApJ 2012)")
    assert entry_heading(C, labels).endswith("(preprint)")


def test_cost_of_counts_cache_and_batch():
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_write_tokens": 1_000_000,
             "cache_write_1h": 1_000_000, "cache_read_tokens": 1_000_000}
    assert cost_of("claude-opus-5-5", usage) == pytest.approx(4 + 2 + 8 + 0.2)
    assert cost_of("claude-sonnet-5-5", {"input_tokens": 1_000_000}, batch=True) == pytest.approx(1.0)


def make_llm(tmp_path, responder, cap=10.0):
    lib = Library(tmp_path / "lib.sqlite")
    lib.create_run("r", "q", {}, str(tmp_path))
    client = FakeClient(responder)
    return LLM(lib, "r", cap, client=client), client, lib


def test_llm_call_sends_effort_thinking_and_fallbacks(tmp_path):
    llm, client, lib = make_llm(tmp_path, lambda p: message("hi"))
    reply = llm.call("plan", "claude-opus-5-5", "high", [{"role": "user", "content": "x"}], system="s")
    params = client.calls[0]
    assert params["output_config"] == {"effort": "high"} and params["thinking"] == {"type": "adaptive"}
    assert params["fallbacks"] == "default" and params["betas"] == ["server-side-fallback-2026-07-01"]
    assert reply.text == "hi" and lib.run_cost("r") > 0


def test_llm_refusal_raises_and_is_logged(tmp_path):
    llm, _, lib = make_llm(tmp_path, lambda p: message("", stop_reason="refusal"))
    with pytest.raises(Refused):
        llm.call("extract", "claude-sonnet-5-5", "medium", [{"role": "user", "content": "x"}])
    assert lib.fallbacks("r")[0]["stop_reason"] == "refusal"


def test_llm_logs_fallback_blocks(tmp_path):
    fb = SimpleNamespace(type="fallback", from_=SimpleNamespace(model="claude-opus-5-5"), to=SimpleNamespace(model="claude-opus-5"))
    llm, _, lib = make_llm(tmp_path, lambda p: message([text_block("ok")], model="claude-opus-5", extra_content=[fb]))
    reply = llm.call("plan", "claude-opus-5-5", "high", [{"role": "user", "content": "x"}])
    assert reply.fallback == "claude-opus-5-5->claude-opus-5"
    assert lib.fallbacks("r")[0]["fallback"] == "claude-opus-5-5->claude-opus-5"


def test_cost_cap_stops_before_the_call(tmp_path):
    llm, client, _ = make_llm(tmp_path, lambda p: message("hi"), cap=0.01)
    with pytest.raises(CostCapReached):
        llm.call("plan", "claude-opus-5-5", "high", [{"role": "user", "content": "x" * 100_000}])
    assert client.calls == []


def test_fallback_usage_iterations_are_each_priced(tmp_path):
    def it(model, inp, out):
        return SimpleNamespace(type="fallback_message" if model != "claude-opus-5-5" else "message", model=model,
                               input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0,
                               cache_read_input_tokens=0, cache_creation=None)

    def responder(params):
        msg = message("ok", model="claude-opus-5")
        msg.usage.iterations = [it("claude-opus-5-5", 1_000_000, 0), it("claude-opus-5", 1_000_000, 0)]
        return msg

    llm, _, lib = make_llm(tmp_path, responder, cap=100)
    reply = llm.call("plan", "claude-opus-5-5", "high", [{"role": "user", "content": "x"}])
    assert reply.cost == pytest.approx(4.0 + 5.0)  # the declined Opus 5.5 attempt plus the Opus 5 rescue
    assert reply.usage["input_tokens"] == 2_000_000


def test_cost_cap_counts_the_whole_max_tokens(tmp_path):
    llm, client, _ = make_llm(tmp_path, lambda p: message("hi"), cap=1.0)
    with pytest.raises(CostCapReached):  # 64k output tokens at $20/M could cost $1.28
        llm.call("synthesize", "claude-opus-5-5", "high", [{"role": "user", "content": "x"}], max_tokens=64000)
    assert client.calls == []


def test_load_dotenv_skips_blank_values(tmp_path, monkeypatch):
    from litreview.config import load_dotenv
    env = tmp_path / ".env"
    env.write_text("# comment\nANTHROPIC_API_KEY=\nADS_API_TOKEN='abc'\nexport LITREVIEW_CONTACT=me\n")
    for k in ("ANTHROPIC_API_KEY", "ADS_API_TOKEN", "LITREVIEW_CONTACT"):
        monkeypatch.delenv(k, raising=False)
    load_dotenv(env)
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert os.environ["ADS_API_TOKEN"] == "abc" and os.environ["LITREVIEW_CONTACT"] == "me"


def test_haiku_is_priced_by_prompt_length():
    short = {"input_tokens": 50_000, "output_tokens": 10_000}
    assert cost_of("claude-haiku-5-5", short) == pytest.approx((50_000 * 0.10 + 10_000 * 0.50) / 1e6)
    # Cache reads count toward the 100k threshold, and the whole request moves to the higher card.
    long = {"input_tokens": 10_000, "cache_read_tokens": 95_000, "output_tokens": 1_000}
    assert cost_of("claude-haiku-5-5", long) == pytest.approx((10_000 * 0.50 + 95_000 * 0.05 + 1_000 * 2.50) / 1e6)
    assert cost_of("claude-sonnet-5-5", {"cache_read_tokens": 1_000_000}) == pytest.approx(0.10)


def test_haiku_call_sends_no_fallbacks(tmp_path):
    llm, client, _ = make_llm(tmp_path, lambda p: message("ok", model="claude-haiku-5-5"))
    llm.call("screen", "claude-haiku-5-5", "medium", [{"role": "user", "content": "x"}])
    assert "fallbacks" not in client.calls[0] and "betas" not in client.calls[0]


def test_haiku_refusal_is_retried_once_on_sonnet(tmp_path):
    def responder(params):
        if params["model"] == "claude-haiku-5-5":
            return message("", stop_reason="refusal", model="claude-haiku-5-5")
        return message("rescued", model=params["model"])

    llm, client, lib = make_llm(tmp_path, responder)
    reply = llm.call("screen", "claude-haiku-5-5", "medium", [{"role": "user", "content": "x"}])
    assert reply.text == "rescued" and reply.model == "claude-sonnet-5-5"
    assert reply.fallback.startswith("claude-haiku-5-5->claude-sonnet-5-5")
    assert [c["model"] for c in client.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert client.calls[1]["fallbacks"] == "default"  # the rescue keeps Sonnet's own server-side fallback
    assert lib.fallbacks("r")[0]["stop_reason"] == "refusal"  # the refused attempt stays in the log


def test_refusal_after_the_rescue_still_raises(tmp_path):
    llm, client, _ = make_llm(tmp_path, lambda p: message("", stop_reason="refusal", model=p["model"]))
    with pytest.raises(Refused):
        llm.call("screen", "claude-haiku-5-5", "medium", [{"role": "user", "content": "x"}])
    assert len(client.calls) == 2
