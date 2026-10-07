# Demonstration Runbook

Maps every scenario in the brief's section 5 to a command and the result to point at.
About 15 minutes end to end.

## 0. Before the demo

```bash
docker compose down -v && docker compose up --build -d   # clean data, matching the values below
docker compose ps                                        # wait until everything is "healthy"
bash scripts/smoke-test.sh                               # all PASS = ready
```

Have open: a terminal, http://localhost:8000/api/agent/docs (Swagger) and http://localhost:16686 (Jaeger).

> On Windows, run the `.sh` scripts in **Git Bash**. Use `python` instead of `python3` if needed.

## 1. The nine required scenarios

| # | Scenario (brief §5) | Capability | Command | What to point out |
|---|---|---|---|---|
| 1 | Retrieve a customer | Single API / capability | `python scripts/ask.py "Show customer C001"` | One step, `get_customer`, executed by the **Customer Agent** (found through its Agent Card) |
| 2 | Retrieve customer and latest order | Multiple API orchestration | `python scripts/ask.py "Show customer C001 and their latest order"` | Plan: `get_customer` → `get_latest_order`, the second **depends on** the first. Answer: O1006, SHIPPED |
| 3 | Retrieve an unknown customer | Error / no-data handling | `python scripts/ask.py "Show customer C999"` | `status=not_found`, "Customer C999 was not found." **Nothing invented.** Also: `curl -H "apikey: demo-client-key" localhost:8000/api/customers/C999` → 404 `CUSTOMER_NOT_FOUND` |
| 4 | Retrieve an order through agent delegation | A2A delegation | `python scripts/ask.py "What is the status of order O1001?"` | Delegated to the **Order Agent** as an A2A task (task ID in the steps). Direct A2A view: `bash scripts/a2a-demo.sh` |
| 5 | Retrieve customer and order through multiple agents | Multi-agent coordination | `python scripts/ask.py "Find customer C001 and tell me their latest order status"` | Two agents, coordinated in order; answer built only from both results |
| 6 | Backend or agent unavailable | Failure handling | `bash scripts/failure-demo.sh` (~2 min) | Service down → `partial`; hung service → timeouts then **circuit open** (instant answer); DB down; agent down → retried, then reported; recovery without restarts |
| 7 | Excessive API requests | Traffic management | Run 6× quickly: `curl -i -H "apikey: ratelimit-probe-key" localhost:8000/api/customers/C001` | Requests 1–5: 200 with `X-RateLimit-Remaining-Minute` counting down; then **429** with `Retry-After` |
| 8 | Unauthorized API request | API security | `curl -i localhost:8000/api/customers/C001` and `curl -i -H "apikey: chat-frontend-key" localhost:8000/api/customers/C001` | **401** without a key; **403** with a valid key that lacks permission (chat UI may only use the Agent API) |
| 9 | Trace one complete request | Observability | `python scripts/trace.py` | Span tree across gateway, coordinator, both agents, both services and the SQL queries; open the printed Jaeger link |

All of 1–5 and 7–9 in one go: `bash scripts/agent-demo.sh` and `bash scripts/smoke-test.sh`.
As an API collection: import `collections/comco-platform.postman_collection.json` into Postman
(or run `npx newman run collections/comco-platform.postman_collection.json`).

## 2. Suggested storyline for the key scenario

*"Find customer C001 and tell me their latest order status."*

1. **Ask it**: `python scripts/ask.py "Find customer C001 and tell me their latest order status"`.
   Read out the answer, then the *Reasoning* (the plan) and the *Steps* (which agent did what).
2. **Follow it**: `python scripts/trace.py`, then open the Jaeger link. Walk down the tree:
   Kong (auth, ACL, rate limit) → coordinator `plan` → `delegate get_customer` → Customer Agent
   `a2a.task` → its call **back through Kong** with its own key → Customer Service → SQL;
   then the same for the order.
3. **Break it**: `docker compose stop order-agent`, ask again → `partial`: customer shown,
   order "currently unavailable". In Jaeger the failed step is red. `docker compose start order-agent`.
4. **Explain the design choices** behind what they just saw: agents as API consumers,
   not-found vs failed, timeout budget, retries in one layer only.

## 3. Likely questions and where the answers are

| Question | Answer in |
|---|---|
| Why Kong / FastAPI / PostgreSQL / Jaeger? | `docs/DECISIONS.md` D1–D3, D8 |
| How is the API secured? Why API keys? What in production? | `docs/DECISIONS.md` D4; `docs/ARCHITECTURE.md` §4 |
| Why do agents go through the gateway? | `docs/ARCHITECTURE.md` §3; D7 |
| How does A2A work here? Why not the SDK? | `docs/AGENTS.md` §3; D6 |
| How do you prevent the agent from making things up? | `docs/AGENTS.md` §5 (not_found vs failed); D5 |
| What happens when X fails? | `docs/FAILURE-HANDLING.md` §1 |
| Why only one retry? Why a circuit breaker? | `docs/FAILURE-HANDLING.md` §3–4; D11 |
| How do you follow one request? | `docs/OBSERVABILITY.md` |
| What would you change for production? | `docs/ARCHITECTURE.md` §6, README → *Known limitations* |
| Why no LLM? | D5: deterministic by design; LLM as planner only is the next step |
