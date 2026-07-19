"""一键重置: 清空数据库 + Redis + 重建表 + 自动创建默认超管。

用法(项目根目录):  python scripts/reset_all.py

执行后:
  1. DROP 所有业务表
  2. FLUSHDB 清空 Redis
  3. 重建表 + 补齐缺失列
  4. 自动创建默认超管用户: huzhen / huzhen189 / 超级管理员
  5. 提示重启两个后端服务
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "business"))

from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from sqlalchemy import text  # noqa: E402


async def reset() -> None:
    print(f"数据库: {settings.database_url[:80]}...")

    # 1) DROP 所有表
    async with engine.begin() as conn:
        def _drop(sync_conn):
            from sqlalchemy import inspect
            insp = inspect(sync_conn)
            tables = insp.get_table_names()
            for t in tables:
                sync_conn.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
            return tables
        tables = await conn.run_sync(_drop)
        if tables:
            print(f"  >> 已 DROP {len(tables)} 张表: {', '.join(tables)}")
        else:
            print("  >> 无表需清理")

    # 2) 清空 Redis
    try:
        import redis.asyncio as aioredis  # noqa: E402
    except ImportError:
        print("  >> 跳过 Redis(未安装 redis 库)")
    else:
        try:
            r = aioredis.from_url(settings.redis_url, decode_responses=True, protocol=2)
            await r.flushdb()
            print("  >> Redis 已清空")
            await r.aclose()
        except Exception as e:
            print(f"  >> Redis 清理失败: {e}")

    # 3) 重建表 + 自动创建默认用户
    await init_db()
    print("  >> 表已重建, 默认用户已就绪")

    await engine.dispose()

    print("\n完成。请重启两个后端服务(前后端无需重启)。")


if __name__ == "__main__":
    confirm = input("⚠ 将清空全部数据并重建,确认? [y/N] ")
    if confirm.lower() != "y":
        print("已取消")
        sys.exit(0)
    asyncio.run(reset())
