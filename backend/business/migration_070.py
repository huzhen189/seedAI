"""v0.7.0 迁移: projects 表加 system_prompt 列。
(优先 ALTER TABLE, 列已存在则跳过)
"""
import asyncio
import sys
sys.path.insert(0, ".")
from app.config import settings
from app.db import engine

async def main():
    async with engine.begin() as conn:
        try:
            await conn.run_sync(lambda c: c.execute(
                "ALTER TABLE projects ADD COLUMN system_prompt TEXT NULL AFTER preview_url"
            ))
            print("✅ system_prompt 列已添加")
        except Exception:
            print("⚠ system_prompt 列可能已存在, 跳过")
    await engine.dispose()

asyncio.run(main())
