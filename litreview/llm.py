"""Claude calls: streaming with server-side refusal fallbacks, Batches for extraction, cost logging.

Every interactive request opts into `fallbacks: "default"`. The Batches API rejects that parameter,
so a batched request that is refused is retried once interactively by the caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import BATCH_DISCOUNT, LONG_PROMPT_PRICES, PRICES, REFUSAL_RESCUE, UNKNOWN_PRICE
from .library import Library

FALLBACK_BETA = "server-side-fallback-2026-07-01"
THINKING = {"type": "adaptive"}


class Refused(Exception):
    def __init__(self, stage: str, category: str | None):
        super().__init__(f"{stage}: Claude declined the request (category: {category or 'unknown'})")
        self.category = category


class CostCapReached(Exception):
    def __init__(self, message: str, *, estimate: float = 0.0, spent: float = 0.0, cap: float = 0.0):
        super().__init__(message)
        self.estimate, self.spent, self.cap = estimate, spent, cap


@dataclass
class Reply:
    blocks: list[dict]  # text blocks: {"text": str, "citations": [{cited_text, start_block_index, ...}]}
    stop_reason: str | None
    model: str
    usage: dict
    cost: float
    fallback: str | None = None
    request_id: str | None = None

    @property
    def text(self) -> str:
        return "".join(b["text"] for b in self.blocks)


def price(model: str, prompt_tokens: int = 0) -> tuple[float, float, float, float, float]:
    """Rates for one request; prompt_tokens picks Haiku's long-prompt rate card."""
    if model in LONG_PROMPT_PRICES:
        threshold, long_rates = LONG_PROMPT_PRICES[model]
        if prompt_tokens > threshold:
            return long_rates
    return PRICES.get(model, UNKNOWN_PRICE)


def cost_of(model: str, usage: dict, batch: bool = False) -> float:
    prompt = usage.get("input_tokens", 0) + usage.get("cache_write_tokens", 0) + usage.get("cache_read_tokens", 0)
    p_in, p_out, p_w5, p_w1h, p_read = price(model, prompt)
    write_1h = usage.get("cache_write_1h", 0)
    write_5m = usage.get("cache_write_tokens", 0) - write_1h
    total = (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + write_5m * p_w5
        + write_1h * p_w1h
        + usage.get("cache_read_tokens", 0) * p_read
    ) / 1e6
    return total * (BATCH_DISCOUNT if batch else 1.0)


def estimate_tokens(obj) -> int:
    """Generous input size (3 characters per token), for the cost cap only."""
    return len(json.dumps(obj, ensure_ascii=False)) // 3


def usage_dict(u) -> dict:
    cache_creation = getattr(u, "cache_creation", None)
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_write_1h": getattr(cache_creation, "ephemeral_1h_input_tokens", 0) or 0,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
    }


def usage_parts(msg, requested: str) -> list[tuple[str, dict]]:
    """(model, usage) per attempt. With fallbacks, usage.iterations is the per-attempt record and
    top-level usage covers only the attempt that produced the message."""
    iterations = getattr(msg.usage, "iterations", None) or []
    served = getattr(msg, "model", None) or requested
    if iterations:
        return [(getattr(it, "model", None) or served, usage_dict(it)) for it in iterations]
    return [(served, usage_dict(msg.usage))]


def text_blocks(content) -> list[dict]:
    out = []
    for b in content:
        if getattr(b, "type", None) != "text":
            continue
        cites = []
        for c in getattr(b, "citations", None) or []:
            cites.append({
                "type": getattr(c, "type", None),
                "cited_text": getattr(c, "cited_text", ""),
                "document_index": getattr(c, "document_index", None),
                "start_block_index": getattr(c, "start_block_index", None),
                "end_block_index": getattr(c, "end_block_index", None),
            })
        out.append({"text": b.text, "citations": cites})
    return out


def fallback_note(msg) -> str | None:
    notes = []
    for b in msg.content:
        if getattr(b, "type", None) == "fallback":
            src = getattr(getattr(b, "from_", None), "model", "?")
            dst = getattr(getattr(b, "to", None), "model", "?")
            notes.append(f"{src}->{dst}")
    iterations = getattr(msg.usage, "iterations", None) or []
    if not notes and any(getattr(i, "type", None) == "fallback_message" for i in iterations):
        notes.append(f"served by {msg.model}")
    return "; ".join(notes) or None


class LLM:
    """One per run. Logs every call's usage and cost to the library and enforces the run's cost cap."""

    def __init__(self, lib: Library, run_id: str, cost_cap: float, client=None):
        self.lib = lib
        self.run_id = run_id
        self.cost_cap = cost_cap
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def spent(self) -> float:
        return self.lib.run_cost(self.run_id)

    def check_budget(self, model: str, input_tokens: int, output_tokens: int, batch: bool = False) -> None:
        """Refuse a call whose worst case (all of max_tokens, input at the cache-write rate) passes the cap."""
        p_in, p_out, _, p_write_1h, _ = price(model, input_tokens)
        estimate = (input_tokens * max(p_in, p_write_1h) + output_tokens * p_out) / 1e6
        estimate *= BATCH_DISCOUNT if batch else 1.0
        if self.spent() + estimate > self.cost_cap:
            raise CostCapReached(
                f"next call (~${estimate:.2f}) would take the run past its ${self.cost_cap:.2f} cap "
                f"(spent ${self.spent():.2f}); raise --cost-cap and run `litreview resume`",
                estimate=estimate, spent=self.spent(), cap=self.cost_cap,
            )

    def call(self, stage: str, model: str, effort: str, messages: list[dict], *,
             system: str | list | None = None, max_tokens: int = 32000,
             output_format: dict | None = None) -> Reply:
        """Streamed request with adaptive thinking, explicit effort and refusal fallbacks."""
        self.check_budget(model, estimate_tokens([system, messages]), max_tokens)
        params = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "thinking": THINKING,
            "output_config": {"effort": effort, **({"format": output_format} if output_format else {})},
        }
        if system:
            params["system"] = system
        msg = self._stream(params)
        reply = self._record(stage, model, effort, msg, batch=False)
        if reply.stop_reason == "refusal" and model in REFUSAL_RESCUE:
            # No server-side fallback for this model, so retry the same request on one that has it.
            rescue = REFUSAL_RESCUE[model]
            self.check_budget(rescue, estimate_tokens([system, messages]), max_tokens)
            msg = self._stream({**params, "model": rescue})
            reply = self._record(stage, rescue, effort, msg, batch=False)
            reply.fallback = f"{model}->{rescue} (retried after a refusal)" + (f"; {reply.fallback}" if reply.fallback else "")
        if reply.stop_reason == "refusal":
            raise Refused(stage, getattr(getattr(msg, "stop_details", None), "category", None))
        return reply

    def _stream(self, params: dict):
        if params["model"].startswith("claude-haiku"):
            # No server-side fallback exists for Haiku; sending it is an error for the list form.
            with self.client.messages.stream(**params) as stream:
                return stream.get_final_message()
        with self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **params) as stream:
            return stream.get_final_message()

    def _record(self, stage: str, model: str, effort: str, msg, batch: bool) -> Reply:
        parts = usage_parts(msg, model)
        usage = {k: sum(u.get(k, 0) for _, u in parts) for k in parts[0][1]}
        served = getattr(msg, "model", model) or model
        cost = sum(cost_of(m, u, batch) for m, u in parts)
        note = fallback_note(msg)
        request_id = getattr(msg, "_request_id", None) or getattr(msg, "id", None)
        self.lib.log_call(self.run_id, stage, model, effort, batch, usage, cost,
                          msg.stop_reason, note, request_id)
        return Reply(text_blocks(msg.content), msg.stop_reason, served, usage, cost, note, request_id)

    # --- batches ------------------------------------------------------------------------

    def batch_submit(self, stage: str, model: str, effort: str,
                     requests: list[tuple[str, dict]], max_tokens: int) -> str:
        """requests: (custom_id, {"system": ..., "messages": [...]}). Returns the batch id."""
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        est_in = sum(estimate_tokens(r) for _, r in requests)
        self.check_budget(model, est_in, len(requests) * max_tokens, batch=True)
        batch = self.client.messages.batches.create(
            requests=[
                Request(
                    custom_id=cid,
                    params=MessageCreateParamsNonStreaming(
                        model=model,
                        max_tokens=max_tokens,
                        thinking=THINKING,
                        output_config={"effort": effort},
                        **r,
                    ),
                )
                for cid, r in requests
            ]
        )
        return batch.id

    def batch_status(self, batch_id: str) -> tuple[str, dict]:
        b = self.client.messages.batches.retrieve(batch_id)
        counts = b.request_counts
        return b.processing_status, {
            k: getattr(counts, k, 0) for k in ("processing", "succeeded", "errored", "canceled", "expired")
        }

    def batch_results(self, stage: str, model: str, effort: str, batch_id: str,
                      skip: set[str] = frozenset()) -> dict[str, Reply | str]:
        """custom_id -> Reply, or an error string for errored, canceled or expired requests.

        Results in `skip` were handled (and their cost recorded) before an interruption.
        """
        out: dict[str, Reply | str] = {}
        for result in self.client.messages.batches.results(batch_id):
            if result.custom_id in skip:
                continue
            kind = result.result.type
            if kind == "succeeded":
                out[result.custom_id] = self._record(stage, model, effort, result.result.message, batch=True)
            elif kind == "errored":
                err = getattr(result.result, "error", None)
                out[result.custom_id] = f"errored: {getattr(getattr(err, 'error', err), 'type', err)}"
            else:
                out[result.custom_id] = kind
        return out
