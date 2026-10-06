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

## D4. Authentication: API keys at the gateway (prototype)

- **Why:** Simple to demonstrate and to explain. Each consumer (external client, each agent)
  gets its own key, so the gateway knows *who* is calling and can apply per-consumer
  rate limits and access rules. Backends trust the gateway and never see raw keys.
- **Trade-off / production:** OAuth 2.0 client-credentials with short-lived JWTs (Keycloak
  or similar), mTLS between internal services, keys in a secrets manager instead of a file.

## D5. Agents: custom Python, deterministic planner (LLM optional)

- **Why:** The brief allows a deterministic agent if it clearly shows decision-making,
  tool selection, orchestration and A2A. A rule-based intent parser makes every demo run
  reproducible and needs no GPU or paid API. The planner is isolated behind one interface
  so an Ollama-backed planner can be swapped in later.
- **Trade-off:** Limited natural-language understanding — only the supported phrasings
  work. This is documented as a known limitation.

## D6. Agent-to-Agent: A2A protocol concepts (Agent Card + JSON-RPC tasks)

- **Why:** A2A is an open, vendor-neutral protocol for agents to discover and call each other.
  Each specialist agent publishes an **Agent Card** at `/.well-known/agent.json`
  (name, endpoint, skills). The Coordinator discovers agents from their cards and sends
  them **tasks** over JSON-RPC; each task returns a **status**
  (`submitted → working → completed | failed`) and its result.
- **Trade-off:** Implemented as a minimal subset of the protocol (synchronous request/response,
  no streaming or push notifications) to keep the prototype readable.

## D7. Agents call the backends *through the gateway*

- **Why:** The brief says agents must use managed APIs, not databases. Going further, agents
  call the same gateway as any external client, using their own API keys. So agent traffic
  is authenticated, rate-limited and logged like everything else — an AI agent is treated
  as just another API consumer. (Modification to the reference diagram, see README.)

## D8. Observability: correlation ID + OpenTelemetry → Jaeger

- **Why:** The gateway assigns an `X-Correlation-ID` to every request; every service and agent
  logs it (structured JSON logs) and forwards it on every outbound call. OpenTelemetry
  traces the same path and Jaeger displays it as one end-to-end trace:
  gateway → coordinator → agent → gateway → backend service.
- **Trade-off / production:** Add a log aggregator (Loki/ELK), Prometheus + Grafana
  dashboards and alerting.

## D9. Deployment: Docker Compose

- **Why:** One command (`docker compose up --build`) starts every component with no manual
  configuration — what the brief asks for. Only the gateway port is published to the host,
  so backends are reachable *only* through API management.
- **Trade-off / production:** Kubernetes (Helm), health-based autoscaling, managed Postgres.
