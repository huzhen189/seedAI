"""选项选择解析:把用户回的 "B" / "2" / "选B" / "用B" / "第二个" 解析为候选索引。

背景
----
- 工具路由选项已改为"系统自决 + 非阻塞 alternatives 提示"(pipeline._aggregate Step5),
  不再阻塞用户。但用户仍可能想切换,或面对 requirement_agent 的"方案多选"(业务决策,保留)。
- 当系统出过选项/候选时,把候选列表存入 pending_options(按 conversation_id);下一轮用户输入
  若是选择 token,这里负责把它映射到候选并短路路由,避免被当成新 query 重新分类。

设计要点
------
- 纯函数 + 轻量状态,不调 LLM,零延迟。
- 选择 token 正则覆盖:单字母 A-H、数字 1-9、中文数字(一二三…八九)、"选X"/"用X"/"切换X"。
- pending_options 存储:优先 Redis(`gen:opt:<conv>`,带 TTL),无 redis 则进程内 dict 兜底。
"""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger("ai_service.intent.selection")

# 中文数字 → 索引(1-based 选项 → 0-based)
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 选择 token 模式(匹配整段输入,允许前后空格)
# 组1: 单字母 A-H(忽略大小写)  组2: 数字 1-9  组3: 中文数字  组4: 选/用/切换 + 字母或中文数字
_SELECT_RE = re.compile(
    r"^\s*(?:"
    r"([A-Ha-h])\s*"                                  # 单字母
    r"|([1-9])\s*"                                    # 数字
    r"|([一二两三四五六七八九十])\s*"                  # 中文数字
    r"|(?:选|用|切换|改成|改为)\s*([A-Ha-h1-9一二两三四五六七八九十])\s*"  # 选X/用X
    r"|第\s*([一二两三四五六七八九十])\s*个"            # 第二个
    r")\s*$"
)


def parse_selection(text: str) -> int | None:
    """把选择 token 解析为 0-based 索引;不是选择则返回 None。

    支持: "B" "2" "二" "选B" "用2" "切换三" "第二个" 等。
    """
    if not text:
        return None
    m = _SELECT_RE.match(text.strip())
    if not m:
        return None
    letter, digit, cn, pick, nth = m.groups()
    if letter:
        return ord(letter.upper()) - ord("A")  # A→0, B→1, ...
    if digit:
        return int(digit) - 1
    if cn:
        return _CN_NUM[cn] - 1
    if pick:
        if pick.isalpha():
            return ord(pick.upper()) - ord("A")
        if pick.isdigit():
            return int(pick) - 1
        return _CN_NUM[pick] - 1
    if nth:
        return _CN_NUM[nth] - 1
    return None


# ── pending_options 存储(按 conversation_id) ──
# 结构: {conv_id: {"skills": [...], "expire": ts}}
_PENDING: dict[int, dict] = {}
_PENDING_TTL = 1800  # 30 分钟


def set_pending_options(conversation_id: int, skills: list[str]) -> None:
    if not conversation_id or not skills:
        return
    _PENDING[conversation_id] = {"skills": list(skills), "expire": time.time() + _PENDING_TTL}
    logger.info("[选择] 记录待选项 conv=%s skills=%s", conversation_id, skills)


def get_pending_options(conversation_id: int) -> list[str] | None:
    if not conversation_id:
        return None
    entry = _PENDING.get(conversation_id)
    if not entry:
        return None
    if time.time() > entry["expire"]:
        _PENDING.pop(conversation_id, None)
        return None
    return entry["skills"]


def clear_pending_options(conversation_id: int) -> None:
    _PENDING.pop(conversation_id, None)


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return (m.get("content", "") or "").strip()
    return ""


def is_selection(text: str) -> bool:
    """快速判断一段输入是否为"选项选择"语义(供前端/worker 预判)。"""
    return parse_selection(text) is not None


_OVERRIDE_RE = re.compile(r"(?:用|切换|改成|改为|选)\s*([a-zA-Z_]+)")


def match_override_name(text: str, known_skills: set[str] | None) -> str | None:
    """检测"用 explain" / "切换成 write_code" 等显式指定 skill 的覆盖式输入。

    返回匹配的已注册 skill 名;否则 None。
    """
    if not text or not known_skills:
        return None
    m = _OVERRIDE_RE.search(text)
    if not m:
        return None
    name = m.group(1).lower()
    for s in known_skills:
        if s.lower() == name:
            return s
    return None


def resolve_selection(messages: list[dict], conversation_id: int | None,
                      skill_exists, known_skills: set[str] | None = None
                      ) -> tuple[str, list[str]] | None:
    """若用户在回复一个待选项/显式指定 skill,返回 (被选skill, 全部候选);否则 None。

    解析顺序:
      1. 显式覆盖"用 X" / "切换成 X"(X 为已知 skill) → 直接路由 X;
      2. 选择 token "B"/"2"/"选B" + 待选项状态 → 映射到候选 skill。

    Args:
        skill_exists: callable(name)->bool,校验候选 skill 是否真实注册。
        known_skills: 已注册 skill 名集合(用于覆盖式匹配)。
    """
    text = last_user_text(messages)
    # 1) 显式覆盖: 用 X / 切换成 X
    if known_skills:
        ov = match_override_name(text, known_skills)
        if ov:
            logger.info("[选择] 显式覆盖 → skill=%s (输入=%.40s)", ov, text)
            return ov, [ov]
    # 2) 待选项 + 选择 token
    if not conversation_id:
        return None
    pending = get_pending_options(conversation_id)
    if not pending:
        return None
    idx = parse_selection(text)
    if idx is None:
        return None
    if idx < 0 or idx >= len(pending):
        logger.info("[选择] 索引越界 idx=%d 候选数=%d → 忽略", idx, len(pending))
        return None
    # idx 0 永远是已选 top1(完整列表首元素) → 重新确认当前选择, 视为无操作
    if idx == 0:
        logger.info("[选择] idx=0 即当前已选 %s → 无操作", pending[0])
        return None
    chosen = pending[idx]
    if not skill_exists(chosen):
        logger.warning("[选择] 候选 skill '%s' 未注册 → 忽略", chosen)
        return None
    logger.info("[选择] 命中 idx=%d → skill=%s (候选=%s)", idx, chosen, pending)
    return chosen, pending
