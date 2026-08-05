"""迁移: 给 approvals 表增加 args JSON 列(用于承载增量发布的 publish_files 等结构化参数)。

幂等: 列已存在则跳过。兼容 MySQL(MEDTADATA) 与本地 sqlite。

用法(项目根目录):
  python scripts/migrate_approval_args.py            # 自动读取 .env 的 MYSQL_URL
  python scripts/migrate_approval_args.py --url "mysql+pymysql://..."  # 指定库
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def _load_env_url() -> str | None:
    """从项目根 .env 读取 MYSQL_URL / DATABASE_URL(不依赖 python-dotenv)。"""
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key in ("MYSQL_URL", "DATABASE_URL", "MYSQL_DATABASE_URL"):
            return val
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None, help="数据库连接串; 缺省从 .env 读取")
    args = ap.parse_args()
    url = args.url or _load_env_url()
    if not url:
        print("未找到数据库连接(.env 无 MYSQL_URL/DATABASE_URL)。请通过 --url 指定。")
        return 2

    parsed = urlparse(url)
    scheme = parsed.scheme.split("+")[0].lower()

    if scheme in ("mysql", "mariadb"):
        import pymysql

        conn = pymysql.connect(
            host=parsed.hostname,
            port=parsed.port or 3306,
            user=parsed.username,
            password=parsed.password or "",
            database=parsed.path.lstrip("/"),
            charset="utf8mb4",
        )
        cur = conn.cursor()
        cur.execute("SHOW COLUMNS FROM approvals LIKE 'args'")
        exists = cur.fetchone() is not None
        if exists:
            print("approvals.args 已存在, 跳过 (MySQL)。")
        else:
            cur.execute("ALTER TABLE approvals ADD COLUMN args JSON NOT NULL DEFAULT (JSON_OBJECT())")
            conn.commit()
            print("已为 approvals 增加 args JSON 列 (MySQL)。")
        cur.close()
        conn.close()
        return 0

    if scheme == "sqlite":
        import sqlite3

        db_path = parsed.path
        if not Path(db_path).exists():
            print(f"sqlite 文件不存在: {db_path}, 跳过(首次启动 create_all 会自动建列)。")
            return 0
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(approvals)")
        cols = {r[1] for r in cur.fetchall()}
        if "args" in cols:
            print("approvals.args 已存在, 跳过 (sqlite)。")
        else:
            cur.execute("ALTER TABLE approvals ADD COLUMN args TEXT NOT NULL DEFAULT '{}'")
            conn.commit()
            print("已为 approvals 增加 args TEXT 列 (sqlite)。")
        conn.close()
        return 0

    print(f"不支持的数据库 scheme: {scheme}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
