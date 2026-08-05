"""L3 动态业务槽的持久化：沉淀为该用户/项目的持久偏好（含更新语义）。

决策 3：动态槽需要持久化；若之前已有同 key，则**更新**而非重复新增。
通过确定性 doc id（``dynpref:{user_id}:{slot_key}``）+ Chroma upsert 实现「同 id 即覆盖」。
"""
from __future__ import annotations

from app.config import settings
from app.ragstore import upsert as _rag_upsert


def _dyn_doc_id(user_id: int, slot_key: str) -> str:
    """动态槽在 user_preferences 集合中的确定性 id。同 (user_id, slot_key) → 同 id → upsert 即更新。"""
    return f"dynpref:{user_id}:{slot_key}"


async def persist_dynamic_slot(user_id: int, slot_key: str, label: str) -> int:
    """把动态槽持久化进 user_preferences（按 user_id 隔离，同 key 更新）。返回写入条数。"""
    doc_id = _dyn_doc_id(user_id, slot_key)
    return await _rag_upsert(
        settings.chroma_collection_user_preferences,
        documents=[f"用户动态偏好: {label} ({slot_key})"],
        metadatas=[{"kind": "dynamic_slot", "user_id": user_id, "slot_key": slot_key}],
        ids=[doc_id],
    )
