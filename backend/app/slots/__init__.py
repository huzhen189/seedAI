"""分层槽位体系（L0/L1/L2/L3）公共入口。详见 ``layers.py``。"""
from __future__ import annotations

from .layers import (
    DYNAMIC_SLOT_TRIGGERS,
    INDUSTRY_BUCKETS,
    L0_IMPLICIT,
    L0_OPTIONAL,
    L0_REQUIRED,
    L1_BUCKETS,
    L2_TYPES,
    LayerKind,
    SlotDef,
    SlotKind,
    SlotStack,
    compose,
    detect_dynamic_slots,
    detect_industry,
    extend,
)
from .persist import _dyn_doc_id, persist_dynamic_slot

__all__ = [
    "DYNAMIC_SLOT_TRIGGERS",
    "INDUSTRY_BUCKETS",
    "L0_IMPLICIT",
    "L0_OPTIONAL",
    "L0_REQUIRED",
    "L1_BUCKETS",
    "L2_TYPES",
    "LayerKind",
    "SlotDef",
    "SlotKind",
    "SlotStack",
    "compose",
    "detect_dynamic_slots",
    "detect_industry",
    "extend",
    "_dyn_doc_id",
    "persist_dynamic_slot",
]
