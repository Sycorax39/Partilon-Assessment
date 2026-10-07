"""Planner: turn a natural-language request into a plan of skill calls.

Deterministic on purpose (the brief allows it): the same request always produces the same
plan, so the demo is reproducible and every decision can be explained. The planner only
decides WHAT is needed (skills and inputs); it does not know which agent provides a skill.
That is resolved at run time from the agents' Agent Cards (discovery).

Rules, in order:
  order ID (O1001)       -> get_order
  customer ID (C001)     -> get_customer
      + "latest/last/recent ... order"                -> + get_latest_order   (after the customer is found)
      + "orders"/"order history"                      -> + list_customer_orders (after the customer is found)
      + "order"/"order status" and no order ID given  -> + get_latest_order
  email address          -> find_customer_by_email (only when no customer ID is given)
      + any order wording                             -> + get_latest_order using the customer ID
                                                          FOUND by the email lookup (data flows between steps)
  nothing recognised     -> no plan ("unsupported"), with examples of supported requests
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_ENTITIES = 5   # bounds the fan-out of one request

_CUSTOMER_ID = re.compile(r"\bC\d{3,6}\b", re.IGNORECASE)
_ORDER_ID = re.compile(r"\bO\d{4,8}\b", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ORDER_WORD = re.compile(r"\border(s)?\b|\bpurchase(s)?\b", re.IGNORECASE)
_LATEST = re.compile(r"\b(latest|last|recent|newest|most recent|current)\b", re.IGNORECASE)
_HISTORY = re.compile(r"\b(orders|order history|all (of )?(their|his|her) orders|purchases)\b", re.IGNORECASE)

SUPPORTED_EXAMPLES = [
    "Show customer C001",
    "Show customer C001 and their latest order",
    "Find customer C001 and tell me their latest order status",
    "What is the status of order O1001?",
    "Show the orders of customer C001",
    "What is the latest order of somchai.j@example.com?",
]


@dataclass
class PlanStep:
    id: str                        # "s1", "s2", ...
    skill: str                     # capability needed; the agent is found via discovery
    input: dict[str, Any]          # values, or "$s1.customer_id" references to earlier results
    purpose: str                   # why this step exists (shown to the user)
    depends_on: list[str] = field(default_factory=list)   # run only if these were FOUND


@dataclass
class Plan:
    query: str
    steps: list[PlanStep]
    reasoning: list[str]           # the decisions taken, in plain language

    @property
    def supported(self) -> bool:
        return bool(self.steps)


def _unique(matches: list[str]) -> list[str]:
    seen: list[str] = []
    for m in matches:
        m = m.upper()
        if m not in seen:
            seen.append(m)
    return seen[:MAX_ENTITIES]


def make_plan(query: str) -> Plan:
    text = " ".join(query.split())
    customer_ids = _unique(_CUSTOMER_ID.findall(text))
    order_ids = _unique(_ORDER_ID.findall(text))
    emails = [] if customer_ids else list(dict.fromkeys(e.lower() for e in _EMAIL.findall(text)))[:MAX_ENTITIES]

    mentions_order = bool(_ORDER_WORD.search(text))
    wants_latest = mentions_order and bool(_LATEST.search(text))
    wants_history = bool(_HISTORY.search(text)) and not wants_latest
    # "...customer C001 and their order status" with no order ID given means the latest order.
    wants_customer_orders = wants_latest or wants_history or (mentions_order and not order_ids)

    steps: list[PlanStep] = []
    reasoning: list[str] = []

    def add(skill: str, input: dict, purpose: str, depends_on: list[str] | None = None) -> str:
        step = PlanStep(id=f"s{len(steps) + 1}", skill=skill, input=input, purpose=purpose,
                        depends_on=depends_on or [])
        steps.append(step)
        return step.id

    order_skill = "list_customer_orders" if wants_history else "get_latest_order"
    order_purpose = "List the customer's orders" if wants_history else "Find the customer's latest order"

    for cid in customer_ids:
        sid = add("get_customer", {"customer_id": cid}, f"Confirm customer {cid} exists and get the profile")
        reasoning.append(f"Customer ID {cid} mentioned -> skill get_customer.")
        if wants_customer_orders:
            add(order_skill, {"customer_id": cid}, f"{order_purpose} ({cid})", depends_on=[sid])
            reasoning.append(f"Order information for {cid} requested -> skill {order_skill}, "
                             f"only after {cid} is confirmed to exist.")

    for email in emails:
        sid = add("find_customer_by_email", {"email": email}, f"Find the customer with email {email}")
        reasoning.append(f"Email {email} mentioned (no customer ID) -> skill find_customer_by_email.")
        if mentions_order:
            add(order_skill, {"customer_id": f"${sid}.customer_id"}, f"{order_purpose} (customer found by email)",
                depends_on=[sid])
            reasoning.append(f"Order information requested -> skill {order_skill} using the customer ID "
                             f"returned by step {sid}.")

    for oid in order_ids:
        add("get_order", {"order_id": oid}, f"Look up order {oid} and its status")
        reasoning.append(f"Order ID {oid} mentioned -> skill get_order.")

    if not steps:
        reasoning.append("No customer ID (e.g. C001), order ID (e.g. O1001) or email address found; "
                         "the request is outside what this assistant supports.")
    return Plan(query=query, steps=steps, reasoning=reasoning)
