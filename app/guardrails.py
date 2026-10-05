"""Deterministic guardrails that run outside the LLM. Cheap, testable, and can't be talked out of."""
from __future__ import annotations
import re
from dataclasses import dataclass

INJECTION = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+|your\s+)*(previous|prior|above)?\s*(instructions|rules|prompt)"
    r"|system\s+prompt|developer\s+mode|you\s+are\s+now\s+(?!a\s+customer)", re.I)
HARD_HANDOFF = re.compile(
    r"\b(lawyer|legal action|legal notice|sue|consumer court|chargeback|police|fraud|scam|cheated)\b", re.I)
LEAK_MARKERS = ("Rules:", "search_knowledge_base", "escalate_to_human", "toolUse", "You are Ava")

BLOCK_REPLY = "I can only help with Acme Gadgets orders and support questions. What can I help you with?"
HANDOFF_REPLY = ("I'm sorry about this. I'm connecting you with a teammate who can help directly, "
                 "and they'll reply here soon.")


@dataclass
class GuardResult:
    action: str  # allow | block | handoff
    reason: str = ""
    reply: str = ""


def check_input(text: str) -> GuardResult:
    if INJECTION.search(text):
        return GuardResult("block", "prompt_injection", BLOCK_REPLY)
    if HARD_HANDOFF.search(text):
        return GuardResult("handoff", "sensitive_keyword", HANDOFF_REPLY)
    return GuardResult("allow")


def redact_pii(text: str) -> str:
    text = re.sub(r"\b(?:\d[ -]?){13,19}\b", "[card]", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    return re.sub(r"\+?\d[\d \-]{8,}\d", "[phone]", text)


def check_output(text: str) -> tuple[str, list[str]]:
    flags: list[str] = []
    # Some models (e.g. Nova) leak chain-of-thought as <thinking>...</thinking>; never show it to the customer.
    text = re.sub(r"(?is)<thinking>.*?</thinking>\s*", "", text)
    text = re.sub(r"(?i)</?thinking>", "", text).strip()
    if any(m in text for m in LEAK_MARKERS):
        flags.append("prompt_leak")
        return BLOCK_REPLY, flags
    if re.search(r"\b(?:\d[ -]?){13,19}\b", text):
        flags.append("card_like_number")
        text = re.sub(r"\b(?:\d[ -]?){13,19}\b", "[redacted]", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)  # WhatsApp bold is single-asterisk
    if len(text) > 1000:
        flags.append("truncated")
        text = text[:997] + "..."
    return text, flags


def ungrounded_numbers(reply: str, evidence: str) -> list[str]:
    """Cheap hallucination heuristic: numbers in the reply that appear in neither the tool outputs,
    retrieved KB text, nor the customer's own message. Not a proof; a monitoring signal."""
    ev = evidence.replace(",", "")
    found = {n.rstrip(".").replace(",", "") for n in re.findall(r"\d[\d,]*\.?\d*", reply)}
    return sorted(n for n in found if len(n) >= 2 and n not in ev)
