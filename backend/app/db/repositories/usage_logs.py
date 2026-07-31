from __future__ import annotations

from app.models import UsageLog

from ._base import BaseRepo


class UsageLogsRepo(BaseRepo[UsageLog]):
    model = UsageLog


usage_log_repo = UsageLogsRepo()
