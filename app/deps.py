from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, get_settings
from .llm import BedrockLLM
from .rag import BedrockEmbedder, HashEmbedder, MemoryVectorStore, PgVectorStore, Retriever, ingest_kb
from .store import DynamoStore, MemoryStore

KB_DIR = Path(__file__).resolve().parent.parent / "kb"


@dataclass
class Deps:
    settings: Settings
    store: object
    llm: object
    retriever: Retriever


def build_deps(settings: Settings | None = None) -> Deps:
    s = settings or get_settings()
    store = DynamoStore(s.table_name, s.region) if s.store_backend == "dynamo" else MemoryStore()
    embedder = BedrockEmbedder(s) if s.embed_backend == "bedrock" else HashEmbedder()
    if s.vector_backend == "pgvector":
        vstore = PgVectorStore(s)
    else:
        vstore = MemoryVectorStore()
        ingest_kb(vstore, embedder, KB_DIR)  # local/eval mode: index the KB at startup
    return Deps(s, store, BedrockLLM(s), Retriever(embedder, vstore, s.retrieval_top_k, s.min_score))
