"""一键重置: 清空数据库 + Redis + Chroma + 重建表 + 自动创建默认超管。

用法(项目根目录):  python scripts/reset_all.py

执行后:
  1. DROP 所有业务表
  2. FLUSHDB 清空 Redis
  3. 清空 Chroma 所有集合数据
  4. 重建表 + 补齐缺失列
  5. 自动创建默认超管用户: huzhen / huzhen189 / 超级管理员
  6. 提示重启两个后端服务
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

    # 2.5) 清空 Chroma 所有集合(v0.9.0 新增)
    try:
        from urllib.parse import urlparse as _up
        import chromadb
        chroma_url = getattr(settings, 'chroma_url', None) or "http://chroma:8000"
        p = _up(chroma_url)
        c = chromadb.HttpClient(host=p.hostname or "localhost", port=p.port or 8000)
        colls = c.list_collections()
        for col in colls:
            try:
                c.delete_collection(col.name if hasattr(col, 'name') else str(col))
                print(f"  >> Chroma 集合已删除: {col}")
            except Exception:
                print(f"  >> Chroma 集合删除失败: {col}")
        print(f"  >> Chroma 已清空({len(colls)} 个集合)")
    except Exception as e:
        print(f"  >> Chroma 清理失败(可忽略): {e}")

    # 3) 重建表
    from app.models import Base  # noqa: E402
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("  >> 表已重建")

    # 4) 补齐缺失列 + 自动创建默认用户
    await init_db()
    print("  >> 默认用户已就绪")

    # 5) 大表 HASH 分区(幂等, 已分区则跳过)
    _PARTITIONS = {
        "messages": ("conversation_id", 16),
        "traces": ("user_id", 16),
        "trace_events": ("trace_id", 16),
        "feedbacks": ("user_id", 16),
        "usage_logs": ("user_id", 16),
        "artifacts": ("project_id", 8),
    }
    async with engine.begin() as conn:
        def _do_partitions(sync_conn):
            for tbl, (col, n) in _PARTITIONS.items():
                try:
                    sync_conn.execute(text(
                        f"ALTER TABLE {tbl} PARTITION BY HASH({col}) PARTITIONS {n}"
                    ))
                    print(f"  >> {tbl} HASH({col}) {n} 分区已应用")
                except Exception:
                    pass  # 已分区则跳过
        await conn.run_sync(_do_partitions)

    await engine.dispose()
    print("\n完成。业务服务 7101 将自动重启; AI 服务 7102 请手动重启。")


if __name__ == "__main__":
    confirm = input("⚠ 将清空全部数据并重建,确认? [y/N] ")
    if confirm.lower() != "y":
        print("已取消")
        sys.exit(0)
    asyncio.run(reset())
