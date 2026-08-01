"""SeedAI 恢复脚本（§15.4 灾备；M11a 影子环境恢复）。

用法：
  python scripts/restore.py backups/20260801T...     # 恢复到原库
  python scripts/restore.py <dir> --target-db mysql+pymysql://.../seedai_shadow  # 影子恢复
  python scripts/restore.py <dir> --target-artifact-dir /data/seedai/artifacts
  python scripts/restore.py <dir> --dry-run          # 仅打印计划

注意：真实恢复会清空并重建目标库；务必先确认 manifest 与影响清单（见 M11a）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from backup import DUMP_FILENAME, MANIFEST_FILENAME, ARTIFACT_ARCHIVE, parse_mysql_url  # noqa: E402


def restore_mysql(sql_path: Path, url: str, dry_run: bool) -> int:
    import pymysql

    cfg = parse_mysql_url(url)
    print(f"[restore] MySQL -> {cfg['host']}:{cfg['port']}/{cfg['db']} (dry_run={dry_run})")
    if dry_run:
        return 0
    conn = pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], database=cfg["db"], charset="utf8mb4",
    )
    statements = 0
    try:
        with sql_path.open("r", encoding="utf-8") as f:
            buf = ""
            for line in f:
                if line.startswith("--"):
                    continue
                buf += line
                if line.strip().endswith(";"):
                    stmt = buf.strip()
                    if stmt:
                        with conn.cursor() as cur:
                            cur.execute(stmt)
                        statements += 1
                    buf = ""
        conn.commit()
    finally:
        conn.close()
    print(f"[restore] 执行 {statements} 条 SQL")
    return statements


def extract_artifacts(archive: Path, dst: Path, dry_run: bool) -> int:
    print(f"[restore] 产物 -> {dst} (dry_run={dry_run})")
    if dry_run:
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            tar.extract(m, dst)
            n += 1
    print(f"[restore] 解包 {n} 个文件")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="SeedAI 恢复（MySQL + 产物）")
    ap.add_argument("backup_dir", type=Path, help="含 manifest.json 的备份目录")
    ap.add_argument("--target-db", default="", help="覆盖恢复目标库（影子恢复用）")
    ap.add_argument("--target-artifact-dir", default="", help="覆盖产物恢复目录")
    ap.add_argument("--dry-run", action="store_true", help="仅打印计划")
    args = ap.parse_args()

    manifest_path = args.backup_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        print(f"[error] 找不到 manifest: {manifest_path}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    target_db = args.target_db or settings.database_url
    target_artifact = Path(args.target_artifact_dir) if args.target_artifact_dir else Path(settings.artifact_dir)

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "target_db": _anon(target_db),
            "target_artifact_dir": str(target_artifact),
            "mysql_dump": manifest["mysql"]["dump"],
            "tables": manifest["mysql"]["tables"],
            "artifact_archive": manifest["artifacts"]["archive"],
            "artifact_file_count": manifest["artifacts"]["file_count"],
        }, ensure_ascii=False, indent=2))
        return 0

    dump_path = args.backup_dir / manifest["mysql"]["dump"]
    restore_mysql(dump_path, target_db, args.dry_run)

    archive = args.backup_dir / manifest["artifacts"]["archive"] if manifest["artifacts"]["archive"] else None
    if archive and archive.exists():
        extract_artifacts(archive, target_artifact, args.dry_run)
    print("[restore] 完成")
    return 0


def _anon(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.hostname}:{p.port}/{p.path.lstrip('/')}"


if __name__ == "__main__":
    raise SystemExit(main())
