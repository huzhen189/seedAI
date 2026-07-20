"""一次性的：把所有文本列改为 utf8mb4 支持 emoji"""
import asyncio, sys
sys.path.insert(0, 'backend/business')
from app.db import engine
from sqlalchemy import text

async def main():
    async with engine.begin() as conn:
        sqls = [
            "ALTER TABLE messages MODIFY content TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
            "ALTER TABLE conversations MODIFY title VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
            "ALTER TABLE trace_events MODIFY payload TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
            "ALTER TABLE feedbacks MODIFY comment TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        ]
        for s in sqls:
            try:
                await conn.execute(text(s))
                print(f"  OK: {s[:60]}...")
            except Exception as e:
                print(f"  SKIP: {e}")
    await engine.dispose()
    print("utf8mb4 DONE")

asyncio.run(main())
