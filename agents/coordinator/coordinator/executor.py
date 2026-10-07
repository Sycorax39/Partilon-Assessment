"""Executor: run a plan by delegating each step to the right agent over A2A.

* Steps whose dependencies are satisfied run IN PARALLEL ("wave" by wave).
* A dependent step runs only if every step it depends on finished with outcome "found".
  Otherwise it is SKIPPED with the reason — e.g. no order lookup for a customer that does
  not exist, or whose existence could not be checked.
* "$s1.customer_id" inputs are filled from earlier results (data flows between agents).
* Every failure becomes a step result with an error code; nothing raises past this module.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from a2a_core import A2AClient, A2AClientError, Task, TaskState
from common.tracing import annotate, event, mark_error, span

from .discovery import AgentRegistry
from .planner import Plan, PlanStep

logger = logging.getLogger(__name__)

AGENT_RETRY_DELAY = 0.3   # seconds before the single retry of a refused connection


@dataclass
class StepResult:
    id: str
    skill: str
    input: dict[str, Any]
    agent: str | None = None
    state: str = "pending"          # completed | failed | rejected | skipped
    outcome: str | None = None      # found | not_found   (completed steps only)
    summary: str | None = None      # the agent's own factual summary
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    task_id: str | None = None
    duration_ms: float | None = None
    reason: str | None = None       # why a step was skipped

    @property
    def found(self) -> bool:
        return self.state == "completed" and self.outcome == "found"


@dataclass
class Execution:
    results: dict[str, StepResult] = field(default_factory=dict)

    def ordered(self, plan: Plan) -> list[StepResult]:
        return [self.results[s.id] for s in plan.steps]


def _resolve_input(step: PlanStep, results: dict[str, StepResult]) -> dict[str, Any]:
    resolved = {}
    for key, value in step.input.items():
        if isinstance(value, str) and value.startswith("$"):
            ref_step, _, ref_field = value[1:].partition(".")
            source = results[ref_step].data or {}
            resolved[key] = source.get(ref_field)
        else:
            resolved[key] = value
    return resolved


def _from_task(result: StepResult, task: Task) -> None:
    result.task_id = task.id
    result.state = task.status.state.value
    parts = task.status.message.parts if task.status.message else []
    result.summary = next((p.text for p in parts if p.kind == "text"), None)
    if task.status.state == TaskState.completed and task.artifacts:
        data = dict(task.artifacts[0].parts[0].data)
        result.outcome = data.pop("outcome", None)
        result.data = data
    else:
        result.error = next((p.data.get("error") for p in parts if p.kind == "data"), None) or {
            "code": "UNKNOWN_ERROR", "message": result.summary}


class Executor:
    def __init__(self, registry: AgentRegistry, client: A2AClient, task_timeout: float = 10.0):
        self.registry = registry
        self.client = client
        self.task_timeout = task_timeout

    async def _run_step(self, step: PlanStep, execution: Execution) -> None:
        with span(f"delegate {step.skill}", **{"step.id": step.id, "a2a.skill": step.skill}) as s:
            await self._delegate(step, execution)
            r = execution.results[step.id]
            annotate(**{"a2a.agent": r.agent, "step.state": r.state, "step.outcome": r.outcome,
                        "a2a.task.id": r.task_id})
            if r.state in ("failed", "rejected"):
                mark_error(s, (r.error or {}).get("code", r.state), (r.error or {}).get("message", ""))

    async def _delegate(self, step: PlanStep, execution: Execution) -> None:
        result = execution.results[step.id]
        started = time.perf_counter()
        try:
            agent = await self.registry.agent_for(step.skill)
            if agent is None:
                result.state = "failed"
                result.error = {"code": "NO_AGENT_FOR_SKILL", "retryable": True,
                                "message": f"No available agent currently offers the skill '{step.skill}'"}
                return
            result.agent = agent.name
            result.input = _resolve_input(step, execution.results)
            logger.info("delegating step", extra={"fields": {
                "event": "a2a.delegate", "step": step.id, "skill": step.skill, "agent": agent.name,
                "input": result.input}})
            for attempt in (1, 2):
                try:
                    task = await self.client.send(agent.endpoint, skill=step.skill, input=result.input,
                                                  timeout=self.task_timeout)
                    _from_task(result, task)
                    break
                except A2AClientError as exc:
                    # Connection refused = the request never reached the agent (e.g. it is restarting),
                    # so one quick retry is safe. Timeouts are NOT retried: the agent may still be
                    # working, and a retry would double the wait.
                    if exc.code == "AGENT_UNREACHABLE" and attempt == 1:
                        event("retry", reason=exc.code, agent=agent.name)
                        logger.warning("agent unreachable, retrying once", extra={"fields": {
                            "event": "a2a.retry", "step": step.id, "agent": agent.name}})
                        await asyncio.sleep(AGENT_RETRY_DELAY)
                        continue
                    self.registry.mark_stale()          # re-discover next time
                    result.state = "failed"
                    result.error = {"code": exc.code, "retryable": exc.retryable, "attempts": attempt,
                                    "message": f"{agent.name} is unavailable: {exc.message}"}
        finally:
            result.duration_ms = round((time.perf_counter() - started) * 1000, 1)
            logger.info("step finished", extra={"fields": {
                "event": "a2a.step", "step": step.id, "skill": step.skill, "agent": result.agent,
                "state": result.state, "outcome": result.outcome,
                "error": (result.error or {}).get("code"), "duration_ms": result.duration_ms}})

    @staticmethod
    def new_execution(plan: Plan) -> Execution:
        return Execution({s.id: StepResult(id=s.id, skill=s.skill, input=dict(s.input)) for s in plan.steps})

    async def run(self, plan: Plan, execution: Execution) -> Execution:
        """Fills `execution` in place, so a caller that times out still sees finished steps."""
        remaining = list(plan.steps)
        while remaining:
            done = {sid for sid, r in execution.results.items() if r.state != "pending"}
            ready = [s for s in remaining if all(d in done for d in s.depends_on)]
            if not ready:                                   # cannot happen with planner output
                for s in remaining:
                    execution.results[s.id].state = "skipped"
                    execution.results[s.id].reason = "unresolvable dependency"
                break
            runnable = []
            for step in ready:
                blockers = [execution.results[d] for d in step.depends_on if not execution.results[d].found]
                if blockers:
                    b = blockers[0]
                    why = "was not found" if b.outcome == "not_found" else f"did not complete ({b.state})"
                    execution.results[step.id].state = "skipped"
                    execution.results[step.id].reason = f"step {b.id} ({b.skill}) {why}"
                    event("step skipped", step=step.id, skill=step.skill, reason=execution.results[step.id].reason)
                else:
                    runnable.append(step)
            await asyncio.gather(*(self._run_step(s, execution) for s in runnable))
            remaining = [s for s in remaining if s not in ready]
        return execution
