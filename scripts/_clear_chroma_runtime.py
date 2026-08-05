"""只清 Chroma 运行时集合(保留知识底座)。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.db.reset_all import clear_chroma  # noqa: E402


async def run() -> int:
    deleted, preserved = await clear_chroma(settings.chroma_url)
    print("deleted (recreated empty):", deleted)
    print("preserved:", preserved)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
