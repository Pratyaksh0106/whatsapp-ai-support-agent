"""Tools exposed to the model, plus the shared escalation routine."""
from __future__ import annotations
import time
import uuid

import boto3

from .observability import event, hash_id

TOOL_SPECS = [
    {"toolSpec": {
        "name": "search_knowledge_base",
        "description": "Search Acme Gadgets policies (shipping, returns, refunds, warranty, payments, cancellation). "
                       "Use for ANY policy or how-it-works question. Returns confidence=low if nothing relevant exists.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "query": {"type": "string", "description": "A COMPLETE natural-language question in English, "
                      "e.g. 'What is the return window for products?' or 'How long does shipping take?'. "
                      "Always a full question - never bare keywords like 'return window'."}},
            "required": ["query"]}}}},
    {"toolSpec": {
        "name": "lookup_order",
        "description": "Get live status, carrier, tracking id and ETA for one of THIS customer's orders.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "order_id": {"type": "string", "description": "Order id like ORD-1001"}},
            "required": ["order_id"]}}}},
    {"toolSpec": {
        "name": "create_ticket",
        "description": "Open a support ticket for issues needing follow-up (damaged/missing/wrong item, refund requests).",
        "inputSchema": {"json": {"type": "object", "properties": {
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "order_id": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]}},
            "required": ["subject", "description"]}}}},
    {"toolSpec": {
        "name": "escalate_to_human",
        "description": "Hand the conversation to a human agent. Use when the customer is upset, you lack a reliable "
                       "answer, money/legal disputes arise, or you cannot resolve the issue.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "reason": {"type": "string"},
            "summary": {"type": "string", "description": "2-line summary for the human agent"}},
            "required": ["reason"]}}}},
]


def do_escalate(settings, store, customer_id: str, reason: str, summary: str = "") -> str:
    """Creates a high-priority ticket and notifies humans via SNS. Used by the tool AND by hard guardrails."""
    ticket_id = "T-" + uuid.uuid4().hex[:8].upper()
    store.create_ticket({"ticket_id": ticket_id, "phone": customer_id, "subject": f"HANDOFF: {reason}",
                         "description": summary or reason, "priority": "high", "status": "open",
                         "created_at": int(time.time())})
    if settings.handoff_topic_arn:
        try:
            boto3.client("sns", region_name=settings.region).publish(
                TopicArn=settings.handoff_topic_arn, Subject=f"WhatsApp handoff {ticket_id}",
                Message=f"Customer: {customer_id}\nTicket: {ticket_id}\nReason: {reason}\nSummary: {summary}")
        except Exception as e:  # never let a notification failure break the customer reply
            event("handoff_notify_failed", error=str(e))
    event("handoff", customer=hash_id(customer_id), reason=reason, ticket=ticket_id)
    return ticket_id


def _search_kb(args, ctx):
    res = ctx.retriever.search(str(args["query"]))
    if res["confidence"] == "low":
        ctx.low_conf_hits += 1
        res["instruction"] = ("No reliable answer exists in the knowledge base. Do NOT guess. Tell the customer you'll "
                              "connect a teammate and call escalate_to_human.")
    return res


def _lookup_order(args, ctx):
    order = ctx.store.get_order(str(args["order_id"]).strip().upper())
    # Authorization guardrail: a customer may only see orders placed with their own WhatsApp number.
    if not order or order.get("phone") != ctx.customer_id:
        return {"found": False, "message": "No order with that id was found for this phone number."}
    return {"found": True, **{k: v for k, v in order.items() if k != "phone"}}


def _create_ticket(args, ctx):
    priority = args.get("priority", "normal")
    ticket_id = "T-" + uuid.uuid4().hex[:8].upper()
    ctx.store.create_ticket({
        "ticket_id": ticket_id, "phone": ctx.customer_id, "subject": str(args["subject"])[:120],
        "description": str(args["description"])[:1000], "order_id": args.get("order_id"),
        "priority": priority if priority in ("low", "normal", "high") else "normal",
        "status": "open", "created_at": int(time.time())})
    return {"ticket_id": ticket_id, "status": "open", "sla": "A teammate will respond within 24 hours."}


def _escalate(args, ctx):
    ctx.handoff = True
    ctx.handoff_reason = str(args.get("reason", "agent_requested"))[:200]
    tid = do_escalate(ctx.settings, ctx.store, ctx.customer_id, ctx.handoff_reason, str(args.get("summary", "")))
    return {"status": "escalated", "ticket_id": tid}


HANDLERS = {"search_knowledge_base": _search_kb, "lookup_order": _lookup_order,
            "create_ticket": _create_ticket, "escalate_to_human": _escalate}


def dispatch(name: str, args: dict, ctx) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        out = {"error": f"unknown tool: {name}"}
    else:
        try:
            out = handler(args or {}, ctx)
        except KeyError as e:
            out = {"error": f"missing required argument: {e}"}
        except Exception as e:
            event("tool_failed", tool=name, error=repr(e))
            out = {"error": "tool_failed"}
    ctx.tool_log.append({"name": name, "input": args, "output": out})
    return out
