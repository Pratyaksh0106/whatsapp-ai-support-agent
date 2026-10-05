"""RAG: heading-aware chunking, Titan embeddings, pgvector (Aurora Data API) or in-memory search."""
from __future__ import annotations
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from .config import Settings


@dataclass
class Chunk:
    id: str
    source: str
    heading: str
    text: str

    @property
    def embed_text(self) -> str:
        # Prefixing the heading path gives short chunks ("30 days") the context they need to match queries.
        return f"{self.source} | {self.heading}\n{self.text}"


def _units(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    return [s for s in re.split(r"(?<=[.!?])\s+", paragraph) if s]


def chunk_markdown(text: str, source: str, max_chars: int = 900, overlap: int = 120) -> list[Chunk]:
    """Split on markdown headings first (semantic boundaries), then pack paragraphs up to max_chars,
    carrying `overlap` chars of the previous chunk forward so answers that straddle a boundary survive."""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            sections.append((" > ".join(h for _, h in stack), body))

    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            flush()
            buf.clear()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
        else:
            buf.append(line)
    flush()

    chunks: list[Chunk] = []
    for heading, body in sections:
        units = [u for p in re.split(r"\n\s*\n", body) if p.strip() for u in _units(p.strip(), max_chars)]
        cur = ""
        pieces: list[str] = []
        for u in units:
            if cur and len(cur) + len(u) + 2 > max_chars:
                pieces.append(cur)
                tail = cur[-overlap:]
                tail = tail[tail.find(" ") + 1:] if " " in tail else tail  # don't start mid-word
                cur = f"{tail}\n\n{u}"
            else:
                cur = f"{cur}\n\n{u}" if cur else u
        if cur:
            pieces.append(cur)
        for i, p in enumerate(pieces):
            cid = hashlib.sha1(f"{source}|{heading}|{i}".encode()).hexdigest()[:16]
            chunks.append(Chunk(cid, source, heading, p))
    return chunks


# ---------- embedders ----------
class BedrockEmbedder:
    def __init__(self, s: Settings, client=None):
        self.s = s
        self.c = client or boto3.client("bedrock-runtime", region_name=s.region)

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text[:8000], "dimensions": self.s.embed_dim, "normalize": True})
        for attempt in range(4):
            try:
                r = self.c.invoke_model(modelId=self.s.embed_model, body=body)
                return json.loads(r["body"].read())["embedding"]
            except ClientError as e:
                if e.response["Error"]["Code"] != "ThrottlingException" or attempt == 3:
                    raise
                time.sleep(0.5 * 2 ** attempt)
        raise RuntimeError("unreachable")


class HashEmbedder:
    """Deterministic bag-of-words embedder. Offline tests only; never use it for real retrieval quality."""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


# ---------- vector stores ----------
class MemoryVectorStore:
    def __init__(self):
        self.rows: list[tuple[Chunk, list[float]]] = []

    def init(self, dim: int): pass

    def replace_source(self, source: str, rows: list[tuple[Chunk, list[float]]]):
        self.rows = [r for r in self.rows if r[0].source != source] + rows

    def search(self, vec: list[float], k: int) -> list[dict]:
        scored = [(sum(a * b for a, b in zip(vec, v)), c) for c, v in self.rows]
        scored.sort(key=lambda x: -x[0])
        return [{"source": c.source, "heading": c.heading, "text": c.text, "score": s} for s, c in scored[:k]]


class PgVectorStore:
    """pgvector on Aurora Serverless v2 via the RDS Data API (no VPC attachment / connection pooling needed in Lambda)."""

    def __init__(self, s: Settings, client=None):
        self.s = s
        self.c = client or boto3.client("rds-data", region_name=s.region)

    def _exec(self, sql: str, params: list | None = None):
        for attempt in range(8):
            try:
                return self.c.execute_statement(
                    resourceArn=self.s.db_cluster_arn, secretArn=self.s.db_secret_arn,
                    database=self.s.db_name, sql=sql, parameters=params or [])
            except ClientError as e:
                if "resum" in str(e).lower():  # Aurora auto-paused (min ACU = 0); wait for it to wake
                    time.sleep(min(2 + attempt * 2, 10))
                    continue
                raise
        raise RuntimeError("database did not resume in time")

    def init(self, dim: int):
        self._exec("CREATE EXTENSION IF NOT EXISTS vector")
        self._exec(f"""CREATE TABLE IF NOT EXISTS kb_chunks (
            id text PRIMARY KEY, source text, heading text, content text, embedding vector({dim}))""")
        self._exec("CREATE INDEX IF NOT EXISTS kb_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops)")

    @staticmethod
    def _lit(vec: list[float]) -> str:
        return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"

    def replace_source(self, source: str, rows):
        self._exec("DELETE FROM kb_chunks WHERE source = :s", [{"name": "s", "value": {"stringValue": source}}])
        for chunk, vec in rows:
            self._exec(
                "INSERT INTO kb_chunks (id, source, heading, content, embedding) "
                "VALUES (:id, :s, :h, :c, CAST(:e AS vector))",
                [{"name": "id", "value": {"stringValue": chunk.id}},
                 {"name": "s", "value": {"stringValue": chunk.source}},
                 {"name": "h", "value": {"stringValue": chunk.heading}},
                 {"name": "c", "value": {"stringValue": chunk.text}},
                 {"name": "e", "value": {"stringValue": self._lit(vec)}}])

    def search(self, vec: list[float], k: int) -> list[dict]:
        r = self._exec(
            "SELECT source, heading, content, 1 - (embedding <=> CAST(:q AS vector)) AS score "
            "FROM kb_chunks ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k",
            [{"name": "q", "value": {"stringValue": self._lit(vec)}},
             {"name": "k", "value": {"longValue": k}}])
        out = []
        for rec in r.get("records", []):
            vals = [next(iter(f.values())) for f in rec]
            out.append({"source": vals[0], "heading": vals[1], "text": vals[2], "score": float(vals[3])})
        return out


class Retriever:
    def __init__(self, embedder, vstore, k: int, min_score: float):
        self.embedder, self.vstore, self.k, self.min_score = embedder, vstore, k, min_score

    def search(self, query: str) -> dict:
        hits = self.vstore.search(self.embedder.embed(query), self.k)
        hits = [h for h in hits if h["score"] >= self.min_score]
        return {
            "confidence": "high" if hits else "low",
            "results": [{"source": h["source"], "section": h["heading"], "text": h["text"],
                         "score": round(h["score"], 3)} for h in hits],
        }


def ingest_kb(vstore, embedder, kb_dir: str | Path) -> int:
    n = 0
    for path in sorted(Path(kb_dir).glob("*.md")):
        chunks = chunk_markdown(path.read_text(), path.name)
        vstore.replace_source(path.name, [(c, embedder.embed(c.embed_text)) for c in chunks])
        n += len(chunks)
    return n
