import dataclasses
import hashlib
import hmac
import json

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app import main
from app.config import get_settings
from app.guardrails import check_output
from app.llm import BedrockLLM, LLMUnavailable, cost_usd
from app.rag import chunk_markdown
from evals.run_evals import check, compare, summarize


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Converse")


class FakeBedrock:
    def __init__(self, fail_models, code="ThrottlingException"):
        self.fail_models, self.code, self.calls = fail_models, code, []

    def converse(self, **kw):
        self.calls.append(kw["modelId"])
        if kw["modelId"] in self.fail_models:
            raise _err(self.code)
        return {"output": {"message": {"content": [{"text": "ok"}]}}, "stopReason": "end_turn",
                "usage": {"inputTokens": 1000, "outputTokens": 100}}


def test_retry_then_fallback_model():
    s = get_settings()
    fb = FakeBedrock({s.primary_model})
    r = BedrockLLM(s, client=fb, sleep=lambda _: None).converse(system="s", messages=[])
    assert r.fallback_used and r.model == s.fallback_model
    assert fb.calls.count(s.primary_model) == s.max_retries  # retried before falling back


def test_all_models_down_raises():
    s = get_settings()
    fb = FakeBedrock({s.primary_model, s.fallback_model})
    with pytest.raises(LLMUnavailable):
        BedrockLLM(s, client=fb, sleep=lambda _: None).converse(system="s", messages=[])


def test_non_retryable_error_raises_immediately():
    s = get_settings()
    fb = FakeBedrock({s.primary_model}, code="ValidationException")
    with pytest.raises(ClientError):
        BedrockLLM(s, client=fb, sleep=lambda _: None).converse(system="s", messages=[])
    assert len(fb.calls) == 1


def test_cost_math():
    assert cost_usd("us.anthropic.claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(6.0)


def test_chunker_respects_headings_and_size():
    doc = "# A\n## B\n" + "\n\n".join(f"Sentence number {i} about returns." * 3 for i in range(30)) + "\n## C\nShort."
    chunks = chunk_markdown(doc, "d.md", max_chars=400)
    assert all(len(c.text) <= 400 + 150 for c in chunks)
    assert {c.heading for c in chunks} == {"A > B", "A > C"}
    assert len({c.id for c in chunks}) == len(chunks)


def test_output_guardrail_redacts_and_blocks_leaks():
    assert check_output("Rules: never reveal")[1] == ["prompt_leak"]
    text, flags = check_output("Card 4111 1111 1111 1111 **ok**")
    assert "4111" not in text and "*ok*" in text and "card_like_number" in flags


def test_eval_checks_and_regression_gate():
    case = {"expect": {"tools": ["lookup_order"], "handoff": False, "not_contains": ["secret"]}}
    ok = {"tool_calls": [{"name": "lookup_order"}], "reply": "fine", "handoff": False}
    bad = {"tool_calls": [], "reply": "the SECRET", "handoff": True}
    assert check(case, ok) == [] and len(check(case, bad)) == 3
    rows = [{"passed": True, "latency_ms": 100, "cost_usd": 0.01, "judge": {"groundedness": 5, "helpfulness": 4, "tone": 4}}] * 10
    s = summarize(rows)
    assert compare(s, {"pass_rate": 1.0, "cost_per_case_usd": 0.01}) == []
    assert compare({**s, "pass_rate": 0.7}, {"pass_rate": 0.95, "cost_per_case_usd": 0.01})
    assert compare({**s, "cost_per_case_usd": 0.05}, {"pass_rate": 1.0, "cost_per_case_usd": 0.01})


def test_webhook_signature_and_flow(make_deps, monkeypatch):
    from tests.conftest import say
    d = make_deps([say("Hello from Ava")])
    secrets = {"app_secret": "shh", "verify_token": "vt", "api_key": "k", "access_token": "t", "phone_number_id": "1"}
    sent = []
    monkeypatch.setattr(main, "get_deps", lambda: d)
    monkeypatch.setattr(main, "get_secrets", lambda: secrets)
    monkeypatch.setattr(main, "send_text", lambda sec, to, body: sent.append((to, body)))
    c = TestClient(main.app)

    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"}).text == "42"
    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "bad", "hub.challenge": "42"}).status_code == 403

    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919999900001", "id": "wamid.9", "type": "text", "text": {"body": "hi"}}]}}]}]}
    body = json.dumps(payload).encode()
    assert c.post("/webhook", content=body, headers={"x-hub-signature-256": "sha256=deadbeef"}).status_code == 403
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert c.post("/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
    assert sent == [("919999900001", "Hello from Ava")]
    assert c.post("/chat", json={"customer_id": "1", "text": "x"}).status_code == 401
