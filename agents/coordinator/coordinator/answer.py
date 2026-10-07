"""Answer composer: build the reply ONLY from what the agents returned.

Every sentence comes from a fixed template filled with fields of a step result, so the
coordinator cannot state anything an agent did not report. Unavailable information is
said to be unavailable, never guessed.

Overall status:
  completed    every step finished (found, or a verified "no orders")
  not_found    every thing the user asked about (customer / order / email) does not exist
  partial      some information was retrieved, some could not be (failure or timeout)
  failed       nothing could be retrieved because of failures               -> HTTP 503
  unsupported  the request was not understood; no agent was called
"""
from __future__ import annotations

from .executor import StepResult
from .planner import SUPPORTED_EXAMPLES, Plan

PRIMARY_LOOKUPS = {"get_customer", "get_order", "find_customer_by_email"}
CUSTOMER_SKILLS = {"get_customer", "find_customer_by_email"}

_REASONS = {
    "BACKEND_UNAVAILABLE": "the backend service is unavailable",
    "GATEWAY_UNREACHABLE": "the API gateway cannot be reached",
    "UPSTREAM_TIMEOUT": "the backend did not respond in time",
    "SKILL_TIMEOUT": "the agent did not finish in time",
    "QUERY_TIMEOUT": "the request took too long",
    "RATE_LIMITED": "the request limit was reached",
    "ACCESS_DENIED": "access was denied (configuration problem)",
    "AGENT_UNREACHABLE": "the responsible agent is not reachable",
    "AGENT_TIMEOUT": "the responsible agent did not respond in time",
    "NO_AGENT_FOR_SKILL": "no agent offering this capability is available",
}


def _subject(r: StepResult) -> str:
    i = r.input
    if "order_id" in i:
        return f"order {i['order_id']}"
    if i.get("customer_id") and not str(i["customer_id"]).startswith("$"):
        return f"customer {i['customer_id']}"
    if "email" in i:
        return f"the customer with email {i['email']}"
    return "this customer"


def _order_facts(o: dict) -> str:
    return (f"total {o['total_amount']} {o['currency']}, placed {o['created_at'][:10]}, "
            f"last updated {o['updated_at'][:10]}")


def _sentence(r: StepResult) -> str | None:
    d = r.data or {}
    if r.state == "completed":
        if r.skill == "get_customer":
            c = d.get("customer")
            return (f"Customer {c['customer_id']} is {c['name']} ({c['tier']} tier, {c['email']})." if c
                    else f"Customer {r.input['customer_id']} was not found.")
        if r.skill == "find_customer_by_email":
            c = d.get("customer")
            return (f"The customer with email {r.input['email']} is {c['customer_id']} ({c['name']}, {c['tier']} tier)."
                    if c else f"No customer was found with the email {r.input['email']}.")
        if r.skill == "get_order":
            o = d.get("order")
            return (f"Order {o['order_id']} (customer {o['customer_id']}) is {o['status']} ({_order_facts(o)})."
                    if o else f"Order {r.input['order_id']} was not found.")
        if r.skill == "get_latest_order":
            o = d.get("order")
            return (f"Their latest order is {o['order_id']}, with status {o['status']} ({_order_facts(o)})."
                    if o else "They have no orders.")
        if r.skill == "list_customer_orders":
            orders = d.get("orders") or []
            if not orders:
                return "They have no orders."
            listing = ", ".join(f"{o['order_id']} ({o['status']}, {o['created_at'][:10]})" for o in orders)
            return f"They have {d.get('total_orders', len(orders))} order(s). Most recent first: {listing}."
        return r.summary
    if r.state == "skipped":
        what = "Order information" if r.skill not in CUSTOMER_SKILLS else "Customer information"
        if "was not found" in (r.reason or ""):
            return f"{what} was not looked up because the customer does not exist."
        return f"{what} was not looked up because the customer could not be verified."
    domain = "Customer information" if r.skill in CUSTOMER_SKILLS else "Order information"
    if r.state == "rejected":
        return f"{domain} for {_subject(r)} could not be requested: {(r.error or {}).get('message', 'invalid request')}."
    code = (r.error or {}).get("code", "UNKNOWN_ERROR")
    reason = _REASONS.get(code, "an unexpected error occurred")
    return f"{domain} for {_subject(r)} is currently unavailable ({reason}). Please try again later."


def compose(plan: Plan, results: list[StepResult]) -> tuple[str, str]:
    """Return (status, answer)."""
    if not plan.supported:
        examples = "; ".join(f'"{e}"' for e in SUPPORTED_EXAMPLES[:4])
        return "unsupported", ("I can answer questions about ComCo customers and orders, but I could not find "
                               f"a customer ID, order ID or email address in your request. Try for example: {examples}.")

    answer = " ".join(s for s in (_sentence(r) for r in results) if s)
    succeeded = [r for r in results if r.state == "completed"]
    problems = [r for r in results if r.state in ("failed", "rejected")]

    if not succeeded:
        return "failed", answer
    if problems:
        return "partial", answer
    primary = [r for r in results if r.skill in PRIMARY_LOOKUPS and r.state == "completed"]
    if primary and all(r.outcome == "not_found" for r in primary):
        return "not_found", answer
    return "completed", answer
