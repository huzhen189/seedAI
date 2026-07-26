"""Business-layer config shim.

The unified single-process app shares ONE Settings object (no more two separate
configs). Every legacy `from .config import settings` in the business layer now
resolves to the single shared Settings. Keep ENV_FILE exported for db.reset_db.
"""

from shared.config import settings, ENV_FILE  # noqa: F401

__all__ = ["settings", "ENV_FILE"]
