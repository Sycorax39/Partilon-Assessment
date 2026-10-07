# ComCo Customer Service Integration Platform

Prototype integration platform demonstrating REST API design, API management, agentic API
consumption, Agent-to-Agent (A2A) communication, observability and containerized deployment.

> **Status:** Step 7 — full flow with failure handling and end-to-end distributed tracing
> (OpenTelemetry → Jaeger). Remaining: final documentation pass (see [Progress](#progress)).

## Architecture (current)

```mermaid
flowchart TD
    client[Client / User] -->|":8000"| gw

    subgraph platform[Docker network: internal]
        gw["API Gateway (Kong OSS)<br/>auth · ACL · rate limit · routing<br/>correlation ID · logging · metrics · tracing"]
        gw -->|/api/customers| cs[Customer Service]
        gw -->|/api/orders| os[Order Service]
        gw -->|/api/agent| co[Coordinator Agent]
        cs --> cdb[(customer-db)]
        os --> odb[(order-db)]
        co -.A2A.-> ca[Customer Agent]
        co -.A2A.-> oa[Order Agent]
        ca -.via gateway.-> gw
        oa -.via gateway.-> gw
        jg[(Jaeger<br/>traces)]
    end
    gw & co & ca & oa & cs & os -. OTLP spans .-> jg
```

Only the gateway (port 8000) is published to the host. Services, databases and agents are on
the internal network, so external consumers can reach backend capabilities only through API
management. Agents also call the backend APIs *through the gateway* (see `docs/DECISIONS.md`, D7).

## Technology stack

| Concern | Choice |
|---|---|
| API gateway | Kong Gateway OSS 3.9 (DB-less, declarative `gateway/kong.yml`) |
| Backend services | Python 3.12 + FastAPI, psycopg 3 with connection pooling |
| Data stores | PostgreSQL 16, one instance per service |
| Agents | Custom Python, deterministic planner |
| Agent-to-Agent | A2A protocol v0.3 (Agent Cards, JSON-RPC `message/send`, task status), `libs/a2a_core` |
| Observability | OpenTelemetry traces → Jaeger 2; JSON logs with correlation + trace IDs; Kong Prometheus metrics |
| Deployment | Docker Compose |

Reasons for each choice: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Repository layout

```
gateway/kong.yml              API gateway configuration (routes, plugins)
libs/common/                  Shared conventions: error format, correlation ID, JSON logs,
                              DB pool, managed-API client (agents -> gateway), retry + circuit breaker,
                              OpenTelemetry tracing
libs/a2a_core/                A2A protocol: Agent Card, tasks, JSON-RPC server/client, CLI
services/customer-service/    Customer REST API — app/ (code), db/ (schema + seed)
services/order-service/       Order REST API    — app/ (code), db/ (schema + seed)
agents/coordinator/           Coordinator Agent — the Agent API: plan, discover, delegate, answer
agents/customer-agent/        Customer Agent    — internal, A2A, skills over the Customer API
agents/order-agent/           Order Agent       — internal, A2A, skills over the Order API
docs/openapi/                 Exported OpenAPI 3.1 specs
docs/AGENTS.md                Agent architecture and A2A interaction
docs/FAILURE-HANDLING.md      Failure matrix, timeout budget, retries, circuit breaker
docs/OBSERVABILITY.md         Tracing, logs, metrics: following one request end to end
docs/DECISIONS.md             Architecture & technology decisions
scripts/                      Smoke test, agent / A2A / failure demos, ask.py + trace.py, OpenAPI export
tests/                        Automated tests (pytest): APIs, gateway, agents
```

## Running it

Prerequisites: Docker Desktop (or Docker Engine + Compose v2), `curl`, `bash`
(on Windows use Git Bash or WSL for the `.sh` scripts).

```bash
docker compose up --build -d     # build and start everything
docker compose ps                # all services should become "healthy"
./scripts/smoke-test.sh          # end-to-end check through the gateway
docker compose logs -f order-service   # structured JSON logs, one line per request
docker compose down              # stop   (add -v to also delete database data)
```

No configuration is needed; defaults live in `docker-compose.yml`. Each service creates its
schema and loads demo data on startup (idempotent, safe to restart). To override database
credentials, copy `.env.example` to `.env`.

### Interactive API docs (Swagger UI)

- Customer API: http://localhost:8000/api/customers/docs
- Order API: http://localhost:8000/api/orders/docs

The docs pages are public. To use "Try it out", click **Authorize** and enter an API key,
e.g. `demo-client-key`.

### API keys (development only)

Every request to an API must carry an `apikey` header. Consumers are defined in
`gateway/kong.yml`:

| Key | Consumer | Can call | Rate limit |
|---|---|---|---|
| `demo-client-key` | External application | Customer, Order, Agent APIs (read + write) | 20/min |
| `chat-frontend-key` | Chat UI | Agent API only | 30/min |
| `customer-agent-key` | Customer Agent | Customer API, read only | 300/min |
| `order-agent-key` | Order Agent | Order API, read only | 300/min |
| `test-runner-key` | Automated tests | Everything | 1000/min |
| `ratelimit-probe-key` | Rate-limit demo | Everything | 5/min |

Gateway responses: **401** missing/invalid key, **403** not allowed for this consumer,
**429** quota exceeded (see `Retry-After` and `X-RateLimit-*` headers), **413** body over 1 MB.

### Tracing (Jaeger)

```bash
python scripts/trace.py          # key scenario -> span tree across all components + Jaeger link
```

Jaeger UI: http://localhost:16686. Every response from the gateway carries `X-Trace-Id`; the
Agent API also returns `trace_id`. How it works: [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md).

### Logs and metrics

```bash
docker compose logs -f gateway          # one JSON line per request: consumer, route, status, latencies
docker compose logs -f customer-service # service logs, same X-Correlation-ID
curl -s http://localhost:8100/metrics | grep kong_http_requests_total   # Prometheus metrics
```

### Asking the agents (Agent API)

```bash
python scripts/ask.py "Find customer C001 and tell me their latest order status"
bash scripts/agent-demo.sh       # all agent scenarios from the brief, plus a trace
```

```bash
curl -X POST http://localhost:8000/api/agent/query -H "apikey: chat-frontend-key" \
     -H "Content-Type: application/json" -d '{"query": "Show customer C001 and their latest order"}'
```

Swagger UI for the Agent API: http://localhost:8000/api/agent/docs

### Failure handling

```bash
bash scripts/failure-demo.sh     # stops / pauses a service, its database and an agent, one at a time
```

Partial answers, retries, timeouts and the circuit breaker are explained in
[`docs/FAILURE-HANDLING.md`](docs/FAILURE-HANDLING.md).

### Talking A2A to the specialist agents directly

The Customer and Order agents are internal. Talk to them with the A2A command-line client
from inside the coordinator container:

```bash
bash scripts/a2a-demo.sh         # discovery, skills, not-found, text routing, correlation
docker compose exec -T coordinator-agent python -m a2a_core.cli --brief send http://order-agent:8000 get_order order_id=O1001
```

How the agents and the A2A interaction work: [`docs/AGENTS.md`](docs/AGENTS.md).

### Automated tests

```bash
pip install -r tests/requirements.txt
pytest tests/ -v                 # everything (API + gateway tests need the stack running)
pytest tests/test_agents.py tests/test_coordinator.py tests/test_resilience.py tests/test_tracing.py -v   # no Docker
```

The tests create extra customers and orders (orders go to C005, so the demo customers stay as
documented). For a clean demo database: `docker compose down -v && docker compose up --build -d`.

## API overview

All paths below are as seen through the gateway. Full specs: `docs/openapi/*.yaml`
(customer-api, order-api, agent-api).

| Method | Path | Purpose | Success | Errors |
|---|---|---|---|---|
| GET | `/api/customers` | List customers (`limit`, `offset`, `email`) | 200 | 400, 503 |
| GET | `/api/customers/{id}` | Get one customer | 200 | 400, 404, 503 |
| POST | `/api/customers` | Create a customer | 201 + `Location` | 400, 409, 503 |
| GET | `/api/orders` | List orders, newest first (`customer_id`, `status`, `limit`, `offset`) | 200 | 400, 503 |
| GET | `/api/orders/{id}` | Get one order with its items | 200 | 400, 404, 503 |
| POST | `/api/orders` | Place an order (status PENDING, total computed) | 201 + `Location` | 400, 503 |
| PATCH | `/api/orders/{id}` | Change status (enforced lifecycle) | 200 | 400, 404, 409, 503 |
| POST | `/api/agent/query` | Ask in natural language (`{"query": "..."}`); see `docs/AGENTS.md` | 200 | 400, 503 |
| GET | `/api/agent/agents` | Agents and skills discovered from Agent Cards | 200 | — |

**Latest order for a customer:** `GET /api/orders?customer_id=C001&limit=1`

**Error format** (every API, every error):

```json
{
  "error": {
    "code": "CUSTOMER_NOT_FOUND",
    "message": "Customer C999 was not found",
    "details": null,
    "correlation_id": "6f1c2a9e-..."
  }
}
```

**Demo data:** customers C001–C005 (C004 has no orders; C999 does not exist).
Orders O1001–O1007; O1001 is C001's oldest order (DELIVERED), O1006 is C001's latest (SHIPPED).

```bash
curl -i -H "apikey: demo-client-key" http://localhost:8000/api/customers/C001
curl -i -H "apikey: demo-client-key" http://localhost:8000/api/customers/C999      # 404 CUSTOMER_NOT_FOUND
curl -i -H "apikey: demo-client-key" "http://localhost:8000/api/orders?customer_id=C001&limit=1"  # latest order
curl -i -X POST http://localhost:8000/api/customers -H "apikey: demo-client-key" \
     -H "Content-Type: application/json" \
     -d '{"name":"Pim Rattanakul","email":"pim.r@example.com"}'                    # 201 + Location
curl -i http://localhost:8000/api/customers/C001                                    # 401 no key
curl -i -H "apikey: chat-frontend-key" http://localhost:8000/api/customers/C001     # 403 not allowed
```

## Progress

- [x] Step 0 — Stack chosen and justified (`docs/DECISIONS.md`)
- [x] Step 1 — Repo skeleton, Docker Compose, gateway routing
- [x] Step 2 — Customer & Order services with data, validation, error model, OpenAPI, tests
- [x] Step 3 — Gateway security: API keys, ACLs, rate limiting, logging, metrics
- [x] Step 4 — Customer & Order agents (Agent Cards, A2A tasks, managed-API tools)
- [x] Step 5 — Coordinator agent: planning, discovery, delegation, answer composition
- [x] Step 6 — Failure handling (timeout budget, retries, circuit breaker, partial answers)
- [x] Step 7 — Observability (OpenTelemetry traces in Jaeger, linked logs, metrics)
- [ ] Step 8 — Tests and API collection
- [ ] Step 9 — Final documentation

## Assumptions

- Customer and order IDs follow the brief's examples: `C` + 3–6 digits, `O` + 4–8 digits.
- Single currency per order; THB by default.
- The Order Service does not verify that the customer exists when an order is created
  (keeps the services decoupled; see `docs/DECISIONS.md`, D10).
- Agents run on a private network and trust each other; only their calls to the business
  APIs are authenticated (at the gateway).

## Known limitations

- API keys are static and stored in `gateway/kong.yml` (development keys only).
- Errors generated by the gateway itself (401/403/429/413) use Kong's `{"message": ...}` body,
  not the platform error envelope (see `docs/DECISIONS.md`, D4).
- Jaeger stores traces in memory (lost on restart) and samples 100 % of requests.
- Rate-limit counters and circuit-breaker state are local to one process (one gateway node,
  one agent instance).
- Schema migrations run at service startup instead of through a migration tool.
- A2A is a subset of v0.3: synchronous `message/send` and `tasks/get` only (no streaming,
  push notifications, cancellation or multi-turn tasks). Tasks are kept in memory (last 1000).
- Agents understand requests through rules (IDs and keywords), not an LLM, so only
  supported phrasings work.
- Customers cannot be updated or deleted; orders cannot have their items changed
  (not needed for the assessment scenarios).
