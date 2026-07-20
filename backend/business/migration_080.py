"""v0.8.0 迁移: projects 表加 status + requirement_doc 列。"""
import asyncio, sys
sys.path.insert(0, ".")
from app.config import settings
from app.db import engine

async def main():
    async with engine.begin() as conn:
        for col, sql in [
            ("status", "ALTER TABLE projects ADD COLUMN status VARCHAR(20) DEFAULT 'draft'"),
            ("requirement_doc", "ALTER TABLE projects ADD COLUMN requirement_doc TEXT NULL"),
        ]:
            try:
                await conn.run_sync(lambda c: c.execute(sql))
                print(f"✅ {col} 已添加")
            except Exception:
                print(f"⚠ {col} 可能已存在, 跳过")
    await engine.dispose()

asyncio.run(main())
