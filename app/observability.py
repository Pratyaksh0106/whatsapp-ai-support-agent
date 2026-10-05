"""Structured JSON logs + CloudWatch Embedded Metric Format (EMF).

EMF = print a specially-shaped JSON line to stdout; in Lambda, CloudWatch turns it into
real metrics with zero SDK calls and zero extra latency.
"""
from __future__ import annotations
import hashlib
import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {"level": record.levelname, "msg": record.getMessage(), "ts": round(time.time(), 3)}
        base.update(getattr(record, "fields", {}) or {})
        return json.dumps(base, default=str)


def _logger() -> logging.Logger:
    lg = logging.getLogger("agent")
    if not lg.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg


log = _logger()


def event(name: str, **fields) -> None:
    log.info(name, extra={"fields": fields})


def hash_id(value: str) -> str:
    """Never log raw phone numbers; a stable short hash still lets you follow a conversation."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def emit_metrics(namespace: str, service: str, metrics: dict[str, tuple[float, str]]) -> None:
    doc = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": namespace,
                "Dimensions": [["Service"]],
                "Metrics": [{"Name": n, "Unit": u} for n, (_, u) in metrics.items()],
            }],
        },
        "Service": service,
    }
    doc.update({n: v for n, (v, _) in metrics.items()})
    print(json.dumps(doc), flush=True)
