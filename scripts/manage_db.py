"""独立数据库管理脚本 — 不依赖两个后端服务运行, 直接操作统一数据库。

用法(在项目根目录执行):
  python scripts/manage_db.py list              # 列出所有表及行数
  python scripts/manage_db.py clear-chat        # 清空对话数据(会话+消息)
  python scripts/manage_db.py clear-traces      # 清空追踪/反馈/用量
  python scripts/manage_db.py clear-all         # ⚠️ 清空全部用户生成数据(保留用户/项目)
  python scripts/manage_db.py reset-quota <uid> # 重置某个用户的每日配额
  python scripts/manage_db.py nuke              # ☠️ DROP 所有表(慎用)

前置条件: 项目根 .env 已配置 DATABASE_URL 或 MYSQL_URL, 且数据库可达。
"""

import asyncio
import sys
from pathlib import Path

# 把 business app 目录加入路径, 复用 config + models + db
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "business"))

from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import Base, Conversation, Feedback, Message, Project, Trace, TraceEvent, UsageLog, User  # noqa: E402
from sqlalchemy import delete, select, text, inspect  # noqa: E402


async def _log(msg: str) -> None:
    print(f"  {msg}")


async def cmd_list() -> None:
    """列出所有表及行数"""
    async with SessionLocal() as s:
        for tname, table in Base.metadata.tables.items():
            c = (await s.execute(select(table))).fetchall()
            print(f"  {tname}: {len(c)} 行")


async def cmd_clear_chat() -> None:
    """清空对话数据(会话 + 消息)"""
    async with SessionLocal() as s:
        r1 = await s.execute(delete(Message))
        r2 = await s.execute(delete(Conversation))
        await s.commit()
        print(f"  已删除 {r1.rowcount} 条消息, {r2.rowcount} 个会话")


async def cmd_clear_traces() -> None:
    """清空追踪 / 反馈 / 用量"""
    async with SessionLocal() as s:
        r1 = await s.execute(delete(TraceEvent))
        r2 = await s.execute(delete(Trace))
        r3 = await s.execute(delete(Feedback))
        r4 = await s.execute(delete(UsageLog))
        await s.commit()
        print(
            f"  已删除 {r1.rowcount} 条追踪事件, {r2.rowcount} 条追踪, "
            f"{r3.rowcount} 条反馈, {r4.rowcount} 条用量"
        )


async def cmd_clear_all() -> None:
    """清空全部用户生成数据(保留 User / Project)"""
    print("  ⚠ 即将清空: 会话 / 消息 / 追踪 / 反馈 / 用量")
    confirm = input("  确认? [y/N] ")
    if confirm.lower() != "y":
        print("  已取消")
        return
    async with SessionLocal() as s:
        await s.execute(delete(TraceEvent))
        await s.execute(delete(Trace))
        await s.execute(delete(Feedback))
        await s.execute(delete(UsageLog))
        await s.execute(delete(Message))
        await s.execute(delete(Conversation))
        await s.commit()
        print("  已清空")


async def cmd_reset_quota(user_id: int) -> None:
    """重置某个用户的每日配额(删除 Redis key quota:daily:{user_id})"""
    try:
        import redis.asyncio as aioredis
    except ImportError:
        print("  错误: 未安装 redis 库")
        return
    r = aioredis.from_url(settings.redis_url, decode_responses=True, protocol=2)
    key = f"quota:daily:{user_id}"
    existed = await r.exists(key)
    if existed:
        await r.delete(key)
        print(f"  已重置用户 {user_id} 的每日配额(counter 已删除)")
    else:
        print(f"  用户 {user_id} 无配额记录,无需重置")
    await r.aclose()


async def cmd_nuke() -> None:
    """DROP 所有表(慎用!下次重启后端会自动重建)"""
    print("  ☠ 即将 DROP 所有业务表!数据不可恢复!")
    confirm = input("  确认? [y/N] ")
    if confirm.lower() != "y":
        print("  已取消")
        return
    async with SessionLocal() as s:
        conn = await s.connection()
        insp = inspect(conn.sync_connection)
        tables = insp.get_table_names()
        for t in tables:
            await conn.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
        await conn.commit()
        print(f"  已 DROP {len(tables)} 张表")


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    print(f"数据库: {settings.database_url[:80]}...")
    print(f"Redis: {settings.redis_url}")
    # 自动补齐缺失列(与业务服务 init_db 一致, 幂等)
    await init_db()

    if cmd == "list":
        await cmd_list()
    elif cmd == "clear-chat":
        await cmd_clear_chat()
    elif cmd == "clear-traces":
        await cmd_clear_traces()
    elif cmd == "clear-all":
        await cmd_clear_all()
    elif cmd == "reset-quota":
        uid = int(sys.argv[2]) if len(sys.argv) > 2 else None
        if uid is None:
            print("  用法: python scripts/manage_db.py reset-quota <user_id>")
            return
        await cmd_reset_quota(uid)
    elif cmd == "nuke":
        await cmd_nuke()
    else:
        print(f"  未知命令: {cmd}")
        print(__doc__)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
