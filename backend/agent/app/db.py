"""Agent DB access — import the shared engine/models directly.

Microservice pattern: agent and business share the same code (backend/shared),
so the agent can read/write MySQL directly. No copy of models lives here.
"""

from __future__ import annotations

from shared.db import SessionLocal, engine, get_db, init_db, dispose_engine  # re-export
from shared.models import (  # re-export models for convenience
    Base, User, Project, Conversation, Message, Artifact, Trace,
)
