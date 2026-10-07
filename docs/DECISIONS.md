# Architecture & Technology Decisions

This file records each technology choice and the reasoning behind it.
It feeds the "Architecture decisions" and "Technology choices" sections of the README
and is the basis for answering "why did you choose X?" in the interview.

Format: **Decision → Why → Trade-off / what I'd do in production.**

---

## D1. Backend services: Python 3.12 + FastAPI

- **Why:** FastAPI generates an OpenAPI 3 spec and Swagger UI automatically from the code,
  so the API documentation can never drift from the implementation. Pydantic models give
  request/response validation with clear 422 errors for free. Async I/O suits services
  that mostly wait on the database or other APIs.
- **Trade-off:** Python is slower than Go/Java for CPU-heavy work, which doesn't matter here
  (the workload is I/O-bound). Using one language for services *and* agents keeps the repo
  simple to read and review.

## D2. Data stores: PostgreSQL 16, one instance per service

- **Why:** The brief requires each service to own its data store. Two separate Postgres
  containers (`customer-db`, `order-db`) make that boundary physical: the Order Service
  cannot join against the customer table even by accident. This is the
  "database-per-service" microservice pattern.
- **Consequence:** "Customer + latest order" can't be a SQL join. It has to be composed at
  the API/agent layer, which is exactly the orchestration the assessment wants to see.
- **Trade-off:** Referential integrity between orders and customers isn't enforced by the
  database. In production this would be handled with validation on order creation and/or
  events (e.g. "customer deleted").

## D3. API Gateway: Kong Gateway OSS 3.9, DB-less (declarative) mode

- **Why:** The whole gateway configuration lives in one version-controlled file
  (`gateway/kong.yml`) — "configuration as code", reviewable in a pull request, no admin
  database to run. Kong ships the plugins the brief asks for:
  - `key-auth` + consumers → authentication and access control
  - `acl` → which consumers may call which APIs
  - `rate-limiting` → traffic control (returns HTTP 429)
  - `correlation-id` → request tracing / correlation header
  - `file-log` / `prometheus` / `opentelemetry` → logging, metrics and tracing
- **Alternatives considered:** Apache APISIX (equally capable, but typically runs with etcd
  as an extra dependency), Envoy (very powerful but low-level config for this scope),
  Traefik (great ingress/router, thinner API-management feature set).
- **Trade-off:** DB-less mode means config changes require a reload rather than an
  Admin API call, and rate-limit counters are local to one gateway node. In production:
  multiple nodes with Redis-backed rate limiting, and config delivered by CI/CD (decK).

## D4. Security at the gateway: API keys, per-route ACLs, per-consumer rate limits

- **Authentication — API keys (`key-auth`).** Every caller is a named *consumer* with its own
  key (`apikey` header). Simple to demonstrate and to explain, and it gives the gateway an
  identity to hang access rules, quotas and logs on. `hide_credentials` strips the key before
  forwarding, so backends never see it and it never appears in backend logs.
- **Authorization — least privilege (`acl`).** Each API has a READ route (GET) and a WRITE route
  (POST/PUT/PATCH/DELETE), and ACL groups are granted per route:

  | Consumer | Customer API | Order API | Agent API |
  |---|---|---|---|
  | `demo-client`, `test-runner` (platform-clients) | read + write | read + write | yes |
  | `chat-frontend` (agent-users) | — | — | yes |
  | `customer-agent` (customer-readers) | read only | — | — |
  | `order-agent` (order-readers) | — | read only | — |

  So an AI agent can never change data, a chat UI can't bypass the agent and query raw APIs,
  and agents can't call back into the coordinator (no loops).
- **Traffic control (`rate-limiting`) per consumer**, sized to each consumer's role
  (external demo client 20/min, agents 300/min, a 5/min "probe" consumer for demonstrating
  429s). Clients see their quota in `X-RateLimit-*` headers and get `Retry-After` on 429.
  The public docs routes are limited per IP instead. `fault_tolerant: true` means a counter
  failure lets traffic through rather than causing an outage.
- **Payload limit:** 1 MB (`request-size-limiting`) → 413.
- **Public docs:** Swagger UI and the OpenAPI JSON are served on separate unauthenticated
  routes so evaluators can browse them; the specs declare the `apikey` scheme so
  "Try it out" works after clicking **Authorize**.
- **Known gap:** errors produced by the gateway itself (401/403/429/413) use Kong's own body
  `{"message": "..."}`, not the platform envelope used by the services. Clients should branch
  on the HTTP status for these. Production fix: Kong custom error templates or a small
  response-transformation plugin so every error, from any layer, has the same shape.
- **Production:** OAuth 2.0 client-credentials with short-lived JWTs (e.g. Keycloak) instead
  of static keys; keys/secrets in a vault, not in `kong.yml`; mTLS between gateway and services;
  Redis-backed rate limiting so limits hold across several gateway nodes.

## D5. Agents: custom Python, deterministic planner (no LLM required)

- **Why deterministic:** the brief accepts a deterministic agent if it clearly shows
  decision-making, tool selection, orchestration and A2A. Rules make every demo run
  reproducible, need no GPU, model download or paid API, and every decision can be explained
  (the response includes the planner's `reasoning`).
- **Separation of concerns in the coordinator:** plan → discover → delegate → answer, each a
  separate module. The planner only says *which skills* are needed; *which agent* provides a
  skill is learned from Agent Cards at run time. Adding an agent with a new skill needs a new
  URL in `AGENT_URLS` plus a planner rule, but no code change to the executor.
- **Dependencies between steps:** the order lookup runs only after the customer is confirmed
  to exist; steps without dependencies run in parallel; results can feed later steps
  (email → customer ID → latest order).
- **Answers from templates over returned data only.** This is the strongest guarantee
  against fabrication: the coordinator has no way to produce a fact an agent didn't return.
  An LLM would write more fluent answers but would need guardrails (structured output, answer
  validation against the step data) to keep the same guarantee.
- **HTTP 503 only when nothing could be retrieved** (`failed`); `partial`, `not_found` and
  `unsupported` are valid answers (200). Monitoring still sees outages as 5xx.
- **Timeout budget:** API 5 s < agent task 8 s < A2A call 10 s < query 12 s < gateway 15 s,
  so each layer answers in a controlled way before the layer above gives up.
- **Trade-off / production:** only supported phrasings are understood. Next step: an LLM
  (e.g. Ollama locally) used *only* as the planner, producing the same structured plan,
  validated against the skills in the discovered Agent Cards; execution and answer
  composition stay deterministic.

## D6. Agent-to-Agent: A2A protocol v0.3 (JSON-RPC), implemented by hand

- **Why A2A:** an open, vendor-neutral protocol for agents to discover and call each other.
  Each agent publishes an **Agent Card** at `/.well-known/agent-card.json` (name, endpoint,
  skills with input schemas). Callers send JSON-RPC `message/send`; the agent returns a
  **Task** with a status (`submitted → working → completed | failed | rejected`), its
  transition history and result **artifacts**. `tasks/get` re-reads a task.
- **Why hand-written (`libs/a2a_core`, ~400 lines) instead of the `a2a-sdk` package:**
  every protocol concept is visible in a small amount of code I can explain line by line,
  there's no dependency on a fast-changing SDK API, and the wire format still follows the
  spec, so a real A2A client could talk to these agents. Trade-off: no streaming, push
  notifications, cancellation or multi-turn tasks (not needed for the scenarios).
- **Structured and text requests:** a caller can name the skill (`data` part) or send plain
  text (`text` part), in which case the agent selects the skill with its own rules. This shows
  tool selection at two levels: the coordinator picks the agent, the agent picks the tool.
- **Outcome semantics:** "not found" is a *completed* task with `outcome: not_found` (a
  verified answer); an outage or timeout is a *failed* task with a stable error code and a
  `retryable` flag (we don't know). Failed tasks carry no artifacts. This is what lets the
  coordinator answer honestly instead of guessing (see `docs/AGENTS.md`, section 4).
- **Deadlines:** 5 s per API call, 8 s per task; the gateway's own upstream timeout is 5 s.
- **Production:** authenticate agent-to-agent calls (mTLS or signed JWTs), persist tasks
  (currently in memory, last 1000), add streaming for long-running tasks, and keep a registry
  of Agent Cards instead of configured URLs.

## D7. Agents call the backends *through the gateway*

- **Why:** The brief says agents must use managed APIs, not databases. Going further, agents
  call the same gateway as any external client, using their own API keys
  (`customer-agent-key`, `order-agent-key`), and get **read-only access to their own API only**. So agent traffic
  is authenticated, rate-limited and logged like everything else — an AI agent is treated
  as just another API consumer. (Modification to the reference diagram, see README.)

## D8a. Gateway logging and monitoring

- **Logging:** Kong's `file-log` plugin writes one JSON record per request to the gateway's
  stdout: consumer, route, service, status, request headers (including `X-Correlation-ID`;
  the API key is already removed) and latencies split into gateway time vs upstream time.
  The plain nginx access log is turned off to avoid duplicate lines.
- **Monitoring:** the `prometheus` plugin exposes request counts, status codes, latencies
  and bandwidth per service/route/consumer at `http://localhost:8100/metrics`.
  A Prometheus + Grafana dashboard would be the production next step.

## D8. Observability: correlation ID + OpenTelemetry → Jaeger

- **Why:** The gateway assigns an `X-Correlation-ID` to every request; every service and agent
  logs it (structured JSON logs) and forwards it on every outbound call. OpenTelemetry
  traces the same path and Jaeger displays it as one end-to-end trace:
  gateway → coordinator → agent → gateway → backend service.
- **Trade-off / production:** Add a log aggregator (Loki/ELK), Prometheus + Grafana
  dashboards and alerting.

## D10. REST API design conventions (Customer & Order APIs)

- **Resource-oriented URLs, standard verbs:** `GET /customers/{id}`, `POST /orders`,
  `PATCH /orders/{id}` (partial update of the status). Correct status codes: 200, 201 + `Location`
  header on create, 400 validation, 404 not found, 409 conflict, 503 dependency down.
- **One error envelope everywhere** (`libs/common/errors.py`):
  `{"error": {"code", "message", "details", "correlation_id"}}`. Clients and agents branch on the
  stable `code` (e.g. `CUSTOMER_NOT_FOUND`), never on message text. The correlation ID in every
  error lets support find the exact request in the logs.
- **Validation returns 400 with per-field details** (FastAPI's default is 422; 400 was chosen so
  every client error family is simple and consistent). Unknown JSON fields are rejected
  (`extra="forbid"`) so typos don't silently do nothing.
- **Human-readable business IDs** (`C001`, `O1001`) generated by Postgres sequences, matching
  the brief's examples. IDs are format-checked on input (`^C\d{3,6}$`) so garbage never reaches the DB.
- **Money as decimal strings** (`"1880.00"`), never floats, to avoid rounding errors. The order
  total is calculated by the server, never trusted from the client.
- **Order status is a state machine:** PENDING → PAID → SHIPPED → DELIVERED, with CANCELLED
  allowed from PENDING/PAID. Illegal moves return 409 `INVALID_STATUS_TRANSITION` and list the
  allowed next states. A row lock (`SELECT … FOR UPDATE`) prevents concurrent updates racing.
- **"Latest order" = `GET /orders?customer_id=C001&limit=1`** (lists are newest first). No
  special endpoint needed; an index on `(customer_id, created_at DESC)` keeps it fast. A customer
  with no orders gets an empty list (200), not a 404 — "no orders" is a valid answer, not an error.
- **Lists are paginated** (`limit` 1–100, `offset`) with `total`, so no endpoint can return an
  unbounded result.
- **Order Service does not check that the customer exists** when an order is created. Doing so
  would couple the two services at write time. Assumption for the prototype; in production
  either validate through the Customer API via the gateway, or keep a local replica of customer
  IDs updated by events.
- **Schema + seed run at service startup** (idempotent SQL: `CREATE … IF NOT EXISTS`,
  `ON CONFLICT DO NOTHING`). This works even if the database volume already exists. Production
  would use a migration tool (Alembic/Flyway) run as a separate deployment step.
- **Liveness vs readiness:** `/health` = process is up; `/ready` = can reach the database.
  Neither is exposed through the gateway.
- **Shared code is minimal** (`libs/common`: errors, correlation ID, logging, DB pool). Each
  service still owns its models, routes, schema and database.

## D9. Deployment: Docker Compose

- **Why:** One command (`docker compose up --build`) starts every component with no manual
  configuration — what the brief asks for. Only the gateway port is published to the host,
  so backends are reachable *only* through API management.
- **Trade-off / production:** Kubernetes (Helm), health-based autoscaling, managed Postgres.
