#!/usr/bin/env bash
# Step 4 demo: talk A2A to the Customer and Order agents directly.
#
# The agents are internal (not published to your machine), so the A2A client runs inside the
# coordinator container, exactly where the Coordinator Agent will call them from in Step 5.
#
# Usage: bash scripts/a2a-demo.sh        (after `docker compose up --build -d`)
set -u
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: don't rewrite arguments that look like paths

a2a() { docker compose exec -T coordinator-agent python -m a2a_core.cli "$@"; }
step() { printf '\n\033[1m### %s\033[0m\n' "$1"; }

CUSTOMER_AGENT=http://customer-agent:8000
ORDER_AGENT=http://order-agent:8000

step "1. Discovery: the Customer Agent's Agent Card"
a2a discover $CUSTOMER_AGENT

step "2. Discovery: the Order Agent's skills"
a2a discover $ORDER_AGENT | grep -E '"(id|description)"'

step "3. Customer Agent: get_customer C001"
a2a --brief send $CUSTOMER_AGENT get_customer customer_id=C001

step "4. Customer Agent: unknown customer C999 (completed, outcome not_found - nothing invented)"
a2a --brief send $CUSTOMER_AGENT get_customer customer_id=C999

step "5. Order Agent: status of order O1001"
a2a --brief send $ORDER_AGENT get_order order_id=O1001

step "6. Order Agent: latest order of customer C001"
a2a --brief send $ORDER_AGENT get_latest_order customer_id=C001

step "7. Order Agent: plain-text request - the agent chooses the skill itself"
a2a --brief ask $ORDER_AGENT "What is the most recent order for customer C001?"

step "8. Order Agent: customer C004 has no orders"
a2a --brief send $ORDER_AGENT get_latest_order customer_id=C004

step "9. Invalid input is rejected before any API call"
a2a --brief send $ORDER_AGENT get_order order_id=1001

step "10. Same correlation ID in every log line"
a2a --brief --correlation-id demo-a2a-001 send $CUSTOMER_AGENT get_customer customer_id=C001 >/dev/null 2>&1
docker compose logs --no-log-prefix customer-agent customer-service gateway 2>/dev/null | grep demo-a2a-001 | cut -c1-220

cat <<'EOF'

Try failure handling yourself:
  docker compose stop order-service
  docker compose exec -T coordinator-agent python -m a2a_core.cli --brief send http://order-agent:8000 get_order order_id=O1001
      -> status: failed, error: BACKEND_UNAVAILABLE
  docker compose start order-service

  docker compose stop order-agent
  docker compose exec -T coordinator-agent python -m a2a_core.cli send http://order-agent:8000 get_order order_id=O1001
      -> client_error: AGENT_UNREACHABLE
  docker compose start order-agent
EOF
