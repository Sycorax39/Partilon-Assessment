#!/usr/bin/env bash
# Step 1 smoke test: is every API reachable through the gateway, with a correlation ID?
# Usage: ./scripts/smoke-test.sh   (after `docker compose up --build -d`)
set -u
GW="${GATEWAY:-http://localhost:8000}"
fail=0

check() {
  local name="$1" method="$2" path="$3" expected="$4"
  local headers status cid
  headers=$(curl -s -o /dev/null -D - -X "$method" "$GW$path")
  status=$(printf '%s' "$headers" | head -1 | awk '{print $2}')
  cid=$(printf '%s' "$headers" | grep -i '^x-correlation-id' | cut -d' ' -f2 | tr -d '\r')
  if [ "$status" = "$expected" ] && [ -n "$cid" ]; then
    echo "PASS  $name  ($method $path -> $status, correlation-id=$cid)"
  else
    echo "FAIL  $name  ($method $path -> got '$status', expected $expected, correlation-id='$cid')"
    fail=1
  fi
}

check "Customer API routed"      GET  /api/customers       200
check "Get customer C001"        GET  /api/customers/C001  200
check "Unknown customer C999"    GET  /api/customers/C999  404
check "Order API routed"         GET  /api/orders          200
check "Get order O1001"          GET  /api/orders/O1001    200
check "Latest order for C001"    GET  "/api/orders?customer_id=C001&limit=1" 200
check "Agent API routed"         POST /api/agent/query     501   # 501 = placeholder until Step 5
check "Unknown route -> 404"     GET  /api/unknown         404

exit $fail
