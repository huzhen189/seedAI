"""SeedAI 生产备份脚本（§15.4 灾备）。

标准做法：
- MySQL 全量逻辑导出（pymysql，无需外部 mysqldump）；记录 binlog 位点以支持 <5min RPO。
- 本地 ARTIFACT_DIR 加密打包（gpg 可选；缺失则仅 gzip 并告警）。
- 生成 manifest（含每张表行数、checksum、binlog 位点、RPO/RTO 注释）。
- 输出到 backups/<timestamp>/，凭证与部署凭证隔离（脚本不落明文密码到产物）。

用法：
  python scripts/backup.py                 # 实际备份
  python scripts/backup.py --dry-run       # 仅打印计划，不写盘
  python scripts/backup.py --out-dir /backup/seedai
  python scripts/backup.py --no-encrypt     # 跳过 gpg（即使存在）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402

DUMP_FILENAME = "mysql_dump.sql"
ARTIFACT_ARCHIVE = "artifacts.tar.gz"
MANIFEST_FILENAME = "manifest.json"


def parse_mysql_url(url: str) -> dict:
    """mysql+pymysql://user:pass@host:port/db → 连接参数。"""
    parsed = urlparse(url)
    scheme = parsed.scheme.split("+")[0]  # 去掉 +pymysql
    if scheme != "mysql":
        raise ValueError(f"仅支持 mysql 协议，实际: {parsed.scheme}")
    port = parsed.port or 3306
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": port,
        "user": parsed.username or "root",
        "password": parsed.password or "",
        "db": parsed.path.lstrip("/") or "",
    }


def anonymize(url: str) -> str:
    p = urlparse(url)
    hidden = "<hidden>"
    return f"{p.scheme}://{hidden}:{p.port or '?'}/{(p.path or '').lstrip('/')}"


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dump_mysql(url: str, out_path: Path) -> dict:
    """逻辑全量导出，返回 {tables, row_counts, binlog}。"""
    import pymysql

    cfg = parse_mysql_url(url)
    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["db"],
        charset="utf8mb4",
        autocommit=True,
    )
    tables: list[str] = []
    row_counts: dict[str, int] = {}
    binlog: dict | None = None
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = [r[0] for r in cur.fetchall()]
            with out_path.open("w", encoding="utf-8") as sql:
                sql.write(f"-- SeedAI backup {datetime.now(timezone.utc).isoformat()}\n")
                sql.write(f"-- source: {anonymize(url)}\n")
                sql.write(f"CREATE DATABASE IF NOT EXISTS `{cfg['db']}`;\n")
                sql.write(f"USE `{cfg['db']}`;\n")
                for table in tables:
                    cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                    cnt = cur.fetchone()[0]
                    row_counts[table] = cnt
                    cur.execute(f"SHOW CREATE TABLE `{table}`")
                    create_stmt = cur.fetchone()[1]
                    sql.write(f"\n-- table {table} ({cnt} rows)\n")
                    sql.write(f"DROP TABLE IF EXISTS `{table}`;\n")
                    sql.write(f"{create_stmt};\n")
                    cur.execute(f"SELECT * FROM `{table}`")
                    cols = [d[0] for d in cur.description]
                    col_list = ", ".join(f"`{c}`" for c in cols)
                    while True:
                        rows = cur.fetchmany(500)
                        if not rows:
                            break
                        for row in rows:
                            vals = ", ".join(_sql_literal(v) for v in row)
                            sql.write(f"INSERT INTO `{table}` ({col_list}) VALUES ({vals});\n")
            # binlog 位点（决定 RPO 能否 <5min）
            try:
                with conn.cursor() as c2:
                    c2.execute("SHOW MASTER STATUS")
                    row = c2.fetchone()
                    if row:
                        names = [d[0] for d in c2.description]
                        binlog = dict(zip(names, row))
            except Exception as exc:  # pragma: no cover
                binlog = {"error": str(exc)}
    finally:
        conn.close()
    return {"tables": tables, "row_counts": row_counts, "binlog": binlog}


def _sql_literal(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


def tar_directory(src: Path, dst: Path) -> int:
    """打包 src 到 dst.tar.gz，返回文件数。"""
    count = 0
    with tarfile.open(dst, "w:gz") as tar:
        for child in sorted(src.rglob("*")):
            if child.is_file():
                tar.add(child, arcname=str(child.relative_to(src)))
                count += 1
    return count


def gpg_encrypt(path: Path, recipient: str) -> Path | None:
    """若存在 gpg 则加密为 path.gpg 并返回；否则返回 None。"""
    try:
        subprocess.run(
            ["gpg", "--batch", "--yes", "--trust-model", "always", "-e", "-r", recipient, str(path)],
            check=True,
            capture_output=True,
        )
        return path.with_suffix(path.suffix + ".gpg")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"[warn] gpg 加密不可用({exc})，仅保留未加密归档", file=sys.stderr)
        return None


def build_manifest(
    *,
    mysql_url: str,
    dump_path: Path,
    artifact_archive: Path | None,
    dump_info: dict,
    artifact_count: int,
    encrypt: bool,
) -> dict:
    manifest = {
        "service": "seedai",
        "kind": "backup-manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "env": settings.env,
        "source_db": anonymize(mysql_url),
        "mysql": {
            "dump": dump_path.name,
            "dump_sha256": sha256_file(dump_path) if dump_path.exists() else None,
            "tables": dump_info["tables"],
            "row_counts": dump_info["row_counts"],
            "binlog": dump_info["binlog"],
        },
        "artifacts": {
            "archive": artifact_archive.name if artifact_archive else None,
            "archive_sha256": sha256_file(artifact_archive) if artifact_archive and artifact_archive.exists() else None,
            "file_count": artifact_count,
        },
        "encryption": "gpg" if encrypt else "none",
        "rpo_objective_min": 5,
        "rto_objective_min": 30,
        "notes": [
            "RPO<5min 需启用 MySQL binlog 并定期归档 binlog；binlog 位点见 mysql.binlog。",
            "RTO<30min 取决于归档体积与网络；恢复见 scripts/restore.py。",
            "备份凭证应与生产部署凭证隔离，并跨账号/跨区保存（由运维在外部完成）。",
        ],
    }
    return manifest


def plan_backup(out_dir: Path, encrypt: bool) -> dict:
    return {
        "out_dir": str(out_dir),
        "encrypt": encrypt,
        "mysql_url": anonymize(settings.database_url),
        "artifact_dir": settings.artifact_dir,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="SeedAI 备份（MySQL 全量 + 产物归档）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "backups", help="备份输出根目录")
    ap.add_argument("--no-encrypt", action="store_true", help="跳过 gpg 加密（即使存在）")
    ap.add_argument("--gpg-recipient", default=os.getenv("BACKUP_GPG_RECIPIENT", ""), help="gpg 接收者")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir / stamp
    encrypt = (not args.no_encrypt) and bool(args.gpg_recipient)

    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan_backup(out_dir, encrypt)}, ensure_ascii=False, indent=2))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    dump_path = out_dir / DUMP_FILENAME
    print(f"[backup] MySQL 导出 -> {dump_path}")
    dump_info = dump_mysql(settings.database_url, dump_path)

    artifact_archive: Path | None = None
    artifact_count = 0
    artifact_src = Path(settings.artifact_dir)
    if artifact_src.is_dir():
        artifact_archive = out_dir / ARTIFACT_ARCHIVE
        print(f"[backup] 产物归档 -> {artifact_archive}")
        artifact_count = tar_directory(artifact_src, artifact_archive)
        if encrypt:
            gpg_encrypt(artifact_archive, args.gpg_recipient)

    manifest = build_manifest(
        mysql_url=settings.database_url,
        dump_path=dump_path,
        artifact_archive=artifact_archive,
        dump_info=dump_info,
        artifact_count=artifact_count,
        encrypt=encrypt,
    )
    manifest_path = out_dir / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[backup] manifest -> {manifest_path}")
    print(f"[backup] 完成: {len(dump_info['tables'])} 张表, {artifact_count} 个产物文件")
    if not dump_info["binlog"]:
        print("[warn] 未取到 binlog 位点，RPO 可能 >5min；请确认 MySQL 已启用 log_bin。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
