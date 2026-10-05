# WhatsApp AI Support Agent on AWS (RAG + tool calling + evals + observability)

Two projects in one repo:

1. **The agent**: WhatsApp webhook -> guardrails -> Claude (Bedrock Converse) tool-calling loop -> RAG over pgvector -> reply.
2. **The reliability layer**: golden-set evals with LLM-as-judge, regression gate in CI, CloudWatch metrics/dashboard/alarms, retries, model fallback, rate limits and a daily spend cap.

```
WhatsApp Cloud API ──► Lambda Function URL ──► FastAPI (Mangum)
                          (HMAC-verified)         │
                                    dedupe ─ mute-if-human ─ input guardrails ─ rate limit ─ budget cap
                                                  │
                                       Bedrock Converse tool loop (Claude Haiku, falls back to Sonnet)
                          ┌───────────────┬───────┴────────┬──────────────────┐
                  search_knowledge_base  lookup_order   create_ticket   escalate_to_human
                  Titan embeddings →     DynamoDB       DynamoDB        DynamoDB ticket + SNS email
                  pgvector (Aurora       (owner-checked)
                  Serverless v2, Data API)
                                                  │
                                 output guardrails ─ grounding check ─ reply via WhatsApp
                                                  │
                            JSON logs + EMF metrics ─► CloudWatch dashboard + alarms
```

## Design choices worth talking about
- **Tool loop** (`app/agent.py`): max 5 iterations; malformed or runaway loops hand off to a human instead of spinning. Tool errors return structured JSON to the model rather than raising.
- **RAG as a tool, not a pre-step**: the model decides when to retrieve, so "hi" costs no embedding call. Chunking (`app/rag.py`): split on markdown headings first, pack paragraphs to ~900 chars, 120 char overlap, and prefix every chunk with its heading path so "30 days" still matches "return window". Cosine top-4 with a similarity floor (`RETRIEVAL_MIN_SCORE`, default 0.35 — tune it with the eval set). Below the floor the tool returns `confidence: low` and tells the model not to guess.
- **Guardrails** live outside the LLM (`app/guardrails.py`), so a clever prompt can't disable them: injection block, legal/fraud keyword handoff, order-ownership check (a customer can only see orders tied to their own number), card-number redaction, system-prompt leak block, and a numeric grounding heuristic that flags numbers not present in any tool output.
- **Handoff triggers**: model calls `escalate_to_human`; sensitive keywords; 2 consecutive low-confidence retrievals; tool-loop overrun; LLM outage; daily budget exhausted. After handoff the bot goes silent on that thread.
- **Reliability**: jittered exponential backoff on throttling/timeouts, then automatic fallback to a second model, then graceful human handoff. Webhook is idempotent (Meta retries). Per-customer rate limit and a global daily USD cap, both stored in DynamoDB so they hold across Lambda containers.
- **Aurora Data API** means the Lambda needs no VPC/NAT/connection pooling. Aurora min capacity is 0 (auto-pause), so the first query after idle waits for resume; `PgVectorStore` handles that.
- **MCP**: `mcp_server/order_server.py` exposes `lookup_order` and `create_ticket` over MCP (stdio), reusing the same store. Add it to Claude Desktop/Claude Code with the snippet in the file.

## Run locally (no AWS infra needed, only Bedrock access for the live parts)
```bash
pip install -r requirements-dev.txt
pytest -q                                   # 20 tests, no AWS needed
STORE_BACKEND=memory VECTOR_BACKEND=memory python -m evals.run_evals --judge   # needs Bedrock creds
STORE_BACKEND=memory python -m mcp_server.order_server                          # MCP server
```

## Deploy to AWS
Prereqs: AWS creds, Terraform >= 1.6, Python 3.12, and Bedrock model access for the Claude models + Titan Text Embeddings V2 in your region (Bedrock console > Model access).
```bash
./scripts/build_lambda.sh
cd terraform && terraform init && terraform apply -var alert_email=you@example.com
# put real secrets in (name from `terraform output app_secret_arn`):
aws secretsmanager put-secret-value --secret-id <app_secret_arn> --secret-string \
  '{"verify_token":"<any-string>","app_secret":"<Meta app secret>","access_token":"<WA token>","phone_number_id":"<id>","api_key":"<demo key>"}'
cd .. && export TABLE_NAME=wa-agent DB_CLUSTER_ARN=<out> DB_SECRET_ARN=<out> AWS_REGION=us-east-1
python -m scripts.seed && python -m scripts.ingest      # demo orders + KB -> pgvector
curl -X POST "$(terraform -chdir=terraform output -raw chat_url)" -H "x-api-key: <demo key>" \
  -H 'content-type: application/json' -d '{"customer_id":"919999900001","text":"Where is ORD-1001?"}'
```
Then in Meta for Developers > WhatsApp > Configuration set the callback URL to `terraform output webhook_url` and the verify token to your `verify_token`. Without a WhatsApp Business account you can still demo everything through `/chat`.

## Evals and CI
`evals/golden.jsonl` has 12 cases (policy, orders, Hinglish, wrong-owner order, damaged item, unknown topic, legal threat, prompt injection). Each is scored on deterministic checks (tools used/forbidden, handoff, required/forbidden text) plus an LLM judge (groundedness / helpfulness / tone, 1-5). `compare()` fails the build if pass-rate falls below 85% or drops >5 pts vs `evals/baseline.json`, judge average < 3.8, or cost per case grows >50%. **Run `python -m evals.run_evals --judge --update-baseline` once on your account** — the committed baseline is a placeholder. CI: unit tests + `terraform validate` on every PR; evals run when you set the repo variable `AWS_ROLE_ARN` (`terraform output ci_role_arn`, created with `-var github_repo=owner/repo`).

## Known limits (be upfront about these in an interview)
- Webhook processes synchronously. For production volume put SQS between the webhook and a worker Lambda so you ack Meta in <1s.
- Model IDs and per-token prices in `app/config.py` / `app/llm.py` are defaults; check them against your region's Bedrock catalog and pricing.
- The grounding check is a heuristic. The judge in the eval set is the stronger signal; wiring Langfuse or sampling production turns into the judge is the natural next step.
- Orders/tickets are demo DynamoDB items, not a real OMS integration.
