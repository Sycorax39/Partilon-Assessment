#!/usr/bin/env bash
# Step 6 demo: failure handling. Breaks one component at a time, asks the Agent API, restores it.
#
#   A. Backend service down       docker compose stop  order-service  -> partial, BACKEND_UNAVAILABLE (after 1 retry)
#   B. Backend hung (timeouts)    docker compose pause order-service  -> 3 slow timeouts, then the circuit
#                                                                       opens and answers come back instantly
#   C. Database down              docker compose stop  order-db       -> partial (upstream DATABASE_UNAVAILABLE)
#   D. Agent unavailable          docker compose stop  order-agent    -> partial, then 503 when nothing is retrievable
#
# Every answer stays honest: what is unavailable is said to be unavailable, nothing is invented.
# Usage: bash scripts/failure-demo.sh     (takes about 2 minutes)
set -u
cd "$(dirname "$0")/.."
PY=python3
"$PY" -c "import sys" 2>/dev/null || PY=python
Q="Find customer C001 and tell me their latest order status"

ask() { "$PY" scripts/ask.py "$@"; echo; }
step() { printf '\n\n########## %s ##########\n' "$1"; }
note() { printf '>>> %s\n' "$1"; }
wait_healthy() {
  for _ in $(seq 1 30); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q "$1")" 2>/dev/null)" = "healthy" ] && return 0
    sleep 2
  done
  echo "(warning: $1 did not report healthy within 60 s)"
}
agent_health() {
  docker compose exec -T order-agent python -c \
    "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"
}
restore_all() { docker compose unpause order-service >/dev/null 2>&1; docker compose start order-service order-db order-agent >/dev/null 2>&1; }
trap restore_all EXIT     # whatever happens, put everything back

step "0. Baseline: everything healthy"
ask "$Q"

step "A. Order SERVICE down"
note "docker compose stop order-service"
docker compose stop order-service >/dev/null
ask "$Q"
note "Customer part answered; order part reported unavailable. The Order Agent retried once (502 is transient)."
docker compose start order-service >/dev/null; wait_healthy order-service

step "B. Order service HUNG -> timeouts -> circuit breaker"
note "docker compose pause order-service   (process frozen: requests hang until the gateway's 4 s timeout)"
docker compose pause order-service >/dev/null
for i in 1 2 3 4; do
  note "query $i"
  ask "$Q"
done
note "Queries 1-3 waited for the timeout. Query 4 answered immediately: the circuit is OPEN and no request"
note "was sent to the frozen service. The Order Agent's circuit state:"
agent_health
docker compose unpause order-service >/dev/null
note "Service resumed. Waiting 16 s for the circuit to allow a trial call..."
sleep 16
ask "$Q"
agent_health

step "C. Order DATABASE down"
note "docker compose stop order-db"
docker compose stop order-db >/dev/null
ask "$Q"
note "The Order Service answered 503 DATABASE_UNAVAILABLE; the agent reported the order data as unavailable."
docker compose start order-db >/dev/null; wait_healthy order-db; sleep 2

step "D. Order AGENT down"
note "docker compose stop order-agent"
docker compose stop order-agent >/dev/null
ask "$Q"
note "Coordinator tried the agent twice (connection refused), then answered with what it had."
ask "What is the status of order O1001?"
note "Nothing retrievable -> HTTP 503, status=failed. Re-discovery found no agent offering get_order."
docker compose start order-agent >/dev/null; wait_healthy order-agent; sleep 1
ask "What is the status of order O1001?"
note "The agent is back and was re-discovered automatically, without restarting the coordinator."

step "Logs: retries and circuit changes"
docker compose logs --no-log-prefix --since 5m order-agent coordinator-agent 2>/dev/null \
  | grep -E '"(tool.api_retry|circuit.state|a2a.retry)"' | cut -c1-220 | tail -15
