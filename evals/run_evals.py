"""Eval harness: golden set -> deterministic checks (+ optional LLM-as-judge) -> regression gate vs baseline.

  python -m evals.run_evals --judge                 # run + gate (exit 1 on regression)
  python -m evals.run_evals --judge --update-baseline
Runs the real agent against Bedrock with in-memory store/vector DB, so it needs AWS creds but no deployed infra.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import statistics
import sys
from pathlib import Path

os.environ.setdefault("STORE_BACKEND", "memory")
os.environ.setdefault("VECTOR_BACKEND", "memory")

HERE = Path(__file__).parent
MIN_PASS_RATE = 0.85      # absolute floor
MAX_PASS_DROP = 0.05      # allowed drop vs baseline
MIN_JUDGE_AVG = 3.8       # 1-5 scale
MAX_COST_GROWTH = 1.5     # cost/case may grow 50% vs baseline

JUDGE_SYSTEM = """You grade a customer-support agent reply. Return ONLY JSON:
{"groundedness": 1-5, "helpfulness": 1-5, "tone": 1-5, "reason": "<one sentence>"}
groundedness: 5 = every factual claim is supported by the EVIDENCE (tool outputs); 1 = invents facts. If no evidence was
needed (greeting/handoff), judge only whether the reply avoids unsupported claims.
helpfulness: did it resolve or correctly route the request? tone: warm, concise, WhatsApp-appropriate."""


def check(case: dict, result: dict) -> list[str]:
    """Deterministic assertions. Returns list of failure reasons (empty = pass)."""
    exp, fails = case["expect"], []
    called = [t["name"] for t in result["tool_calls"]]
    reply = (result["reply"] or "").lower()
    for t in exp.get("tools", []):
        if t not in called:
            fails.append(f"expected tool {t}, called {called}")
    for t in exp.get("no_tools", []):
        if t in called:
            fails.append(f"forbidden tool {t} was called")
    if exp.get("handoff") is not None and bool(result["handoff"]) != exp["handoff"]:
        fails.append(f"handoff={result['handoff']} expected {exp['handoff']}")
    if exp.get("contains_any") and not any(s.lower() in reply for s in exp["contains_any"]):
        fails.append(f"reply lacks any of {exp['contains_any']}")
    for s in exp.get("not_contains", []):
        if s.lower() in reply:
            fails.append(f"reply contains forbidden text {s!r}")
    return fails


def judge(llm, model: str, case: dict, result: dict) -> dict:
    evidence = json.dumps([t["output"] for t in result["tool_calls"]], ensure_ascii=False)[:3000]
    prompt = f"CUSTOMER: {case['input']}\nEVIDENCE: {evidence}\nREPLY: {result['reply']}"
    r = llm.converse(system=JUDGE_SYSTEM, messages=[{"role": "user", "content": [{"text": prompt}]}],
                     max_tokens=300, model=model)
    text = "".join(b.get("text", "") for b in r.content)
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def summarize(rows: list[dict]) -> dict:
    lat = sorted(r["latency_ms"] for r in rows)
    judged = [r["judge"] for r in rows if r.get("judge")]
    avg = lambda k: round(statistics.mean(j[k] for j in judged if k in j), 2) if judged else None
    return {
        "cases": len(rows),
        "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 3),
        "judge_avg": round(statistics.mean([avg("groundedness"), avg("helpfulness"), avg("tone")]), 2) if judged else None,
        "groundedness_avg": avg("groundedness"),
        "latency_p50_ms": round(lat[len(lat) // 2], 1),
        "latency_p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
        "cost_per_case_usd": round(sum(r["cost_usd"] for r in rows) / len(rows), 5),
    }


def compare(summary: dict, baseline: dict | None) -> list[str]:
    """Regression gate. Returns failure messages (empty = ship it)."""
    fails = []
    if summary["pass_rate"] < MIN_PASS_RATE:
        fails.append(f"pass_rate {summary['pass_rate']} < floor {MIN_PASS_RATE}")
    if summary["judge_avg"] is not None and summary["judge_avg"] < MIN_JUDGE_AVG:
        fails.append(f"judge_avg {summary['judge_avg']} < {MIN_JUDGE_AVG}")
    if baseline:
        if summary["pass_rate"] < baseline["pass_rate"] - MAX_PASS_DROP:
            fails.append(f"pass_rate regressed {baseline['pass_rate']} -> {summary['pass_rate']}")
        if baseline.get("cost_per_case_usd") and summary["cost_per_case_usd"] > baseline["cost_per_case_usd"] * MAX_COST_GROWTH:
            fails.append(f"cost/case grew {baseline['cost_per_case_usd']} -> {summary['cost_per_case_usd']}")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    from app.deps import build_deps
    from app.seed import seed_orders
    from app.service import handle_message
    from app.store import MemoryStore

    deps = build_deps()
    cases = [json.loads(l) for l in (HERE / "golden.jsonl").read_text().splitlines() if l.strip()]
    rows = []
    for c in cases:
        deps.store = MemoryStore()  # isolate every case: no shared handoff/mute state
        seed_orders(deps.store)
        r = handle_message(deps, c["customer"], c["input"])
        fails = check(c, r)
        row = {"id": c["id"], "passed": not fails, "fails": fails, "reply": r["reply"], "latency_ms": r["latency_ms"],
               "cost_usd": r["cost_usd"], "tools": [t["name"] for t in r["tool_calls"]], "flags": r["flags"]}
        if args.judge:
            row["judge"] = judge(deps.llm, deps.settings.judge_model, c, r)
        rows.append(row)
        print(("PASS " if row["passed"] else "FAIL ") + c["id"], "|", (r["reply"] or "")[:90].replace("\n", " "), flush=True)
        for f in fails:
            print("     -", f)

    summary = summarize(rows)
    print("\n" + json.dumps(summary, indent=2))
    (HERE / "results.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=2, ensure_ascii=False))
    base_path = HERE / "baseline.json"
    if args.update_baseline:
        base_path.write_text(json.dumps(summary, indent=2))
        print("baseline updated")
        return 0
    failures = compare(summary, json.loads(base_path.read_text()) if base_path.exists() else None)
    for f in failures:
        print("REGRESSION:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
