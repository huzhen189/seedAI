"""A 方案 CI 硬校验套件（纯 pytest，无网络）。

运行：``pytest backend/tests/test_slot_schema.py``
所有不变量失败即阻断合入——把「槽位冲突 / 遗漏 / 非良构」锁死在 CI。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让 backend 可被 import（仓库根在 backend/ 上一级）。
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.slots import (  # noqa: E402
    DYNAMIC_SLOT_TRIGGERS,
    INDUSTRY_BUCKETS,
    L0_REQUIRED,
    L1_BUCKETS,
    L2_TYPES,
    SlotStack,
    compose,
    detect_dynamic_slots,
    detect_industry,
    extend,
)
from app.slots.persist import _dyn_doc_id  # noqa: E402
from app.slots.validate import validate_all  # noqa: E402


def test_no_invariants_violated():
    errors = validate_all()
    assert errors == [], "槽位不变量校验失败:\n" + "\n".join(f"  - {e}" for e in errors)


def test_l0_required_exactly_four():
    keys = {d.key for d in L0_REQUIRED}
    # 必填 key 已对齐到 site.* SIR 命名空间（与 build_spec 消费的 SIR 键一致），
    # 否则"信息收集"硬闸门永远失效（旧裸 key 与 site.* 对不上）。
    assert keys == {"site.name", "site.theme", "site.brief", "site.deploy_target"}, keys


def test_l0_excludes_contact_as_universal():
    all_l0 = {d.key for d in L0_REQUIRED}
    all_l0 |= {d.key for d in __import__("app.slots", fromlist=["L0_OPTIONAL"]).L0_OPTIONAL}
    all_l0 |= {d.key for d in __import__("app.slots", fromlist=["L0_IMPLICIT"]).L0_IMPLICIT}
    # 联系电话/邮箱/语言不得作为通用槽
    assert "contact_phone" not in all_l0
    assert "contact_email" not in all_l0
    assert "language" not in all_l0


def test_only_three_industry_buckets():
    assert set(L1_BUCKETS.keys()) == {"content_showcase", "ecommerce_service", "interactive_platform"}


def test_fifty_industries_covered():
    assert len(INDUSTRY_BUCKETS) == 50, len(INDUSTRY_BUCKETS)


def test_detect_industry():
    assert detect_industry("帮我做个餐饮官网") == "餐饮"
    assert detect_industry("想建一个摄影作品集") == "摄影"
    assert detect_industry("随便聊聊今天天气") is None


def test_compose_restaurant_official_site_union_buckets():
    stack = compose("餐饮", ["corporate"])
    keys = {s.key for s in stack.slots}
    # L0 必填齐全
    assert {"site.name", "site.theme", "site.brief", "site.deploy_target"} <= keys
    # 行业桶(ecommerce_service) 与 类型桶(content_showcase) 的可选槽都应出现（并集）
    assert "payment_methods" in keys          # 来自 ecommerce_service
    assert "showcase_sections" in keys        # 来自 content_showcase
    # 必填仍只有 L0
    assert {s.key for s in stack.required} == {"site.name", "site.theme", "site.brief", "site.deploy_target"}


def test_compose_multi_type_union():
    stack = compose("电商", ["commerce", "blog"])
    keys = {s.key for s in stack.slots}
    assert "product_categories" in keys   # commerce extra
    assert "blog_categories" in keys       # blog extra


def test_compose_no_signal_defaults_to_content_showcase():
    stack = compose(None, None)
    keys = {s.key for s in stack.slots}
    assert "showcase_sections" in keys
    assert "payment_methods" not in keys


def test_extend_forces_dyn_prefix():
    d = extend("membership_tiers", "会员体系")
    assert d.key == "dyn_membership_tiers"
    assert d.layer.value == "L3"


def test_guidance_reports_missing_required():
    stack = compose("餐饮", ["corporate"])
    g = stack.guidance(filled=set())  # 啥都没填
    assert set(g["missing_required"]) == {"网站名称", "样式风格", "内容主题 / 主要目的", "部署目标"}
    # 已填 site.name 后不再提示
    g2 = stack.guidance(filled={"site.name"})
    assert "网站名称" not in g2["missing_required"]


def test_dyn_doc_id_deterministic_and_update_semantics():
    # 同 (user_id, slot_key) → 同 id → upsert 即更新（满足“之前有的话更新”）
    assert _dyn_doc_id(2, "dyn_membership_tiers") == _dyn_doc_id(2, "dyn_membership_tiers")
    assert _dyn_doc_id(2, "dyn_membership_tiers") != _dyn_doc_id(3, "dyn_membership_tiers")
    assert _dyn_doc_id(2, "dyn_membership_tiers").startswith("dynpref:2:")


def test_detect_dynamic_slots():
    # 命中会员 → 注入 dyn_membership_tiers
    out = detect_dynamic_slots("帮我做一个会员制电商网站")
    keys = {s.key for s in out}
    assert "dyn_membership_tiers" in keys
    # 多业务概念 → 并集（会员 + 积分 + 预约）
    out = detect_dynamic_slots("做一个带积分和预约的会员平台")
    keys = {s.key for s in out}
    assert {"dyn_membership_tiers", "dyn_points_system", "dyn_booking_slots"} <= keys
    # 「优惠」与「优惠券」映射到同一 raw_key → 去重（不重复注入）
    out = detect_dynamic_slots("优惠券优惠活动")
    assert sum(1 for s in out if s.key == "dyn_coupon_rules") == 1
    # 无业务信号 → 空
    assert detect_dynamic_slots("随便聊聊今天天气") == []
    # 全部为 L3 且强制 dyn_ 前缀
    for s in out:
        assert s.layer.value == "L3"
        assert s.key.startswith("dyn_")


def test_compose_includes_dynamic_slots():
    dyn = detect_dynamic_slots("会员制网站")
    stack = compose("电商", ["commerce"], dynamic=dyn)
    keys = {s.key for s in stack.slots}
    assert "dyn_membership_tiers" in keys
    # 动态槽进入栈后，仍需保留 L0 必填（不被覆盖）
    assert {"site.name", "site.theme", "site.brief", "site.deploy_target"} <= keys


def test_dynamic_triggers_mapped():
    # 至少覆盖一批常见业务概念，且每项是三元组
    assert len(DYNAMIC_SLOT_TRIGGERS) >= 10
    for kw, (raw, label, hint) in DYNAMIC_SLOT_TRIGGERS.items():
        assert raw and label and hint
        assert not raw.startswith("dyn_")  # extend() 负责加前缀
