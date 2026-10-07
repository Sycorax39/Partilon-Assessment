# Failure Handling

How the platform behaves when parts of it fail. It covers the brief's section 4.5:
backend API unavailable, agent unavailable, timeout or downstream error. The rule throughout:
**controlled errors, never invented information.**

Demo: `bash scripts/failure-demo.sh` (about 2 minutes; breaks and restores one component at a time).

## 1. Failure matrix

| What fails | Detected by | What happens | Caller sees |
|---|---|---|---|
| **Order Service down** (container stopped) | Gateway: upstream connection fails → 502/503 | Order Agent retries once after ~0.3 s, then fails the A2A task with `BACKEND_UNAVAILABLE` | `partial`: customer shown, "order information is currently unavailable" |
| **Order Service hung** (paused / overloaded) | Gateway: no response within 4 s → 504 | Agent maps it to `UPSTREAM_TIMEOUT` (not retried); after 3 consecutive failures the **circuit opens** | First answers after ~4 s; then **instant** answers (`CIRCUIT_OPEN`) until the backend recovers |
| **Order database down** | Order Service: pool can't connect within 3 s → 503 `DATABASE_UNAVAILABLE` | Agent retries once, then fails the task with `BACKEND_UNAVAILABLE` (upstream code kept in the details) | `partial` / `failed` with "currently unavailable" |
| **Order Agent down** | Coordinator: A2A connection refused | One retry after 0.3 s (the request never reached the agent, so retrying is safe); then the step fails with `AGENT_UNREACHABLE` and the agent is re-discovered on the next request | `partial`; once discovery confirms it's gone: `NO_AGENT_FOR_SKILL` |
| **Customer Agent down** | Same | The order lookup is **skipped**: orders aren't reported for a customer whose existence couldn't be verified | `failed`, **HTTP 503** |
| **Agent slow** | Agent's own 8 s task deadline → `SKILL_TIMEOUT`; coordinator's 10 s A2A timeout → `AGENT_TIMEOUT` | Step fails, others continue | `partial` |
| **Whole query too slow** | Coordinator's 12 s query deadline | Unfinished steps are failed with `QUERY_TIMEOUT`; finished steps are kept | `partial` or `failed` |
| **Agent quota exhausted** | Gateway rate limit → 429 | `RATE_LIMITED` (not retried: retrying would only burn more quota) | "the request limit was reached" |
| **Agent's credentials wrong** | Gateway → 401/403 | `ACCESS_DENIED` (not retried; configuration problem) | "access was denied" |
| **Customer / order doesn't exist** | Service → 404 `*_NOT_FOUND` | *Not a failure:* task **completed** with `outcome: not_found` | `not_found`: "Customer C999 was not found." |

## 2. Timeout budget

Each layer gives up **before** the layer above it, so every caller receives a controlled answer
from the layer below instead of hitting its own timeout:

```
Service DB pool 3 s
  < Gateway -> backend 4 s           (Kong read_timeout; returns 504)
    < Agent -> gateway 5 s           (API_TIMEOUT_SECONDS)
      < Agent task deadline 8 s      (SKILL_TIMEOUT_SECONDS)
        < Coordinator -> agent 10 s  (A2A_TIMEOUT_SECONDS)
          < Whole query 12 s         (QUERY_TIMEOUT_SECONDS)
            < Gateway -> coordinator 15 s
```

## 3. Retries: where, and only where they help

| Layer | Retries? | Why |
|---|---|---|
| Gateway → services | **No** (`retries: 0`) | Retrying in one place only; the gateway doesn't know which calls are safe to repeat |
| Agent → gateway | **1 retry**, only for connection refused / 502 / 503, with exponential backoff + jitter | Reads are idempotent; these failures are fast and often transient (container restarting) |
| | **No** for 504 / timeouts | A retry would wait another 4 s and blow the 8 s task budget |
| | **No** for 4xx, 429 | Retrying can't fix a bad request or an exhausted quota |
| Coordinator → agent | **1 retry**, only for connection refused | The request never reached the agent, so nothing runs twice |
| | **No** for agent timeouts or failed tasks | The agent already retried what was worth retrying |

**Why retries happen at one layer only:** if every layer retried 3 times, one user request
during an outage would become 3 × 3 × 3 = 27 backend calls (a *retry storm*), hitting the
backend hardest exactly when it is weakest.

## 4. Circuit breaker

Implemented in `libs/common/resilience.py`, used by each agent per API (`/api/customers`,
`/api/orders`):

```
CLOSED --3 consecutive failures--> OPEN --15 s--> HALF_OPEN --trial succeeds--> CLOSED
                                    ^                 |
                                    +--trial fails----+
```

- Counts only failures that indicate an **unhealthy backend**: 5xx, timeouts, unreachable gateway.
  404s, 4xx and 429s don't count.
- While open: calls fail **immediately** with `CIRCUIT_OPEN`. No request is sent, users get
  an answer in milliseconds instead of waiting for timeouts, and the struggling backend gets
  room to recover.
- Half-open: exactly one trial call is let through; success closes the circuit.
- Visible at each agent's `/health` and in the logs (`"event": "circuit.state"`):

```json
{"status": "ok", "service": "order-agent",
 "circuits": {"/api/orders": {"state": "open", "consecutive_failures": 3, "retry_in_seconds": 14.9}}}
```

Settings: `CIRCUIT_FAILURE_THRESHOLD` (3), `CIRCUIT_RESET_SECONDS` (15), `API_RETRY_ATTEMPTS` (2).

## 5. Partial answers and dependencies

- Steps that don't depend on each other run in parallel and fail independently: an order outage
  doesn't hide the customer data that *was* retrieved (`status: partial`).
- A step that depends on another runs only if that step **found** its data. If the customer
  could not be verified (not found or unavailable), the order lookup is skipped and the answer
  says why.
- `failed` (nothing retrievable) is the only status returned as **HTTP 503**, so outages show
  up in gateway metrics and alerts. `partial` and `not_found` are valid answers (200).

## 6. Self-healing

| Component back after a failure | Recovery |
|---|---|
| Service database | Connection pool checks connections before use and reconnects automatically |
| Backend service | Circuit half-opens after 15 s; the first successful call closes it |
| Agent | Coordinator re-reads Agent Cards after a failed call (and every 60 s), so a restarted agent is used again without restarting the coordinator |
| Agent down when the coordinator starts | Coordinator starts anyway; the missing agent is discovered later |

## 7. What production would add

- **Distributed circuit state and rate limits** (e.g. Redis), so all replicas agree.
- **Gateway active/passive health checks** on upstreams (Kong upstream targets) and multiple
  replicas per service behind them.
- **Error envelope for gateway-generated errors**: Kong's own 401/403/429/502/504 bodies are
  `{"message": "..."}`, not the platform's `{"error": {...}}` (clients branch on the HTTP
  status for those). Fix with Kong custom error templates or a response-transformer, verified
  against the Kong version in use.
- **Bulkheads**: separate connection pools per downstream, so one slow backend can't use up
  all the agent's connections.
- **Alerting** on circuit-open events and 503 rates.
