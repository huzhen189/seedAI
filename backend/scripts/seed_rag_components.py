"""冷启动/重置后重建向量库种子数据。

修复此前 scripts/seed_rag_components.py 缺失导致 components / error_patterns 集合长期为 0 的问题。
(运行时 ensure_collections 也会在集合为空时自动播种, 本脚本用于手动全量重建 / 调试。)

用法:
    cd backend
    python scripts/seed_rag_components.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许以脚本方式直接运行(backend 根加入 sys.path)
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.agent.knowledge.chroma import (  # noqa: E402
    _COMPONENT_SEEDS,
    seed_components,
    seed_error_patterns,
)
from app.agent.intent.vector_store import ensure_intent_index  # noqa: E402


def main() -> None:
    print("[seed] 写入组件库 components ...")
    n_comp = seed_components(_COMPONENT_SEEDS)
    print(f"        components 写入 {n_comp} 条")

    print("[seed] 写入错误模式 error_patterns ...")
    n_err = seed_error_patterns()
    print(f"        error_patterns 写入 {n_err} 条")

    print("[seed] 重建意图索引 intents (82 句) ...")
    ensure_intent_index()
    print("        intents 索引完成")

    print("[seed] 全部种子数据就绪 ✅")


if __name__ == "__main__":
    main()
