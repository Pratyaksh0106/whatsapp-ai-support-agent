import dataclasses

import pytest

from app.config import get_settings
from app.deps import KB_DIR, Deps
from app.llm import LLMResponse
from app.rag import HashEmbedder, MemoryVectorStore, Retriever, ingest_kb
from app.seed import seed_orders
from app.store import MemoryStore


class ScriptedLLM:
    """Plays back a fixed list of LLMResponses so the tool loop can be tested without Bedrock."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def converse(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def tool_use(name, args, tid="tu1"):
    return LLMResponse("tool_use", [{"toolUse": {"toolUseId": tid, "name": name, "input": args}}],
                       input_tokens=100, output_tokens=20, cost_usd=0.0002)


def say(text):
    return LLMResponse("end_turn", [{"text": text}], input_tokens=100, output_tokens=30, cost_usd=0.0003)


@pytest.fixture
def make_deps():
    def _make(script, **overrides):
        base = dict(store_backend="memory", vector_backend="memory", embed_backend="hash",
                    handoff_topic_arn="", min_score=0.1)
        s = dataclasses.replace(get_settings(), **{**base, **overrides})
        store = MemoryStore()
        seed_orders(store)
        vs, emb = MemoryVectorStore(), HashEmbedder()
        ingest_kb(vs, emb, KB_DIR)
        return Deps(s, store, ScriptedLLM(script), Retriever(emb, vs, s.retrieval_top_k, s.min_score))
    return _make
