"""Trace one complete request through the platform and show it from Jaeger.

    python scripts/trace.py                                   # the key scenario
    python scripts/trace.py "What is the status of order O1001?"
    python scripts/trace.py --trace-id 4bf92f3577b34da6a3ce929d0e0e4736   # show an existing trace

1. Sends the request to the Agent API through the gateway (with a fresh correlation ID).
2. Waits for every component to report its spans to Jaeger.
3. Prints the span tree (who called whom, how long it took, errors) and the Jaeger UI link.

Standard library only.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

GATEWAY = os.getenv("GATEWAY", "http://localhost:8000")
JAEGER = os.getenv("JAEGER", "http://localhost:16686")
KEY_SCENARIO = "Find customer C001 and tell me their latest order status"
TAGS = ("correlation.id", "a2a.skill", "a2a.state", "a2a.outcome", "step.state", "agent.status",
        "error.code", "http.status_code", "http.response.status_code", "plan.steps")


def http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"null")


def fetch_trace(trace_id, wait=20.0):
    """Poll Jaeger until the span count stops growing (components export in batches)."""
    deadline, last, stable, spans = time.time() + wait, -1, 0, None
    while time.time() < deadline:
        try:
            code, _, body = http("GET", f"{JAEGER}/api/traces/{trace_id}")
        except urllib.error.URLError:
            print(f"Jaeger is not reachable at {JAEGER}"); return None
        if code == 200 and body and body.get("data"):
            data = body["data"][0]
            n = len(data["spans"])
            stable = stable + 1 if n == last else 0
            last, spans = n, data
            if stable >= 2:
                break
        time.sleep(1.5)
    return spans


def show(data):
    procs = {pid: p["serviceName"] for pid, p in data["processes"].items()}
    spans = data["spans"]
    ids = {s["spanID"] for s in spans}
    children = {}
    for s in spans:
        parent = next((r["spanID"] for r in s.get("references", []) if r["refType"] == "CHILD_OF"), None)
        children.setdefault(parent if parent in ids else None, []).append(s)
    t0 = min(s["startTime"] for s in spans)

    def line(s, depth):
        tags = {t["key"]: t["value"] for t in s.get("tags", [])}
        err = tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR"
        shown = {k: tags[k] for k in TAGS if k in tags}
        events = [f.get("value") for log in s.get("logs", []) for f in log.get("fields", []) if f["key"] == "event"]
        print(f"{'  ' * depth}{procs[s['processID']]:<18} {s['operationName'][:42]:<42} "
              f"+{(s['startTime'] - t0) / 1000:7.1f}ms {s['duration'] / 1000:7.1f}ms"
              f"{'  ERROR' if err else ''}  {shown if shown else ''}{'  events=' + str(events) if events else ''}")
        for c in sorted(children.get(s["spanID"], []), key=lambda x: x["startTime"]):
            line(c, depth + 1)

    for root in sorted(children.get(None, []), key=lambda x: x["startTime"]):
        line(root, 0)
    services = sorted(set(procs.values()))
    print(f"\n{len(spans)} spans from {len(services)} components: {', '.join(services)}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("query", nargs="?", default=KEY_SCENARIO)
    p.add_argument("--trace-id", help="show an existing trace instead of sending a request")
    p.add_argument("--key", default=os.getenv("API_KEY", "chat-frontend-key"))
    a = p.parse_args()

    trace_id = a.trace_id
    if not trace_id:
        cid = f"trace-{uuid.uuid4().hex[:8]}"
        code, headers, body = http("POST", f"{GATEWAY}/api/agent/query", {"query": a.query},
                                   {"Content-Type": "application/json", "apikey": a.key, "X-Correlation-ID": cid})
        if not isinstance(body, dict) or "answer" not in body:
            print(f"HTTP {code}: {body}"); return 1
        trace_id = headers.get("X-Trace-Id") or headers.get("x-trace-id") or body.get("trace_id")
        print(f"Q: {a.query}\nA: {body['answer']}")
        print(f"HTTP {code} · status={body['status']} · correlation-id={cid} · trace-id={trace_id}\n")
        if not trace_id:
            print("No trace ID returned: is tracing enabled (OTEL_EXPORTER_OTLP_ENDPOINT)?"); return 1

    print("Collecting spans from Jaeger ...\n")
    data = fetch_trace(trace_id)
    if not data:
        print("Trace not found in Jaeger (yet). Try again in a few seconds with --trace-id."); return 1
    show(data)
    print(f"\nOpen in Jaeger: {JAEGER}/trace/{trace_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
