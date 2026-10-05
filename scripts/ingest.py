"""Chunk + embed kb/*.md and load into pgvector (Aurora Data API).  Usage: python -m scripts.ingest"""
from app.config import get_settings
from app.deps import KB_DIR
from app.rag import BedrockEmbedder, PgVectorStore, ingest_kb

s = get_settings()
vs = PgVectorStore(s)
vs.init(s.embed_dim)
print("chunks indexed:", ingest_kb(vs, BedrockEmbedder(s), KB_DIR))
