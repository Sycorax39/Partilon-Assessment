#!/usr/bin/env bash
# Smoke test through the API gateway: routing, security, rate limiting, correlation IDs.
# Usage: bash scripts/smoke-test.sh   (after `docker compose up --build -d`)
set -u
GW="${GATEWAY:-http://localhost:8000}"
KEY="${API_KEY:-test-runner-key}"
fail=0

# check NAME METHOD PATH EXPECTED_STATUS [API_KEY]   (pass "-" as key for no key)
check() {
  local name="$1" method="$2" path="$3" expected="$4" key="${5:-$KEY}"
  local args=(-s -o /dev/null -D - -X "$method")
  [ "$key" != "-" ] && args+=(-H "apikey: $key")
  [ "$method" = "POST" ] && args+=(-H "Content-Type: application/json" -d '{}')
  local headers status cid
  headers=$(curl "${args[@]}" "$GW$path")
  status=$(printf '%s' "$headers" | head -1 | awk '{print $2}')
  cid=$(printf '%s' "$headers" | grep -i '^x-correlation-id' | cut -d' ' -f2 | tr -d '\r')
  if [ "$status" = "$expected" ]; then
    echo "PASS  $name  ($method $path -> $status${cid:+, correlation-id=$cid})"
  else
    echo "FAIL  $name  ($method $path -> got '$status', expected $expected)"
    fail=1
  fi
}

echo "--- Routing ---"
check "Get customer C001"            GET  /api/customers/C001  200
check "Unknown customer C999"        GET  /api/customers/C999  404
check "Get order O1001"              GET  /api/orders/O1001    200
check "Latest order for C001"        GET  "/api/orders?customer_id=C001&limit=1" 200
check "Unknown route"                GET  /api/unknown         404
check "API docs are public"          GET  /api/customers/docs  200 -

echo "--- Security ---"
check "No API key -> 401"            GET  /api/customers/C001  401 -
check "Wrong API key -> 401"         GET  /api/customers/C001  401 wrong-key
check "Chat UI on Customer API -> 403"  GET  /api/customers/C001  403 chat-frontend-key
check "Customer agent reads customers"  GET  /api/customers/C001  200 customer-agent-key
check "Customer agent reads orders -> 403"  GET /api/orders/O1001 403 customer-agent-key
check "Order agent writes order -> 403"     POST /api/orders     403 order-agent-key

echo "--- Agent API (Coordinator -> A2A -> agents -> gateway -> services) ---"
# query NAME TEXT EXPECTED_HTTP EXPECTED_STATUS
query() {
  local name="$1" text="$2" expected="$3" want="$4" out status st
  out=$(curl -s -w '\n%{http_code}' -X POST "$GW/api/agent/query" -H "apikey: chat-frontend-key" \
        -H "Content-Type: application/json" -d "{\"query\": \"$text\"}")
  status=$(printf '%s' "$out" | tail -n 1)
  st=$(printf '%s' "$out" | sed '$d' | grep -o '"status":"[a-z_]*"' | head -1 | cut -d'"' -f4)
  if [ "$status" = "$expected" ] && [ "$st" = "$want" ]; then
    echo "PASS  $name  (HTTP $status, status=$st)"
  else
    echo "FAIL  $name  (got HTTP '$status' status='$st', expected $expected/$want)"
    fail=1
  fi
}
query "Retrieve a customer"            "Show customer C001"                                       200 completed
query "Customer + latest order"        "Find customer C001 and tell me their latest order status" 200 completed
query "Unknown customer"               "Show customer C999"                                       200 not_found
query "Order via agent delegation"     "What is the status of order O1001?"                       200 completed
query "Unsupported request"            "Book me a flight"                                         200 unsupported
check "Agent API requires a key"       POST /api/agent/query 401 -
check "Agent API docs are public"      GET  /api/agent/docs  200 -

echo "--- Observability ---"
hdr=$(curl -s -o /dev/null -D - -X POST "$GW/api/agent/query" -H "apikey: chat-frontend-key" \
      -H "Content-Type: application/json" -d '{"query": "Show customer C001"}')
tid=$(printf '%s' "$hdr" | grep -i '^x-trace-id' | cut -d' ' -f2 | tr -d '\r')
if [ -n "$tid" ]; then echo "PASS  Gateway returns X-Trace-Id ($tid)"; else echo "FAIL  no X-Trace-Id header"; fail=1; fi
s=$(curl -s -o /dev/null -w '%{http_code}' "${JAEGER:-http://localhost:16686}/")
if [ "$s" = "200" ]; then echo "PASS  Jaeger UI is up (http://localhost:16686)"; else echo "FAIL  Jaeger UI -> $s"; fail=1; fi

echo "--- Rate limiting (ratelimit-probe: 5 requests/minute) ---"
got429=0
for i in $(seq 1 8); do
  s=$(curl -s -o /dev/null -w '%{http_code}' -H "apikey: ratelimit-probe-key" "$GW/api/customers/C001")
  printf '  request %d -> %s\n' "$i" "$s"
  [ "$s" = "429" ] && { got429=1; break; }
done
if [ $got429 = 1 ]; then echo "PASS  Excessive requests -> 429"; else echo "FAIL  never received 429"; fail=1; fi

exit $fail
