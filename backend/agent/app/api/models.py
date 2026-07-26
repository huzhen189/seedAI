"""Agent Core API contracts — normalized SSE + decision objects.

These are the wire-level shapes exchanged between business, agent and frontend.
Keeping them here (not inside legacy `proxy`) makes the protocol explicit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("agent.api.models")

# Canonical SSE event names (Superset of legacy list, now seq-numbered)
StreamEventType = Literal[
    "intent", "node", "think", "plan", "token", "preview", "qc",
    "requirement_doc", "refined", "clarify", "confirm", "options",
    "paused", "checkpoint", "done", "error", "aborted", "unsupported",
]


class StreamEvent(BaseModel):
    """One SSE frame. `seq` enables resumable replay via stream_exists."""
    event: StreamEventType
    seq: int
    data: dict[str, Any] = Field(default_factory=dict)

    def to_sse(self) -> str:
        """Serialize as `event: <name>\\ndata: <json>\\n\\n`."""
        import json
        return f"event: {self.event}\ndata: {json.dumps({'seq': self.seq, **self.data}, ensure_ascii=False)}\n\n"


# ----- ContextBundle: assembled memory (Memory Manager output) -----
class MessageView(BaseModel):
    role: str
    content: str


class VectorHit(BaseModel):
    score: float
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SlotState(BaseModel):
    prior_intent: Optional[str] = None       # e.g. "build/doc" (sticky belief)
    collected: dict[str, Any] = Field(default_factory=dict)


class ContextBundle(BaseModel):
    """Assembled before classification; slots are ALWAYS present (structurally
    prevents the old 'novelty-early-return bypasses cross-turn memory' bug)."""
    recent_messages: list[MessageView] = Field(default_factory=list)
    project_rule: str = ""
    conv_memory: list[VectorHit] = Field(default_factory=list)
    project_memory: list[VectorHit] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    slots: SlotState = Field(default_factory=SlotState)


# ----- DecisionRequest: Worker's sole input -----
class DecisionRequest(BaseModel):
    """Single normalized input to the Worker pipeline."""
    trace_id: str
    conversation_id: int
    project_id: int
    user_id: int
    q: str
    model: str = "qwen"
    flags: dict[str, bool] = Field(default_factory=dict)   # confirmed / clarified
    context: ContextBundle = Field(default_factory=ContextBundle)
    constraints: dict[str, Any] = Field(default_factory=dict)


# ----- IntentDecision: Router output (classifier only, no execution) -----
IntentDecisionType = Literal["route", "clarify", "block", "chat_casual", "unsupported"]


class ClarifyOption(BaseModel):
    label: str
    value: str
    recommended: bool = False


class IntentDecision(BaseModel):
    decision: IntentDecisionType
    intent: Optional[str] = None          # e.g. "doc" / "build" / "chat"
    skill: Optional[str] = None           # e.g. "task_doc"
    confidence: float = 0.0
    missing_slots: list[str] = Field(default_factory=list)
    clarify_options: list[ClarifyOption] = Field(default_factory=list)
    clarify_multi: bool = False
    clarify_allow_free_text: bool = True
    clarify_free_text_hint: str = ""
    reason: str = ""


# ----- Worker outputs -----
class SubTask(BaseModel):
    name: str
    skill: str
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


# ----- GenerateReq (entry): must declare `clarified` to avoid silent drop (R1) -----
class GenerateReq(BaseModel):
    """Replaces the legacy GenerateReq that lacked `clarified` (caused R1)."""
    trace_id: Optional[str] = None
    conversation_id: int
    project_id: int
    user_id: int
    q: str
    model: str = "qwen"
    confirmed: bool = False
    clarified: bool = False             # FIXED: declared (was dropped by pydantic)
    token: str = ""
