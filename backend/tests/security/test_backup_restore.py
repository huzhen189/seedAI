"""备份/恢复脚本纯逻辑测试（M10d）：不触达数据库。

覆盖: MySQL URL 解析、SHA256、manifest 结构、恢复计划组装。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from backup import (  # noqa: E402
    anonymize,
    build_manifest,
    parse_mysql_url,
    plan_backup,
    sha256_file,
)


def test_parse_mysql_url() -> None:
    cfg = parse_mysql_url("mysql+pymysql://root:secret@db.example.com:3306/seed_ai")
    assert cfg["host"] == "db.example.com"
    assert cfg["port"] == 3306
    assert cfg["user"] == "root"
    assert cfg["password"] == "secret"
    assert cfg["db"] == "seed_ai"


def _ctypes_delete(path: Path) -> None:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.DeleteFileW(str(path))


def test_sha256_known_vector() -> None:
    p = Path(__file__).parent / ".tmp_sha_test.txt"
    p.write_text("abc", encoding="utf-8")
    try:
        assert sha256_file(p) == hashlib.sha256(b"abc").hexdigest()
    finally:
        _ctypes_delete(p)


def test_anonymize_strips_credentials() -> None:
    a = anonymize("mysql+pymysql://root:secret@db:3306/seed_ai")
    assert "secret" not in a
    assert "root" not in a
    assert "seed_ai" in a


def test_build_manifest_shape() -> None:
    info = {"tables": ["users", "projects"], "row_counts": {"users": 1, "projects": 0}, "binlog": {"File": "binlog.0001"}}
    m = build_manifest(
        mysql_url="mysql+pymysql://root:secret@db:3306/seed_ai",
        dump_path=Path("/backup/mysql_dump.sql"),
        artifact_archive=Path("/backup/artifacts.tar.gz"),
        dump_info=info,
        artifact_count=3,
        encrypt=False,
    )
    assert m["kind"] == "backup-manifest"
    assert m["mysql"]["tables"] == ["users", "projects"]
    assert m["mysql"]["row_counts"]["users"] == 1
    assert m["mysql"]["binlog"]["File"] == "binlog.0001"
    assert m["artifacts"]["file_count"] == 3
    assert m["encryption"] == "none"
    assert m["rpo_objective_min"] == 5 and m["rto_objective_min"] == 30
    # 凭证不得出现在 manifest
    assert "secret" not in str(m)


def test_plan_backup_keys() -> None:
    plan = plan_backup(Path("/backup/x"), encrypt=False)
    assert "out_dir" in plan and "mysql_url" in plan and "artifact_dir" in plan
    assert "secret" not in plan["mysql_url"]
