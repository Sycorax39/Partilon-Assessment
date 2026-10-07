#!/usr/bin/env bash
# Step 5 demo: the brief's Agentic + A2A scenarios, through the API gateway.
#
#   client --(apikey)--> Kong --> Coordinator --(A2A)--> Customer / Order Agent --> Kong --> services
#
# Usage: bash scripts/agent-demo.sh        (after `docker compose up --build -d`)
set -u
cd "$(dirname "$0")/.."
PY=python3
"$PY" -c "import sys" 2>/dev/null || PY=python      # Windows usually has "python", not "python3"

ask() { "$PY" scripts/ask.py "$@"; echo; }
step() { printf '\n===== %s =====\n' "$1"; }

step "1. Retrieve a customer (single capability)"
ask "Show customer C001"

step "2. Retrieve customer and latest order (multiple APIs orchestrated)"
ask "Show customer C001 and their latest order"

step "3. Retrieve an unknown customer (no data -> nothing invented)"
ask "Show customer C999"

step "4. Retrieve an order through agent delegation (A2A)"
ask "What is the status of order O1001?"

step "5. KEY SCENARIO: customer and order through multiple agents"
ask --trace key-scenario-001 "Find customer C001 and tell me their latest order status"

step "6. Unknown customer: the order lookup is skipped, not guessed"
ask "Find customer C999 and tell me their latest order status"

step "7. Data flows from one agent to the next (email -> customer ID -> latest order)"
ask "What is the latest order of somchai.j@example.com?"

step "8. Request outside the assistant's scope"
ask "Can you book me a flight to Chiang Mai?"

step "9. Follow the key scenario through every component (correlation ID key-scenario-001)"
docker compose logs --no-log-prefix gateway coordinator-agent customer-agent order-agent customer-service order-service 2>/dev/null \
  | grep key-scenario-001 | cut -c1-200

cat <<'EOF'

Failure handling - try it yourself:
  docker compose stop order-agent        # or: order-service
  python scripts/ask.py "Find customer C001 and tell me their latest order status"
      -> status=partial: customer shown, order information "currently unavailable"
  docker compose start order-agent

  docker compose stop customer-agent
  python scripts/ask.py "Show customer C001"
      -> HTTP 503, status=failed, nothing invented
  docker compose start customer-agent
EOF
