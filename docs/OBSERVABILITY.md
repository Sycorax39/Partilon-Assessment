# Observability

How to follow one request across the whole platform (brief section 4.6). Three signals,
linked by two IDs:

| Signal | Tool | Where |
|---|---|---|
| **Traces**: who called whom, in what order, how long, where it failed | OpenTelemetry → **Jaeger** | http://localhost:16686 |
| **Logs**: what each component did, one JSON line per event | stdout of each container | `docker compose logs <service>` |
| **Metrics**: request counts, status codes, latencies per route and consumer | Kong `prometheus` plugin | http://localhost:8100/metrics |

| ID | Created by | Found in |
|---|---|---|
| `X-Correlation-ID` | The gateway (or the client) | Every log line (`correlation_id`), every error body, every server span (`correlation.id`), A2A task metadata |
| Trace ID (W3C `traceparent`) | The gateway's OpenTelemetry plugin | `X-Trace-Id` response header, every log line (`trace_id`), Agent API response (`trace_id`) |

So from **any** starting point you can get to everything else: a log line or an error
response leads to the trace in Jaeger, and a trace leads to the logs.

## 1. Trace one complete request (demo)

```bash
python scripts/trace.py          # sends the key scenario, prints the span tree + Jaeger link
```

```
api-gateway        kong                                        +0.0ms   152.3ms
  api-gateway      kong.access.plugin.key-auth / acl / rate-limiting ...
  coordinator-agent  POST /agent/query                           +3.1ms  145.0ms  correlation.id=trace-1a2b3c4d  agent.status=completed
    coordinator-agent  coordinator.plan                            +4.0ms    0.1ms  plan.steps=s1:get_customer -> s2:get_latest_order
    coordinator-agent  delegate get_customer                       +4.3ms   64.8ms  a2a.agent=Customer Agent
      coordinator-agent  POST customer-agent/                        (A2A message/send)
        customer-agent     a2a.task get_customer                       a2a.state=completed  a2a.outcome=found
          customer-agent     GET gateway/api/customers/C001
            api-gateway        kong                                    (key-auth as customer-agent, ACL, rate limit)
              customer-service   GET /customers/{customer_id}
                customer-service   SELECT                              (SQL query)
    coordinator-agent  delegate get_latest_order                   a2a.agent=Order Agent
      ... same shape through the Order Agent, the gateway and the Order Service ...
    coordinator-agent  coordinator.answer                          agent.status=completed
```

*(Shape of the output; exact span names and timings vary.)*

In the Jaeger UI: open the link printed by `trace.py` or `ask.py`, or **Search** →
Service `coordinator-agent` → Tags `correlation.id=<id>`.

## 2. What each component contributes

| Component | Spans | Key attributes |
|---|---|---|
| **Kong** (`opentelemetry` plugin) | One per request, plus router, plugin (`key-auth`, `acl`, `rate-limiting`) and upstream (balancer) spans | `service.name=api-gateway`; HTTP route and status |
| **Coordinator** | `POST /agent/query` → `coordinator.plan` → one `delegate <skill>` per step → `coordinator.answer` | `agent.query`, `plan.steps`, `plan.reasoning`, `step.state`, `agent.status`; `retry` / `step skipped` events |
| **Customer / Order Agent** | `POST /` (A2A) → `a2a.task <skill>` → `GET gateway/api/...` | `a2a.skill`, `a2a.state`, `a2a.outcome`, `a2a.task.id`; `retry` and circuit-breaker events |
| **Customer / Order Service** | `GET /customers/{id}`, `GET /orders` → SQL statements | `http.status_code`, `db.statement` |

Failures are visible at a glance: failed steps and tasks are marked **ERROR** with an
`error.code` (e.g. `BACKEND_UNAVAILABLE`, `CIRCUIT_OPEN`), and each retry attempt appears as its
own child span. Agent discovery shows up too: when the registry refreshes, the Agent Card
fetches appear inside the step that needed them.

## 3. How the trace stays connected

1. Kong starts the trace and forwards a W3C `traceparent` header to the coordinator.
2. The coordinator's FastAPI instrumentation continues it. Its custom spans (`plan`, `delegate`)
   are children of the request span.
3. The coordinator's httpx client (instrumented) **injects** `traceparent` into each A2A call.
4. Each agent continues the trace, and its httpx client injects `traceparent` into its call to the gateway.
5. Kong continues the trace (`propagation`: W3C) and forwards it to the service.
6. The service continues it; psycopg instrumentation adds the SQL spans.

Nothing in the business code passes trace IDs around by hand. The only manual part is
`X-Correlation-ID`, which is forwarded explicitly because it is also used for logs and error
bodies.

## 4. Logs

Every line is JSON with the same fields:

```json
{"ts": "2026-10-07T10:36:33.399+00:00", "level": "INFO", "service": "order-agent",
 "correlation_id": "key-scenario-001", "trace_id": "58d7261fad28bd50cfdb49cd6e1f64d6",
 "message": "task status", "event": "a2a.task.status", "task_id": "…", "skill": "get_latest_order", "state": "completed"}
```

Useful events: `coordinator.plan`, `a2a.delegate`, `a2a.step`, `a2a.task.status`,
`tool.api_call`, `tool.api_retry`, `circuit.state`, `coordinator.answer`.

```bash
docker compose logs --no-log-prefix | grep key-scenario-001          # one request, every component
docker compose logs gateway                                          # Kong: consumer, route, status, latencies
```

## 5. Configuration

| Setting | Where | Value |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | all Python components | `http://jaeger:4318` (tracing is off when unset, e.g. in unit tests) |
| `OTEL_BSP_SCHEDULE_DELAY` | all Python components | 1000 ms (spans exported every second) |
| `KONG_TRACING_INSTRUMENTATIONS` | gateway | `request,router,balancer,plugin_access` |
| `KONG_TRACING_SAMPLING_RATE` | gateway | `1.0` (every request) |
| `opentelemetry` plugin | `gateway/kong.yml` | `traces_endpoint: http://jaeger:4318/v1/traces`, `X-Trace-Id` response header |

Spans are exported in the background in batches. If Jaeger is down, requests are not slowed
or failed; only the traces are lost.

## 6. Limitations and production next steps

- Jaeger keeps traces **in memory**: they disappear when the container restarts. Production:
  an OpenTelemetry Collector in front, and persistent storage (Elasticsearch/OpenSearch,
  Cassandra, or a managed backend).
- **100 % sampling** is fine for a demo; production would sample (e.g. 1–10 %, plus all errors
  via tail-based sampling in the Collector).
- Logs stay in container stdout; production would ship them to Loki or ELK and link them to
  traces by `trace_id`.
- Metrics are exposed but not scraped; production would add Prometheus + Grafana dashboards
  and alerts (error rate, latency, circuit-open events).
