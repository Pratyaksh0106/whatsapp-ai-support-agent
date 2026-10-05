from __future__ import annotations
import json
import os
from functools import lru_cache

import boto3
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from mangum import Mangum
from pydantic import BaseModel

from .config import get_settings
from .deps import build_deps
from .observability import event
from .service import handle_message
from .whatsapp import extract_messages, send_text, verify_signature

app = FastAPI(title="WhatsApp Support Agent")
UNSUPPORTED = "I can read text messages for now. Could you type your question?"


@lru_cache
def get_deps():
    return build_deps()


@lru_cache
def get_secrets() -> dict:
    s = get_settings()
    if s.secrets_arn:
        raw = boto3.client("secretsmanager", region_name=s.region).get_secret_value(SecretId=s.secrets_arn)
        return json.loads(raw["SecretString"])
    return {k: os.environ.get(k.upper(), "") for k in
            ("verify_token", "app_secret", "access_token", "phone_number_id", "api_key")}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/webhook")
def verify(mode: str = Query(None, alias="hub.mode"), token: str = Query(None, alias="hub.verify_token"),
           challenge: str = Query(None, alias="hub.challenge")):
    if mode == "subscribe" and token and token == get_secrets().get("verify_token"):
        return PlainTextResponse(challenge or "")
    raise HTTPException(403, "verification failed")


@app.post("/webhook")
async def webhook(request: Request, x_hub_signature_256: str | None = Header(None)):
    body = await request.body()
    secrets = get_secrets()
    if not verify_signature(secrets.get("app_secret", ""), body, x_hub_signature_256):
        raise HTTPException(403, "bad signature")
    deps = get_deps()
    for m in extract_messages(json.loads(body)):
        try:
            if m["text"] is None:
                reply = UNSUPPORTED
            else:
                reply = handle_message(deps, m["from"], m["text"], m["id"]).get("reply")
            if reply:
                send_text(secrets, m["from"], reply)
        except Exception as e:  # always 200 once authenticated, otherwise Meta retries and we double-reply
            event("webhook_error", error=repr(e))
    return {"status": "ok"}


class ChatIn(BaseModel):
    customer_id: str
    text: str


class ResetIn(BaseModel):
    customer_id: str


@app.post("/chat")
def chat(body: ChatIn, x_api_key: str | None = Header(None)):
    """Demo/testing endpoint (no WhatsApp needed): curl -H 'x-api-key: ...' -d '{"customer_id":"919999900001","text":"..."}'"""
    key = get_secrets().get("api_key")
    if not key or x_api_key != key:
        raise HTTPException(401, "invalid api key")
    r = handle_message(get_deps(), body.customer_id, body.text)
    return {k: r.get(k) for k in ("reply", "handoff", "handoff_reason", "latency_ms", "cost_usd", "flags")} | {
        "tools": [t["name"] for t in r.get("tool_calls", [])]}


@app.post("/reset")
def reset(body: ResetIn, x_api_key: str | None = Header(None)):
    """Demo helper: clear a customer's conversation + handoff state so a thread can be replayed."""
    from .store import _blank_state
    key = get_secrets().get("api_key")
    if not key or x_api_key != key:
        raise HTTPException(401, "invalid api key")
    get_deps().store.put_state(body.customer_id, _blank_state())
    return {"status": "reset", "customer_id": body.customer_id}


handler = Mangum(app, lifespan="off")
