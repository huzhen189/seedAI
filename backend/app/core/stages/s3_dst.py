from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

from app.core.contracts import SirState, StageId, StageStatus
from app.core.turn_context import TurnContext
from app.db.repositories import sir_snapshots as sir_repo
from app.slots import persist_dynamic_slot  # A 方案 L3：动态槽持久化（含更新语义）
from .base import BaseStage

# 多值槽位：按**并集**合并（保序去重），而不是整体覆盖。
# 语义原因：「再加一个联系我板块」不应抹掉上一轮已确认的板块。
_UNION_SLOTS = frozenset({"site.sections", "site.style"})
# 多值槽位防御性上限：union 分支只增不删（除非显式给 [] 清空），不封顶会被多轮
# 狂加板块/风格撑爆 system prompt。超出则保留最近 N 个（旧者视为过期遗忘），
# 与 constraints/pending 的 [-20:] 同套路；N 取少量冗余余量（真实站点极少超 12 板块）。
_UNION_SLOT_CAP = 24
# 清空指令的哨兵值：delta 里显式给 [] 表示"用户要求清空该多值槽位"，
# 与"本轮没提到"（key 不存在）严格区分，否则永远无法删除已沉淀的板块。
_CLEAR = "__clear__"


class S3DstStage(BaseStage):
    """S3 槽位/约束合并（§5.6，DST = Dialogue State Tracking）。

    ## 合并对象（明确定义，此前含糊）
      - **左操作数 `sir_base`**：会话级**跨轮持久基态**，由 S1 从 ``sir_snapshots``
        取最新 base 快照加载；回溯控制（correct/supplement）时取 **prior_turn 的快照**，
        语义即"回滚到上一轮结束时的状态"。
      - **右操作数 `sir_delta`**：本轮 S2 从用户话语确定性抽取的**增量**
        （槽位/约束/待办/记忆提示）。
      - **输出 `sir_after_dst`**：合并结果，是 S6 建站、S7 固化、下一轮 S1 的唯一真相。

    ## 合并策略（按槽位语义分流，而非一律 dict 覆盖）
      - 单值槽位（如 ``site.theme``/``site.type``）：**后写覆盖**——用户改主意以最新为准；
      - 多值槽位（``site.sections``/``site.style``）：**并集累积**——"再加一个板块"只增不抹；
        delta 显式给 ``[]`` 视为清空指令（与"本轮没提"区分）；
      - constraints：按 ``kind`` 去重后覆盖同类，避免同一维度约束无限堆叠（旧实现是
        无脑 extend，10 轮对话后能堆出 10 条互相矛盾的 theme 约束）；
      - pending / memory_hints：追加并截断到最近 20 条，防止无界增长。

    ## 可追溯性（本次优化重点）
      每次合并都产出一份结构化 ``sir_diff``，并同时落到三个位置：
        1. ``sir_snapshots`` 表（kind=base，带 ``prev_snapshot_id`` 形成快照链，可回放）；
        2. ``StageResult.output_refs``（SSE stage 事件透出，前端/调试可见）；
        3. 一条 INFO 日志，含 added/updated/removed/unchanged 四类槽位键。
      ``sir_diff`` 结构：
        ``{"added":[...], "updated":[{"key","from","to"}], "removed":[...],
           "unchanged_count":N, "base_snapshot_id":X, "result_snapshot_id":Y}``
    """

    stage_id = StageId.S3

    async def run(self, context: TurnContext):
        base = context.sir_base
        if context.understanding is None:
            logger.debug("[S3] 无理解结果，透传基态 turn=%s", context.turn_id)
            context.sir_after_dst = base
            context.sir_diff = {"added": [], "updated": [], "removed": [], "unchanged_count": len(base.slots)}
            return self.result(StageStatus.NO_OP, "understanding_missing")

        delta = context.understanding.sir_delta
        merged_slots, diff = self._merge_slots(base.slots, delta.slots)
        merged = SirState(
            slots=merged_slots,
            constraints=self._merge_constraints(base.constraints, delta.constraints),
            pending=[*base.pending, *delta.pending][-20:],
            memory_hints=[*base.memory_hints, *delta.memory_hints][-20:],
        )
        context.sir_after_dst = merged

        # A 方案 L3 动态槽持久化：把本轮识别出的 L3 动态业务槽沉淀为该用户/项目的持久偏好。
        # 来源是 understanding.slot_stack（A 方案分层槽位栈），其中 ``dyn_`` 前缀槽由
        # ``detect_dynamic_slots`` 在 S2 注入，代表用户提及的业务概念（会员 / 积分 / 预约 …）。
        # 确定性 doc id（``dynpref:{user_id}:{key}``）+ Chroma upsert → 同 key 自动更新
        # （满足决策 3「若之前有的话，可能还要进行更新」）。
        dyn_slots = []
        ss = context.understanding.slot_stack if context.understanding else None
        if isinstance(ss, dict):
            for s in ss.get("slots", []):
                if isinstance(s, dict) and str(s.get("key", "")).startswith("dyn_"):
                    dyn_slots.append(s)
        if dyn_slots:
            try:
                for s in dyn_slots:
                    await persist_dynamic_slot(
                        context.user.user_id, str(s["key"]), str(s.get("label", s["key"]))
                    )
                logger.info(
                    "[S3] 持久化 L3 动态槽 %d 个 user=%s keys=%s",
                    len(dyn_slots), context.user.user_id, [s["key"] for s in dyn_slots],
                )
            except Exception as exc:  # noqa: BLE001 — 持久化失败不得中断 DST 主链路
                logger.warning("[S3] 持久化动态槽失败(非致命): %s", exc, exc_info=True)

        changed = bool(diff["added"] or diff["updated"] or diff["removed"])
        diff["base_snapshot_id"] = context.sir_base_snapshot_id

        # 可追溯：合并结果落快照，形成 prev_snapshot_id 链，供下一轮 S1 加载与事后回放。
        # 只在真正有变化时落库，避免纯闲聊轮刷出大量同内容快照。
        if changed and self.session is not None:
            try:
                snap = await sir_repo.insert(
                    self.session,
                    conversation_id=context.session.conversation_id,
                    turn_id=context.turn_id,
                    kind="base",
                    snapshot=merged.model_dump(),
                    prev_snapshot_id=context.sir_base_snapshot_id,
                )
                context.sir_after_dst_snapshot_id = snap.id
                diff["result_snapshot_id"] = snap.id
            except Exception as exc:  # noqa: BLE001 — 快照失败不得中断主链路
                logger.warning("[S3] 落 SIR 快照失败(非致命): %s", exc, exc_info=True)
                diff["snapshot_error"] = str(exc)[:200]

        context.sir_diff = diff
        logger.info(
            "[S3] DST 合并 turn=%s base_snap=%s -> snap=%s | +%s ~%s -%s (unchanged=%d)",
            context.turn_id, context.sir_base_snapshot_id, context.sir_after_dst_snapshot_id,
            diff["added"], [u["key"] for u in diff["updated"]], diff["removed"], diff["unchanged_count"],
        )

        status = StageStatus.COMPLETED if changed else StageStatus.NO_OP
        return self.result(
            status,
            "sir_merged" if changed else "sir_no_change",
            output_refs=self._trace_refs(diff),
        )

    # ------------------------------------------------------------ 合并原语

    @staticmethod
    def _merge_slots(
        base: dict[str, Any], delta: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """按槽位语义合并，并产出结构化 diff。"""
        merged = dict(base)
        added: list[str] = []
        updated: list[dict[str, Any]] = []
        removed: list[str] = []

        for key, new_value in delta.items():
            old_value = merged.get(key)
            if key in _UNION_SLOTS:
                if isinstance(new_value, list) and not new_value:
                    # 显式空列表 = 清空该多值槽位
                    if key in merged:
                        merged.pop(key)
                        removed.append(key)
                    continue
                current = list(old_value) if isinstance(old_value, list) else []
                union = list(current)
                for item in (new_value if isinstance(new_value, list) else [new_value]):
                    if item not in union:
                        union.append(item)
                # 防御性上限：超出保留最近 _UNION_SLOT_CAP 个，旧者过期遗忘。
                # 注意保留语义为"最近追加优先"，故截断头部（最旧）而非尾部。
                if len(union) > _UNION_SLOT_CAP:
                    union = union[-_UNION_SLOT_CAP:]
                if union != current:
                    merged[key] = union
                    if key in base:
                        updated.append({"key": key, "from": current, "to": union})
                    else:
                        added.append(key)
                continue

            if new_value == _CLEAR:
                if key in merged:
                    merged.pop(key)
                    removed.append(key)
                continue
            if key not in merged:
                merged[key] = new_value
                added.append(key)
            elif merged[key] != new_value:
                updated.append({"key": key, "from": old_value, "to": new_value})
                merged[key] = new_value

        touched = set(added) | {u["key"] for u in updated} | set(removed)
        return merged, {
            "added": sorted(added),
            "updated": updated,
            "removed": sorted(removed),
            "unchanged_count": len([k for k in merged if k not in touched]),
        }

    @staticmethod
    def _merge_constraints(
        base: list[dict[str, Any]], delta: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """同 ``kind`` 覆盖、新 kind 追加。避免同维度约束无限堆叠且互相矛盾。"""
        by_kind: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for item in [*base, *delta]:
            kind = str(item.get("kind") or f"_anon{len(order)}")
            if kind not in by_kind:
                order.append(kind)
            by_kind[kind] = item
        return [by_kind[k] for k in order][-20:]

    @staticmethod
    def _trace_refs(diff: dict[str, Any]) -> list[str]:
        """把 diff 摘要编成 output_refs（SSE 可见），便于线上按 turn 追溯合并轨迹。"""
        refs: list[str] = []
        if diff.get("result_snapshot_id"):
            refs.append(f"sir_snapshot:{diff['result_snapshot_id']}")
        if diff.get("base_snapshot_id"):
            refs.append(f"sir_base:{diff['base_snapshot_id']}")
        for key in diff.get("added", []):
            refs.append(f"+{key}")
        for item in diff.get("updated", []):
            refs.append(f"~{item['key']}")
        for key in diff.get("removed", []):
            refs.append(f"-{key}")
        return refs[:20]
