"""向量库 3.2/3.3/3.4 接线验证(确定性, 不依赖完整建站 LLM)。

验证点:
  3.2 建站 RAG: build_rag_context 命中 components(已 seed) + memory。
  3.4 个性化/复用: 模拟 _distill_memories 写入 project_memory + user_preferences +
                   error_patterns(已 seed), 验证 build_rag_context 能检索回并注入。
  3.3 上下文连贯: find_relevant_message_contents 返回相关历史消息正文(阈值过滤)。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.agent.knowledge import chroma as CH  # noqa: E402

TEST_UID = 99001
TEST_PID = 99001
QUERY = "帮我做一个深色科技风的摄影作品集网站，要有玻璃拟态卡片和响应式导航"


def banner(t: str) -> None:
    print("\n" + "=" * 64)
    print(t)
    print("=" * 64)


def main() -> None:
    banner("[3.2 + 3.4] build_rag_context 全集合召回")
    # 模拟蒸馏写侧(与 queue._distill_memories 调用一致)
    CH.upsert_project_memory(TEST_PID, TEST_UID, "decision",
                             "项目采用深蓝紫科技风，首页含 Hero 与作品网格", importance=4)
    CH.upsert_user_preference(TEST_UID, "style",
                              "用户偏好深色模式与玻璃拟态风格，留白克制", importance=4)
    print(f"[写] project_memory / user_preferences 已写入 (pid={TEST_PID}, uid={TEST_UID})")

    ctx = CH.build_rag_context(QUERY, project_id=TEST_PID, user_id=TEST_UID)
    print(f"\n[RAG] 注入上下文总长 = {len(ctx)} 字\n")
    for sec in ("【组件库参考】", "【历史记忆】", "【项目记忆】", "【用户偏好】", "【错误模式经验】"):
        hit = sec in ctx
        print(f"  {'✅' if hit else '❌'} {sec}  {'命中' if hit else '未命中'}")
    assert "【组件库参考】" in ctx, "3.2 components 未命中(种子应已写入)"
    assert "【项目记忆】" in ctx, "3.4 project_memory 检索失败"
    assert "【用户偏好】" in ctx, "3.4 user_preferences 检索失败"
    assert "【错误模式经验】" in ctx, "3.4 error_patterns 检索失败"
    print("\n  → 3.2/3.4 读侧闭环 ✅ (组件库/项目记忆/用户偏好/错误经验均注入 Planner)")

    banner("[3.3] find_relevant_message_contents 多轮上下文")
    # 模拟 Worker [2/6] 索引历史消息
    CH.index_message(900001, TEST_PID * 100 + 1, "user", "我想做一个摄影作品集网站，深色的")
    CH.index_message(900002, TEST_PID * 100 + 1, "assistant", "好的，我来帮你规划深色摄影作品集")
    CH.index_message(900003, TEST_PID * 100 + 1, "user", "首页要一个大的 Hero 和作品网格")
    hist = CH.find_relevant_message_contents("摄影作品集网站首页怎么设计", TEST_PID * 100 + 1, top_k=6)
    print(f"\n[3.3] 相关历史召回 {len(hist)} 条:")
    for h in hist:
        print(f"   · 相似度={h['score']:.3f}  {h['content'][:50]}")
    assert hist, "3.3 未召回任何相关历史消息"
    print("\n  → 3.3 上下文召回 ✅ (返回相关历史正文, Worker 可注入 system 消息)")

    banner("结论")
    print("3.2 ✅ 组件库 RAG 生效(种子已写入)")
    print("3.3 ✅ 多轮上下文召回生效(返回正文可注入)")
    print("3.4 ✅ 个性化闭环生效(写侧蒸馏 + 读侧 RAG 通)")


if __name__ == "__main__":
    main()
