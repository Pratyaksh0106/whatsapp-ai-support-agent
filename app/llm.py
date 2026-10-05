"""Bedrock Converse wrapper: retries w/ jittered backoff, model fallback, token + cost accounting."""
from __future__ import annotations
import random
import time
from dataclasses import dataclass, field

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

from .config import Settings
from .observability import event

RETRYABLE_CODES = {
    "ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException",
    "InternalServerException", "ModelNotReadyException",
}
# USD per 1M tokens (input, output). Approximate list prices: verify against the Bedrock pricing page.
PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}


class LLMUnavailable(Exception):
    pass


def cost_usd(model_id: str, in_tok: int, out_tok: int) -> float:
    for key, (pin, pout) in PRICES.items():
        if key in model_id:
            return (in_tok * pin + out_tok * pout) / 1_000_000
    return 0.0


@dataclass
class LLMResponse:
    stop_reason: str
    content: list = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    fallback_used: bool = False


class BedrockLLM:
    def __init__(self, settings: Settings, client=None, sleep=time.sleep):
        self.s = settings
        self.sleep = sleep
        self.client = client or boto3.client(
            "bedrock-runtime", region_name=settings.region,
            config=Config(retries={"max_attempts": 1}, read_timeout=25, connect_timeout=3),
        )

    def converse(self, *, system: str, messages: list, tools: list | None = None,
                 max_tokens: int = 700, model: str | None = None) -> LLMResponse:
        models = [model or self.s.primary_model]
        if not model and self.s.fallback_model and self.s.fallback_model != models[0]:
            models.append(self.s.fallback_model)
        last_err: Exception | None = None
        for idx, m in enumerate(models):
            for attempt in range(self.s.max_retries):
                kwargs = {
                    "modelId": m,
                    "system": [{"text": system}],
                    "messages": messages,
                    "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
                }
                if tools:
                    kwargs["toolConfig"] = {"tools": tools}
                t0 = time.perf_counter()
                try:
                    resp = self.client.converse(**kwargs)
                    return self._parse(resp, m, (time.perf_counter() - t0) * 1000, idx > 0)
                except ClientError as e:
                    code = e.response.get("Error", {}).get("Code", "")
                    if code not in RETRYABLE_CODES:
                        raise
                    last_err = e
                except (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError) as e:
                    last_err = e
                delay = min(0.4 * 2 ** attempt, 4.0) * (0.5 + random.random() / 2)
                event("llm_retry", model=m, attempt=attempt + 1, error=type(last_err).__name__, delay_s=round(delay, 2))
                self.sleep(delay)
            event("llm_model_exhausted", model=m)
        raise LLMUnavailable(str(last_err))

    @staticmethod
    def _parse(resp: dict, model: str, latency_ms: float, fallback: bool) -> LLMResponse:
        usage = resp.get("usage", {})
        i, o = usage.get("inputTokens", 0), usage.get("outputTokens", 0)
        return LLMResponse(
            stop_reason=resp.get("stopReason", "end_turn"),
            content=resp["output"]["message"]["content"],
            model=model, input_tokens=i, output_tokens=o,
            cost_usd=cost_usd(model, i, o), latency_ms=latency_ms, fallback_used=fallback,
        )
