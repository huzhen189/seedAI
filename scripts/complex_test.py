"""复杂对话回归测试（已废弃）。

v2.0.0 起，业务服务与 AI 核心合并为单进程，旧的 `POST http://127.0.0.1:7102/generate`
端点已不存在（对话统一走 `GET {BASE}/api/chat` SSE 网关）。

本文件不再维护——等价且更完整的回归能力已合并进 `run_tests.py`
（200 条用例，含 30 条 `quick` 模式复杂/术语/复杂条件/混合场景），用法：

    python scripts/run_tests.py --quick    # 快速 30 条
    python scripts/run_tests.py            # 完整 200 条
    python scripts/run_tests.py --csv      # 导出 CSV 报告

如需单独跑"复杂语句"子集，直接复用 `run_tests.py` 的 TEST_CASES 即可。
"""
import sys

if __name__ == "__main__":
    print("⚠️  complex_test.py 已废弃：旧 AI 服务 7102/generate 端点不存在。")
    print("    请改用: python scripts/run_tests.py [--quick] [--csv]")
    sys.exit(2)
