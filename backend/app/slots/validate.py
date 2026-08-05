"""A 方案的 CI 硬校验：防槽位冲突 / 遗漏（决策 4 = 纯 pytest）。

本模块暴露一组纯函数，返回错误字符串列表（空 = 通过）。``validate_all()`` 汇总所有不变量，
被 ``tests/test_slot_schema.py`` 调用；也可在运行时作为守卫调用。

不变量：
  1. key 全局唯一（定义层面：L0/L1/L2 之间无重复 key，避免组合时意外覆盖语义）。
  2. required 完整性：任意 compose 组合都必须含齐 L0 必填项。
  3. 层级良构：L1 必须 inherits 至少一个 L0 key；L2 extra 必须 inherits L0；extend 只能产 L3。
  4. 类型/校验合法：kind/layer 取值合法，validation 为 dict。
  5. 孤儿检测：行业→合法桶；L2 类型桶存在；inherits 引用的 key 真实存在。
"""
from __future__ import annotations

from .layers import (
    INDUSTRY_BUCKETS,
    L0_IMPLICIT,
    L0_OPTIONAL,
    L0_REQUIRED,
    L1_BUCKETS,
    L2_TYPES,
    LayerKind,
    SlotDef,
    SlotKind,
    compose,
    extend,
)


def all_defined_slots() -> list[SlotDef]:
    out: list[SlotDef] = []
    out.extend(L0_REQUIRED)
    out.extend(L0_OPTIONAL)
    out.extend(L0_IMPLICIT)
    for defs in L1_BUCKETS.values():
        out.extend(defs)
    for spec in L2_TYPES.values():
        out.extend(spec["extra"])
    return out


def validate_unique_keys() -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    for d in all_defined_slots():
        owner = f"{d.layer.value}:{d.key}"
        if d.key in seen:
            errors.append(f"槽位 key 冲突: {d.key} 同时定义在 {seen[d.key]} 与 {owner}")
        else:
            seen[d.key] = owner
    return errors


def validate_required_completeness() -> list[str]:
    errors: list[str] = []
    l0_req = {d.key for d in L0_REQUIRED}
    # 抽样若干组合（无信号 / 单行业 / 单类型 / 行业+类型 / 多类型）
    combos = [
        ("无信号", None, None),
        ("餐饮", "餐饮", None),
        ("官网", None, ["corporate"]),
        ("餐饮官网", "餐饮", ["corporate"]),
        ("电商+博客", "电商", ["commerce", "blog"]),
    ]
    for name, ind, types in combos:
        stack = compose(ind, types)
        got = {s.key for s in stack.required}
        missing = l0_req - got
        if missing:
            errors.append(f"组合[{name}] 缺失必填槽: {sorted(missing)}")
    return errors


def validate_layer_wellformed() -> list[str]:
    errors: list[str] = []
    l0_keys = {d.key for d in L0_REQUIRED} | {d.key for d in L0_OPTIONAL} | {d.key for d in L0_IMPLICIT}
    for bucket, defs in L1_BUCKETS.items():
        for d in defs:
            if d.layer != LayerKind.L1:
                errors.append(f"L1 桶 {bucket} 内槽位 {d.key} 层级非 L1")
            if not set(d.inherits) & l0_keys:
                errors.append(f"L1 槽位 {d.key} 未 inherits 任何 L0 key (inherits={d.inherits})")
    for t, spec in L2_TYPES.items():
        for d in spec["extra"]:
            if d.layer != LayerKind.L2:
                errors.append(f"L2 类型 {t} 内槽位 {d.key} 层级非 L2")
            if not set(d.inherits) & l0_keys:
                errors.append(f"L2 槽位 {d.key} 未 inherits 任何 L0 key (inherits={d.inherits})")
    return errors


def validate_types() -> list[str]:
    errors: list[str] = []
    valid_kinds = {k.value for k in SlotKind}
    valid_layers = {l.value for l in LayerKind}
    for d in all_defined_slots():
        if d.kind.value not in valid_kinds:
            errors.append(f"槽位 {d.key} kind 非法: {d.kind}")
        if d.layer.value not in valid_layers:
            errors.append(f"槽位 {d.key} layer 非法: {d.layer}")
        if not isinstance(d.validation, dict):
            errors.append(f"槽位 {d.key} validation 非 dict")
    return errors


def validate_orphans() -> list[str]:
    errors: list[str] = []
    buckets = set(L1_BUCKETS.keys())
    # 行业 → 合法桶
    for kw, (label, bucket) in INDUSTRY_BUCKETS.items():
        if bucket not in buckets:
            errors.append(f"行业 {kw}({label}) 映射到未知桶: {bucket}")
    # L2 类型桶存在
    for t, spec in L2_TYPES.items():
        if spec["bucket"] not in buckets:
            errors.append(f"L2 类型 {t} 引用未知桶: {spec['bucket']}")
    # inherits 引用真实存在
    all_keys = {d.key for d in all_defined_slots()}
    for d in all_defined_slots():
        for ref in d.inherits:
            if ref not in all_keys:
                errors.append(f"槽位 {d.key} inherits 未定义 key: {ref}")
    return errors


def validate_extend_prefix() -> list[str]:
    errors: list[str] = []
    cases = [("membership_tiers", "会员体系"), ("comment_moderation", "评论审核"), ("vip", "会员等级")]
    for raw, label in cases:
        d = extend(raw, label)
        if d.layer != LayerKind.L3:
            errors.append(f"extend({raw}) 层级非 L3")
        if not d.key.startswith("dyn_"):
            errors.append(f"extend({raw}) 未加 dyn_ 前缀: {d.key}")
        if not raw.startswith("dyn_") and d.key != f"dyn_{raw}":
            errors.append(f"extend({raw}) key 组装错误: {d.key}")
    return errors


def validate_compose_dedup() -> list[str]:
    """同名 key 后者覆盖（L2 不应意外覆盖 L0 必填；这里仅验证 compose 不丢 L0 必填）。"""
    errors: list[str] = []
    stack = compose("餐饮", ["corporate"])
    req_keys = {s.key for s in stack.required}
    if "site.name" not in req_keys or "site.theme" not in req_keys:
        errors.append("compose 去重后丢失 L0 必填项（覆盖语义异常）")
    return errors


def validate_all() -> list[str]:
    errors: list[str] = []
    errors += validate_unique_keys()
    errors += validate_required_completeness()
    errors += validate_layer_wellformed()
    errors += validate_types()
    errors += validate_orphans()
    errors += validate_extend_prefix()
    errors += validate_compose_dedup()
    return errors
