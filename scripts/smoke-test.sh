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
check "Agent API routed"             POST /api/agent/query     501   # placeholder until Step 5
check "Unknown route"                GET  /api/unknown         404
check "API docs are public"          GET  /api/customers/docs  200 -

echo "--- Security ---"
check "No API key -> 401"            GET  /api/customers/C001  401 -
check "Wrong API key -> 401"         GET  /api/customers/C001  401 wrong-key
check "Chat UI on Customer API -> 403"  GET  /api/customers/C001  403 chat-frontend-key
check "Customer agent reads customers"  GET  /api/customers/C001  200 customer-agent-key
check "Customer agent reads orders -> 403"  GET /api/orders/O1001 403 customer-agent-key
check "Order agent writes order -> 403"     POST /api/orders     403 order-agent-key

echo "--- Rate limiting (ratelimit-probe: 5 requests/minute) ---"
got429=0
for i in $(seq 1 8); do
  s=$(curl -s -o /dev/null -w '%{http_code}' -H "apikey: ratelimit-probe-key" "$GW/api/customers/C001")
  printf '  request %d -> %s\n' "$i" "$s"
  [ "$s" = "429" ] && { got429=1; break; }
done
if [ $got429 = 1 ]; then echo "PASS  Excessive requests -> 429"; else echo "FAIL  never received 429"; fail=1; fi

exit $fail
