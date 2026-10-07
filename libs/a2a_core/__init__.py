"""Minimal implementation of the Agent2Agent (A2A) protocol, v0.3 JSON-RPC binding.

  models.py  Agent Card, Message, Task, status and JSON-RPC shapes
  server.py  A2AAgent: publish the card, execute tasks (message/send, tasks/get)
  client.py  A2AClient: discover agents, send tasks
  cli.py     command-line client for demos

Written by hand (instead of the a2a-sdk package) to keep the protocol visible and explainable
in a small amount of code. The wire format follows the A2A specification.
"""
from .client import A2AClient, A2AClientError
from .models import AgentCard, Task, TaskState
from .server import A2AAgent, SkillError, SkillRejected, SkillResult

__all__ = ["A2AAgent", "A2AClient", "A2AClientError", "AgentCard", "SkillError", "SkillRejected",
           "SkillResult", "Task", "TaskState"]
