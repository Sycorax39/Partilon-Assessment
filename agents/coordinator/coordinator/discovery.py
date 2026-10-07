"""Agent discovery: learn which agent offers which skill by reading their Agent Cards.

The coordinator is configured with agent *addresses* only (AGENT_URLS). Capabilities are
discovered: on startup it fetches each /.well-known/agent-card.json and builds a
skill -> agent registry. The planner asks for skills ("get_latest_order"); this registry
answers "the Order Agent at http://order-agent:8000/ provides it".

Resilience:
  * an agent that is down at startup is simply missing from the registry; it is retried
    later, so the coordinator starts and serves what it can;
  * the registry refreshes when it is older than CARD_TTL_SECONDS, or immediately when a
    needed skill is missing (at most once per MIN_REFRESH_INTERVAL);
  * when an agent becomes unreachable it is marked stale so the next request re-discovers it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from a2a_core import A2AClient, A2AClientError, AgentCard

logger = logging.getLogger(__name__)

CARD_TTL_SECONDS = 60.0
MIN_REFRESH_INTERVAL = 5.0


@dataclass
class DiscoveredAgent:
    base_url: str          # where we found the card
    card: AgentCard

    @property
    def name(self) -> str:
        return self.card.name

    @property
    def endpoint(self) -> str:
        return self.card.url or self.base_url


class AgentRegistry:
    def __init__(self, base_urls: list[str], client: A2AClient, card_timeout: float = 3.0):
        self.base_urls = [u.rstrip("/") for u in base_urls if u.strip()]
        self.client = client
        self.card_timeout = card_timeout
        self.agents: dict[str, DiscoveredAgent] = {}         # base_url -> agent
        self.unreachable: dict[str, str] = {}                # base_url -> error code
        self._refreshed_at = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self, base_url: str) -> None:
        try:
            card = await asyncio.wait_for(self.client.get_card(base_url), timeout=self.card_timeout)
            self.agents[base_url] = DiscoveredAgent(base_url, card)
            self.unreachable.pop(base_url, None)
        except (A2AClientError, asyncio.TimeoutError) as exc:
            code = getattr(exc, "code", "AGENT_TIMEOUT")
            self.agents.pop(base_url, None)
            self.unreachable[base_url] = code
            logger.warning("agent discovery failed", extra={"fields": {"agent_url": base_url, "error": code}})

    async def refresh(self, force: bool = False) -> None:
        async with self._lock:
            age = time.monotonic() - self._refreshed_at
            if not force and age < CARD_TTL_SECONDS:
                return
            if force and age < MIN_REFRESH_INTERVAL:
                return
            await asyncio.gather(*(self._fetch(u) for u in self.base_urls))
            self._refreshed_at = time.monotonic()
            logger.info("agents discovered", extra={"fields": {
                "event": "a2a.discovery",
                "agents": {a.name: [s.id for s in a.card.skills] for a in self.agents.values()},
                "unreachable": self.unreachable}})

    def _lookup(self, skill_id: str) -> DiscoveredAgent | None:
        for agent in self.agents.values():
            if any(s.id == skill_id for s in agent.card.skills):
                return agent
        return None

    async def agent_for(self, skill_id: str) -> DiscoveredAgent | None:
        await self.refresh()
        agent = self._lookup(skill_id)
        if agent is None:                       # maybe a new or recovered agent offers it now
            await self.refresh(force=True)
            agent = self._lookup(skill_id)
        return agent

    def mark_stale(self) -> None:
        self._refreshed_at = 0.0

    def snapshot(self) -> dict:
        return {
            "agents": [{"name": a.name, "url": a.endpoint, "description": a.card.description,
                        "protocolVersion": a.card.protocolVersion,
                        "skills": [{"id": s.id, "description": s.description} for s in a.card.skills]}
                       for a in self.agents.values()],
            "unreachable": [{"url": u, "error": e} for u, e in self.unreachable.items()],
        }
