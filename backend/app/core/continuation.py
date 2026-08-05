"""跨轮承接（Continuation）—— 把「上下文承接关系」从模型推理里拿出来，变成可计算、可验证、可回滚的一等数据结构。

设计取舍（用户拍板）：
  - 抉择 1 = **C（回指链同主题最近前情，最多 5 条，取置信度最高那条）**：不引入 LLM，
    用确定性信号解析当前句与前情的承接关系，避免「无脑把历史灌进意图分类」导致的串味。
  - 抉择 2 = **A（target_slots 写死 ["site.brief"]）**：承接只折进 brief，长尾风格承接
    走 user_context 用户画像兜底。

解析信号（纯字符串，无 NER / 无语义模型）：
  1. **词面 n-gram 重叠**：当前句与每条前情的 CJK 2~4 字滑窗交集 —— 主题相关的朴素代理。
  2. **选择题承接（关键）**：前情含「选择题标记」(好还是/对比/vs/选/推荐/二选一…) 且
     当前句含「选择回指」(哪个/选哪个/用哪个…) → 强信号：当前句在引用前一轮给出的选项。
     这正是「买雨伞好还是买雨衣好」→「用哪个」的承接，可在多前情中精准命中，
     不依赖字面重叠（当前句根本不含「雨伞/雨衣」）。
  3. **回指词兜底**：当前句含回指词(哪个/这个/刚才/之前…) 但全无重叠/choice 信号 →
     回落链接最近一条前情（首版够用，覆盖单前情常见场景）。

评分 = n-gram重叠*2 + (choice承接? 3 : 0) + 近因加成 (cap-idx)*0.3；取最高分，>0 即 references。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # 仅类型期引用，运行期不导入 contracts（避免 contracts ←→ continuation 循环）
    from app.core.contracts import ActiveTask

# 回指词：当前句在引用前文的某样东西（但不一定是选项）。
_ANAPHORA = (
    "哪个", "哪一个", "这个", "那个", "继续", "刚才", "之前", "前面",
    "承接", "根据上述", "之前说的", "还是",
)
# 前情侧的「选择题标记」：该前情曾给出选项 / 做对比 / 让选。
_CHOICE_MARKERS = (
    "还是", "对比", "vs", " versus ", "选", "推荐", "好还是", "二选一", "权衡", "两者之间",
)
# 当前句侧的「选择回指」：在问「哪个」选项。
_CHOICE_REF = ("哪个", "哪一个", "选哪个", "用哪个", "推荐哪个", "挑哪个", "两者之间", "二选一")


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _ngrams(text: str, n_min: int = 2, n_max: int = 4) -> set[str]:
    """CJK 2~4 字滑窗集合（与 s1_recall._pref_keywords 同思路，去停用边界）。"""
    cjk = [ch for ch in text if _is_cjk(ch)]
    s = "".join(cjk)
    out: set[str] = set()
    if len(s) < 2:
        return out
    for n in range(n_min, n_max + 1):
        for i in range(len(s) - n + 1):
            out.add(s[i : i + n])
    return out


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)


class Continuation(BaseModel):
    """跨轮承接边（一等数据结构）。

    - ``relation``: independent | references
    - ``source_turn_id``: 被承接的前情 turn_id（来自 gist）
    - ``summary``: 承接内容摘要（直接取自前情 gist，无需 LLM 重写）
    - ``target_slots``: 承接应折进的槽位（抉择 2=A，固定 ["site.brief"]）
    - ``overlap_entities``: 当前句与前情共享的 n-gram（可验证用）
    - ``confidence``: 确定性评分换算的置信度（0~1），可测试

    v2 起改为 pydantic ``BaseModel``（原 ``@dataclass``，构造签名不变）：
    ``SirState.continuation`` 需要随 SIR 快照一起 JSON 序列化持久化，
    dataclass 走不了 pydantic 的 ``model_dump``/``model_validate`` 往返。
    **不继承 ``ContractModel``** 是刻意的——那会引入 contracts ←→ continuation 循环导入；
    本类只依赖 pydantic，由 contracts 单向导入。
    """

    model_config = ConfigDict(extra="forbid")

    relation: str = "independent"
    source_turn_id: str | None = None
    summary: str | None = None
    target_slots: list[str] = Field(default_factory=list)
    overlap_entities: list[str] = Field(default_factory=list)
    confidence: float = 0.0


def resolve_continuation(
    message: str, gist: list[dict], cap: int = 5
) -> Continuation:
    """确定性跨轮承接解析 —— 无 LLM、可计算、可验证、可回滚。

    Args:
        message: 当前轮 clean_message。
        gist: S1 产出的前情列表，**index 0 = 最近一条前情**（近→远），最多 cap 条。
              每项含 ``turn_id / role / summary / content``。
        cap: 最多考察的前情条数（用户定 5）。
    Returns:
        Continuation；无任何承接则为 independent（全默认字段）。
    """
    if not gist or not message:
        return Continuation()
    items = gist[:cap]
    cur_ng = _ngrams(message)
    has_anaphora = _has_any(message, _ANAPHORA)
    has_choice_ref = _has_any(message, _CHOICE_REF)

    best: Continuation | None = None
    best_score = 0.0
    for idx, g in enumerate(items):
        text = (g.get("summary") or g.get("content") or "")
        if not text:
            continue
        overlap = cur_ng & _ngrams(text)
        choice_link = has_choice_ref and _has_any(text, _CHOICE_MARKERS)
        if not overlap and not choice_link:
            continue
        # 近因加成：index 越小(越近)分越高。
        score = len(overlap) * 2.0 + (3.0 if choice_link else 0.0) + (cap - idx) * 0.3
        if score > best_score:
            best_score = score
            best = Continuation(
                relation="references",
                source_turn_id=g.get("turn_id"),
                summary=(g.get("summary") or text)[:200],
                target_slots=["site.brief"],
                overlap_entities=sorted(overlap),
                confidence=round(min(0.99, 0.6 + score / 20.0), 2),
            )
    if best is not None:
        return best

    # 纯回指、无字面/choice 信号 → 回落链接最近一条前情（首版够用，blast radius 极小）。
    if has_anaphora:
        g0 = items[0]
        return Continuation(
            relation="references",
            source_turn_id=g0.get("turn_id"),
            summary=(g0.get("summary") or g0.get("content") or "")[:200],
            target_slots=["site.brief"],
            confidence=0.7,
        )
    return Continuation()


def already_seeded(cont: Continuation | None, task: "ActiveTask | Any | None") -> bool:
    """该 task 是否**已经吸收过承接**（幂等闸门，供 ``transition.plan_round`` 使用）。

    语义比"同源判等"更强：**一个 task 生命周期内只吸收一次承接**。
    原因——下一轮 ``resolve_continuation`` 很可能改指向"上一轮的追问消息"
    （追问文本与当前回答天然高重叠），若按 ``source_turn_id`` 相等判定，
    异源承接会被反复播种，``task.goal`` 每轮滚雪球膨胀（正是 v1 往 ``site.brief``
    追加 ``（承接：…）`` 的老毛病）。首次承接即锁定 lineage，后续只更新槽位不再改 goal。
    """
    if cont is None or task is None:
        return False
    return getattr(task, "continuation_source", None) is not None


__all__ = ["Continuation", "already_seeded", "resolve_continuation"]
