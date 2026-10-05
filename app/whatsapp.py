"""WhatsApp Cloud API helpers: signature verification, payload parsing, sending."""
from __future__ import annotations
import hashlib
import hmac

import httpx

GRAPH = "https://graph.facebook.com/v21.0"


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def extract_messages(payload: dict) -> list[dict]:
    """Returns [{from, id, text|None, type}] for every inbound message in a Cloud API webhook payload."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                out.append({"from": m.get("from"), "id": m.get("id"), "type": m.get("type"),
                            "text": m.get("text", {}).get("body") if m.get("type") == "text" else None})
    return out


def send_text(secrets: dict, to: str, body: str) -> None:
    r = httpx.post(
        f"{GRAPH}/{secrets['phone_number_id']}/messages",
        headers={"Authorization": f"Bearer {secrets['access_token']}"},
        json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}},
        timeout=10)
    r.raise_for_status()
