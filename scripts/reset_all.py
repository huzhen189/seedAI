"""SeedAI 全量重置命令行入口。

默认 dry-run。实际执行示例：
python scripts/reset_all.py --execute --allow-production --confirm "RESET seed_ai"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.reset_all import (  # noqa: E402
    CONFIRMATION_PHRASE,
    ResetExecutionError,
    ResetSafetyError,
    reset_all,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="全量重置 SeedAI 的数据库、Redis、运行时 Chroma 集合与本地产物"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="实际执行；未提供时只输出 dry-run 计划",
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="允许 ENV=production；生产执行必须显式提供",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"实际执行必须精确传入确认短语: {CONFIRMATION_PHRASE}",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=None,
        help="覆盖产物根目录；必须是名为 artifacts 的安全目录",
    )
    parser.add_argument(
        "--no-seed-system-rules",
        action="store_true",
        help="重置后不重插系统规则（默认会重插，保证刚性规则不被清空）",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    try:
        report = await reset_all(
            execute=bool(args.execute),
            allow_production=bool(args.allow_production),
            confirmation=str(args.confirm),
            artifact_root=args.artifact_root,
            reseed_system_rules=not args.no_seed_system_rules,
        )
    except ResetSafetyError as exc:
        print(json.dumps({"ok": False, "code": "reset_safety_error", "detail": str(exc)}, ensure_ascii=False))
        return 2
    except ResetExecutionError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "reset_execution_error",
                    "detail": str(exc),
                    "report": exc.report.as_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps({"ok": True, "report": report.as_dict()}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
