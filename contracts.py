"""GitHub Watch 与中立 runtime 能力的本地结构合同。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import Context, ServiceKey
from agent.plugin_composition.messages import MessageCatalog
from agent.plugin_contracts import CallRef, ContentPart, Input, Message, json_value


Outcome = Literal["success", "denied", "error", "interrupted"]


@dataclass(frozen=True, slots=True)
class Result:
    """工具 owner 接受的最小结果结构。"""

    outcome: Outcome
    parts: tuple[ContentPart, ...]


class InvalidArguments(ValueError):
    """工具参数或调用来源不满足声明合同。"""


class CallSource(Protocol):
    @property
    def call_ref(self) -> CallRef: ...

    @property
    def messages(self) -> tuple[Message, ...]: ...


class BoundTool(Protocol):
    idempotent: bool

    async def prepare(
        self,
        arguments: Mapping[str, object],
        source: CallSource | None = None,
    ) -> Mapping[str, object] | str: ...

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result: ...

    async def query(self, key: str) -> Result | None: ...


class ToolRef(Protocol):
    name: str
    description: Mapping[str, object]


class ToolView(Protocol):
    refs: tuple[ToolRef, ...]


class ToolCatalog(Protocol):
    async def declare_group(
        self,
        ctx: Context,
        *,
        always_on: bool = False,
        description: str = "未声明用途",
    ) -> object: ...

    async def register(
        self,
        ctx: Context,
        *,
        name: str,
        description: str,
        parameters: Mapping[str, object],
        open: Callable[[Mapping[str, object]], AbstractAsyncContextManager[BoundTool]],
        capture: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        public: bool = True,
        idempotent: bool = False,
        risk: Literal["read-only", "read-write", "external-side-effect"] = "read-write",
        search_hint: str | None = None,
    ) -> ToolRef: ...

    def view(self, *refs: ToolRef) -> ToolView: ...


TOOLS = ServiceKey[ToolCatalog]("tools.v1")


class Programmatic(BaseModel):
    """Marker base only for local typed request declarations."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SessionIdParams(Programmatic):
    session_id: str = Field(min_length=1, max_length=512)


class AdmitParams(SessionIdParams):
    persist_memory: bool = False


class SendParams(SessionIdParams):
    message_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=1_048_576)


class RequestTransport(Protocol):
    connection_id: str


class ProgrammaticService(Protocol):
    async def call(
        self,
        method: str,
        params: BaseModel,
        transport: RequestTransport | None = None,
    ) -> dict[str, object]: ...


PROGRAMMATIC = ServiceKey[ProgrammaticService]("programmatic.v1")


class Turn(Protocol):
    status: Literal["open", "complete", "quiet", "abandoned"]
    message_ids: tuple[str, ...]


class TurnProjection(Protocol):
    def project(self, messages: Sequence[Message], source: str) -> tuple[Turn, ...]: ...


TURN_PROJECTION = ServiceKey[TurnProjection]("turn.projection.v1")


__all__ = [
    "AdmitParams",
    "BoundTool",
    "CallSource",
    "ContentPart",
    "Input",
    "InvalidArguments",
    "Message",
    "MessageCatalog",
    "PROGRAMMATIC",
    "ProgrammaticService",
    "RequestTransport",
    "Result",
    "SendParams",
    "TOOLS",
    "ToolCatalog",
    "ToolRef",
    "ToolView",
    "TURN_PROJECTION",
    "Turn",
    "TurnProjection",
    "json_value",
]
