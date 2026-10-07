"""A2A protocol data model (subset of A2A v0.3, JSON-RPC binding).

Shapes follow the open Agent2Agent specification so the agents interoperate in the same
way real A2A agents do:

  AgentCard   published at /.well-known/agent-card.json — who the agent is, where it lives,
              which skills it offers. Used for discovery.
  Message     what a client sends: a role plus "parts" (text and/or structured data).
  Task        the unit of work the agent creates for a message, with a lifecycle status
              (submitted -> working -> completed | failed | rejected) and result artifacts.

Not implemented (documented as limitations): streaming (message/stream), push notifications,
task cancellation, multi-turn input-required flows, authentication between agents.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = "0.3.0"


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# ---- Agent Card (discovery) ----------------------------------------------------------------------

class AgentSkill(_Model):
    id: str
    name: str
    description: str
    tags: list[str] = []
    examples: list[str] = []
    inputModes: list[str] = ["application/json", "text/plain"]
    outputModes: list[str] = ["application/json", "text/plain"]
    # Extension (not in the A2A spec): JSON schema of the structured input the skill accepts,
    # so a coordinator can build a valid request from the card alone.
    inputSchema: dict[str, Any] | None = None


class AgentCapabilities(_Model):
    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = True


class AgentProvider(_Model):
    organization: str
    url: str | None = None


class AgentCard(_Model):
    protocolVersion: str = PROTOCOL_VERSION
    name: str
    description: str
    url: str
    version: str
    preferredTransport: str = "JSONRPC"
    provider: AgentProvider | None = None
    capabilities: AgentCapabilities = AgentCapabilities()
    defaultInputModes: list[str] = ["application/json", "text/plain"]
    defaultOutputModes: list[str] = ["application/json", "text/plain"]
    skills: list[AgentSkill]


# ---- Messages and parts --------------------------------------------------------------------------

class TextPart(_Model):
    kind: Literal["text"] = "text"
    text: str


class DataPart(_Model):
    kind: Literal["data"] = "data"
    data: dict[str, Any]


Part = Annotated[Union[TextPart, DataPart], Field(discriminator="kind")]


class Message(_Model):
    kind: Literal["message"] = "message"
    role: Literal["user", "agent"]
    parts: list[Part] = Field(min_length=1)
    messageId: str = Field(default_factory=new_id)
    taskId: str | None = None
    contextId: str | None = None
    metadata: dict[str, Any] | None = None


# ---- Tasks ---------------------------------------------------------------------------------------

class TaskState(str, Enum):
    submitted = "submitted"
    working = "working"
    completed = "completed"
    failed = "failed"
    rejected = "rejected"

    @property
    def is_final(self) -> bool:
        return self in (TaskState.completed, TaskState.failed, TaskState.rejected)


class TaskStatus(_Model):
    state: TaskState
    message: Message | None = None      # explanation for the client (e.g. why it failed)
    timestamp: str = Field(default_factory=now_iso)


class Artifact(_Model):
    artifactId: str = Field(default_factory=new_id)
    name: str
    description: str | None = None
    parts: list[Part]


class Task(_Model):
    kind: Literal["task"] = "task"
    id: str = Field(default_factory=new_id)
    contextId: str = Field(default_factory=new_id)
    status: TaskStatus
    artifacts: list[Artifact] = []
    history: list[Message] = []
    metadata: dict[str, Any] = {}


# ---- JSON-RPC 2.0 envelope -----------------------------------------------------------------------

class JsonRpcError(_Model):
    code: int
    message: str
    data: Any | None = None


# Standard JSON-RPC codes plus the A2A-specific ones used here.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001


class MessageSendParams(_Model):
    message: Message
    metadata: dict[str, Any] | None = None


class TaskQueryParams(_Model):
    id: str
