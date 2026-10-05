"""Per-message pipeline: dedupe -> handoff mute -> input guardrails -> rate/budget limits -> agent -> output guardrails -> metrics."""
from __future__ import annotations
import os
import time

from .agent import Context, run_agent
from .guardrails import BLOCK_REPLY, HANDOFF_REPLY, check_input, check_output, redact_pii, ungrounded_numbers
from .llm import LLMUnavailable
from .observability import emit_metrics, event, hash_id
from .tools import do_escalate

RATE_REPLY = "You're sending messages very quickly. Please give me a moment and try again in a minute."
DOWN_REPLY = ("Sorry, I'm having trouble right now. I've passed your message to a teammate "
              "who will reply here shortly.")
HISTORY_TURNS = 10


def _trim(history: list) -> list:
    h = history[-HISTORY_TURNS:]
    while h and h[0]["role"] != "user":
        h = h[1:]
    return h


def _remember(state: dict, user_text: str, reply: str | None) -> None:
    state["history"].append({"role": "user", "content": [{"text": user_text}]})
    if reply:
        state["history"].append({"role": "assistant", "content": [{"text": reply}]})
    state["history"] = _trim(state["history"])


def handle_message(deps, customer_id: str, text: str, msg_id: str | None = None) -> dict:
    s, store = deps.settings, deps.store
    t0 = time.perf_counter()
    text = (text or "").strip()[:1000]
    today = time.strftime("%Y-%m-%d", time.gmtime())

    if msg_id and store.seen_message(msg_id):  # Meta retries webhooks; make processing idempotent
        return {"status": "duplicate", "reply": None}

    state = store.get_state(customer_id)
    if state.get("handoff"):  # a human owns this thread now; the bot stays silent
        _remember(state, text, None)
        store.put_state(customer_id, state)
        event("muted_for_human", customer=hash_id(customer_id))
        return {"status": "muted", "reply": None, "handoff": True}

    result = {"status": "ok", "reply": "", "handoff": False, "handoff_reason": "", "tool_calls": [],
              "flags": [], "cost_usd": 0.0, "iterations": 0, "fallback_used": False}
    guard = check_input(text)

    if guard.action == "block":
        result.update(reply=guard.reply, flags=["blocked:" + guard.reason])
    elif store.incr(f"{customer_id}:{int(time.time() // 60)}", 120) > s.rate_limit_per_min:
        result.update(status="rate_limited", reply=RATE_REPLY, flags=["rate_limited"])
    elif store.get_spend(today) >= s.daily_budget_usd:
        result.update(handoff=True, handoff_reason="daily_budget_exceeded", reply=DOWN_REPLY, flags=["budget"])
    elif guard.action == "handoff":
        result.update(handoff=True, handoff_reason=guard.reason, reply=HANDOFF_REPLY)
    else:
        ctx = Context(s, deps.llm, store, deps.retriever, customer_id)
        try:
            res = run_agent(text, state["history"], ctx)
            reply, flags = check_output(res.reply)
            evidence = text + " " + " ".join(str(t["output"]) for t in ctx.tool_log)
            ungrounded = ungrounded_numbers(reply, evidence)
            if ungrounded:
                flags.append("ungrounded_numbers:" + ",".join(ungrounded))
            # Low-confidence streak: two consecutive unanswerable questions => stop flailing, hand off.
            state["low_conf_streak"] = state.get("low_conf_streak", 0) + 1 if ctx.low_conf_hits else 0
            if state["low_conf_streak"] >= 2 and not ctx.handoff:
                ctx.handoff, ctx.handoff_reason = True, "repeated_low_confidence"
            result.update(reply=reply, handoff=ctx.handoff, handoff_reason=ctx.handoff_reason, flags=flags,
                          tool_calls=ctx.tool_log, cost_usd=res.cost_usd, iterations=res.iterations,
                          fallback_used=res.fallback_used, input_tokens=res.input_tokens,
                          output_tokens=res.output_tokens)
            if ctx.handoff and not any(t["name"] == "escalate_to_human" for t in ctx.tool_log):
                do_escalate(s, store, customer_id, ctx.handoff_reason, text[:300])
        except LLMUnavailable:
            result.update(handoff=True, handoff_reason="llm_unavailable", reply=DOWN_REPLY, flags=["llm_unavailable"])

    if result["handoff"] and result["handoff_reason"] in ("sensitive_keyword", "daily_budget_exceeded", "llm_unavailable"):
        do_escalate(s, store, customer_id, result["handoff_reason"], text[:300])
    if result["handoff"]:
        state["handoff"] = True
    _remember(state, text, result["reply"])
    store.put_state(customer_id, state)
    if result["cost_usd"]:
        store.add_spend(today, result["cost_usd"])

    result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    _record(s, customer_id, text, result)
    return result


def _record(s, customer_id, text, r) -> None:
    fields = dict(customer=hash_id(customer_id), status=r["status"], handoff=r["handoff"],
                  handoff_reason=r["handoff_reason"], tools=[t["name"] for t in r["tool_calls"]],
                  iterations=r["iterations"], latency_ms=r["latency_ms"], cost_usd=round(r["cost_usd"], 6),
                  fallback=r["fallback_used"], flags=r["flags"])
    if os.environ.get("LOG_CONTENT") == "1":
        fields["user_text"] = redact_pii(text)[:300]
        fields["reply"] = redact_pii(r["reply"] or "")[:300]
    event("turn", **fields)
    emit_metrics(s.namespace, s.service, {
        "Turns": (1, "Count"),
        "LatencyMs": (r["latency_ms"], "Milliseconds"),
        "InputTokens": (r.get("input_tokens", 0), "Count"),
        "OutputTokens": (r.get("output_tokens", 0), "Count"),
        "CostUSD": (r["cost_usd"], "None"),
        "ToolCalls": (len(r["tool_calls"]), "Count"),
        "Handoff": (1 if r["handoff"] else 0, "Count"),
        "FallbackModel": (1 if r["fallback_used"] else 0, "Count"),
        "Errors": (1 if "llm_unavailable" in r["flags"] else 0, "Count"),
        "UngroundedAnswer": (1 if any(f.startswith("ungrounded") for f in r["flags"]) else 0, "Count"),
    })
