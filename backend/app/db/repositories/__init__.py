from .entities import (
    approvals,
    artifacts,
    conversations,
    deployments,
    messages,
    outbox,
    projects,
    purge_jobs,
    tasks,
    tombstones,
    tool_calls,
    turn_checkpoints,
    turns,
    usage_ledger,
    users,
)
from .sir_snapshots import SirSnapshotsRepo
from .memories import MemoryRepo
from .project_events import ProjectEventRepo
from .project_facts import ProjectFactRepo
from .user_facts import UserFactRepo
from .user_soft_preferences import UserSoftPreferenceRepo

# SIR 快照仓储（S1 读基态 / S3 落合并结果）。此前未在包级导出，
# 调用方只能 `from app.db.repositories import sir_snapshots` 命中子模块，
# 类型检查看不到符号，改错也无法在 mypy 阶段暴露。
sir_snapshots = SirSnapshotsRepo()
memories = MemoryRepo()
project_events = ProjectEventRepo()
project_facts = ProjectFactRepo()
user_facts = UserFactRepo()
user_soft_preferences = UserSoftPreferenceRepo()

__all__ = [
    "approvals",
    "artifacts",
    "conversations",
    "deployments",
    "memories",
    "messages",
    "outbox",
    "project_events",
    "project_facts",
    "projects",
    "purge_jobs",
    "sir_snapshots",
    "tasks",
    "tombstones",
    "tool_calls",
    "turn_checkpoints",
    "turns",
    "usage_ledger",
    "user_facts",
    "user_soft_preferences",
    "users",
]
