from __future__ import annotations
import os
from dataclasses import dataclass
from functools import lru_cache


def _e(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    region: str
    primary_model: str
    fallback_model: str
    judge_model: str
    embed_model: str
    embed_dim: int
    embed_backend: str   # bedrock | hash (hash = offline/test only)
    store_backend: str   # dynamo | memory
    vector_backend: str  # pgvector | memory
    table_name: str
    db_cluster_arn: str
    db_secret_arn: str
    db_name: str
    secrets_arn: str
    handoff_topic_arn: str
    namespace: str
    service: str
    max_tool_iterations: int
    max_retries: int
    retrieval_top_k: int
    min_score: float
    rate_limit_per_min: int
    daily_budget_usd: float


@lru_cache
def get_settings() -> Settings:
    return Settings(
        region=_e("AWS_REGION", _e("AWS_DEFAULT_REGION", "us-east-1")),
        primary_model=_e("PRIMARY_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        fallback_model=_e("FALLBACK_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        judge_model=_e("JUDGE_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        embed_model=_e("EMBED_MODEL", "amazon.titan-embed-text-v2:0"),
        embed_dim=int(_e("EMBED_DIM", "1024")),
        embed_backend=_e("EMBED_BACKEND", "bedrock"),
        store_backend=_e("STORE_BACKEND", "dynamo"),
        vector_backend=_e("VECTOR_BACKEND", "pgvector"),
        table_name=_e("TABLE_NAME", "wa-agent"),
        db_cluster_arn=_e("DB_CLUSTER_ARN"),
        db_secret_arn=_e("DB_SECRET_ARN"),
        db_name=_e("DB_NAME", "agent"),
        secrets_arn=_e("APP_SECRETS_ARN"),
        handoff_topic_arn=_e("HANDOFF_TOPIC_ARN"),
        namespace=_e("METRIC_NAMESPACE", "WhatsAppAgent"),
        service=_e("SERVICE_NAME", "wa-agent"),
        max_tool_iterations=int(_e("MAX_TOOL_ITERATIONS", "5")),
        max_retries=int(_e("LLM_MAX_RETRIES", "3")),
        retrieval_top_k=int(_e("RETRIEVAL_TOP_K", "4")),
        min_score=float(_e("RETRIEVAL_MIN_SCORE", "0.35")),
        rate_limit_per_min=int(_e("RATE_LIMIT_PER_MIN", "10")),
        daily_budget_usd=float(_e("DAILY_BUDGET_USD", "20")),
    )
