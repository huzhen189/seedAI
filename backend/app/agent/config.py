"""Agent (inference) layer config shim.

Settings used to be defined only inside ai_service/app/config.py. Now both layers
share the single `shared.config.settings`. This shim keeps every
`from .config import settings` (and `from ..config import settings`) resolving
after the merge.
"""

from shared.config import settings  # noqa: F401

__all__ = ["settings"]
