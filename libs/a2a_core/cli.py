"""Tiny A2A command-line client, for demos and debugging inside the Docker network.

    python -m a2a_core.cli discover http://order-agent:8000
    python -m a2a_core.cli send     http://order-agent:8000 get_order order_id=O1001
    python -m a2a_core.cli ask      http://order-agent:8000 "latest order for C001"
    python -m a2a_core.cli get      http://order-agent:8000 <task-id>

Run it from any agent container, e.g.:
    docker compose exec -T coordinator-agent python -m a2a_core.cli discover http://customer-agent:8000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from common.observability import resolve_correlation_id, set_correlation_id

from .client import A2AClient, A2AClientError


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _print_brief(task) -> None:
    status = task.status
    parts = status.message.parts if status.message else []
    answer = next((p.text for p in parts if p.kind == "text"), "")
    error = next((p.data.get("error") for p in parts if p.kind == "data"), None)
    history = " -> ".join(h["state"] for h in task.metadata.get("statusHistory", []))
    print(f"  task     : {task.id}")
    print(f"  skill    : {task.metadata.get('skill')}")
    print(f"  status   : {status.state.value}   ({history})")
    print(f"  answer   : {answer}")
    if error:
        print(f"  error    : {error.get('code')} (retryable={error.get('retryable')})")
    if task.artifacts:
        data = task.artifacts[0].parts[0].data
        print(f"  outcome  : {data.get('outcome')}")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="a2a_core.cli", description="A2A command-line client")
    parser.add_argument("--correlation-id", help="reuse a correlation ID (default: new UUID)")
    parser.add_argument("--brief", action="store_true", help="print a short summary instead of the full task JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover", help="fetch and print an agent's Agent Card"); p.add_argument("url")
    p = sub.add_parser("send", help="call a skill with key=value inputs")
    p.add_argument("url"); p.add_argument("skill"); p.add_argument("inputs", nargs="*")
    p = sub.add_parser("ask", help="send a plain-text request; the agent picks the skill")
    p.add_argument("url"); p.add_argument("text")
    p = sub.add_parser("get", help="fetch a task by ID"); p.add_argument("url"); p.add_argument("task_id")
    args = parser.parse_args(argv)

    cid = resolve_correlation_id(args.correlation_id)
    set_correlation_id(cid)
    print(f"# correlation-id: {cid}", file=sys.stderr)

    client = A2AClient()
    try:
        if args.cmd == "discover":
            card = await client.get_card(args.url)
            _print(card.model_dump(mode="json", exclude_none=True))
            return 0
        if args.cmd == "send":
            inputs = dict(kv.split("=", 1) for kv in args.inputs)
            task = await client.send(args.url, skill=args.skill, input=inputs)
        elif args.cmd == "ask":
            task = await client.send(args.url, text=args.text)
        else:
            task = await client.get_task(args.url, args.task_id)
        if args.brief:
            _print_brief(task)
        else:
            _print(task.model_dump(mode="json", exclude_none=True))
        print(f"# task {task.id}: {task.status.state.value}", file=sys.stderr)
        return 0
    except A2AClientError as exc:
        _print({"client_error": {"code": exc.code, "message": exc.message}})
        return 2
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
