"""本地产物路径统一助手(P1 改造核心)。

设计要点(已与用户逐项确认):
  - 产物根 = ARTIFACT_DIR(绝对路径, 与 backend 平级);
  - 本地布局: {ARTIFACT_DIR}/{uid}/{pid}/v{ver}/{fname};
  - COS 布局(发布时): previews/{uid}/{pid}/v{ver}/{fname}(与本地同规则, 无需换算);
  - git 仓库根 = {ARTIFACT_DIR}/{uid}/{pid}(一个站点一仓, tag vN 快照回滚)。

所有落盘/索引/git/落库调用方统一走此模块, 消除各处手拼 `anon/<trace>` 的歧义。
"""

from __future__ import annotations

from pathlib import Path

from app.config import settings


def get_artifact_dir() -> Path:
    """绝对产物根目录(只读, 已含兜底解析)。"""
    return Path(settings.artifact_dir)


def _norm_id(uid: int | None, pid: int | None) -> tuple[str, str]:
    """uid/pid → 目录段; None 一律降级 anon(避免空段导致路径串台)。"""
    return (str(uid) if uid is not None else "anon",
            str(pid) if pid is not None else "anon")


def repo_path(uid: int | None, pid: int | None) -> Path:
    """站点 git 仓库根 = {ARTIFACT_DIR}/{uid}/{pid}。"""
    u, p = _norm_id(uid, pid)
    return get_artifact_dir() / u / p


def site_dir(uid: int | None, pid: int | None, version: int | None) -> Path:
    """某版本产物目录 = {ARTIFACT_DIR}/{uid}/{pid}/v{ver}。

    version 为 None(老数据/未下发语义版本)时降级为 'site'(兼容旧 anon 约定),
    但不应再出现在新链路中 —— 仅作安全兜底。
    """
    u, p = _norm_id(uid, pid)
    ver_seg = f"v{version}" if version else "site"
    return get_artifact_dir() / u / p / ver_seg


def to_rel_path(path: str | Path) -> str:
    """任意绝对/相对路径 → 相对 ARTIFACT_DIR 的正斜杠路径(用于存库 + 前端拼接)。"""
    p = Path(path)
    if not p.is_absolute():
        p = (get_artifact_dir() / p).resolve()
    try:
        rel = p.resolve().relative_to(get_artifact_dir().resolve())
    except ValueError:
        # 不在产物树内(异常输入), 直接返回相对表示, 不串台
        rel = p
    return str(rel).replace("\\", "/")


def rel_path_for(uid: int | None, pid: int | None, version: int | None, fname: str) -> str:
    """直接拼出 {uid}/{pid}/v{ver}/{fname} 的存储相对路径。"""
    u, p = _norm_id(uid, pid)
    ver_seg = f"v{version}" if version else "site"
    return f"{u}/{p}/{ver_seg}/{fname}"


def cos_key_for(uid: int | None, pid: int | None, version: int | None, fname: str) -> str:
    """发布时上传 COS 的 key(与本地同规则, previews 前缀)。"""
    base = settings.cos_base_path.strip("/") or "previews"
    return f"{base}/{rel_path_for(uid, pid, version, fname)}"


def trash_dir() -> Path:
    """回收区根目录 = {ARTIFACT_DIR}/.trash。删除项目时把整目录物理移入此处(可恢复)。"""
    return get_artifact_dir() / ".trash"


def find_repo(uid: int | None, pid: int | None) -> Path:
    """查找站点 git 仓库根(若存在)。与 repo_path 同义, 仅语义化别名。"""
    return repo_path(uid, pid)


def parse_rel_path(rel_path: str) -> dict | None:
    """反解 artifacts 静态路径 → {uid, pid, version, fname}。

    支持格式:
      {uid}/{pid}/v{ver}/{fname}
      {uid}/{pid}/v{ver}/{subdir...}/{fname}   (多页面子目录)
      .trash/{project_id}_{ts}/{uid}/{pid}/...  (回收区内, uid/pid 段顺延)
    返回 None 表示无法解析(非法/越界路径)。
    """
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    # 跳过可能的 .trash/{id} 前缀
    idx = 0
    if parts[0] == ".trash":
        idx = 2
        if len(parts) < 3:
            return None
    # 需要至少 uid/pid/ver/fname
    if len(parts) - idx < 4:
        return None
    uid, pid, ver = parts[idx], parts[idx + 1], parts[idx + 2]
    # 校验是否为纯数字段(uid/pid) + 版本段 v{N}
    if not (uid.isdigit() and pid.isdigit() and ver.startswith("v") and ver[1:].isdigit()):
        return None
    fname = "/".join(parts[idx + 3:])
    return {
        "uid": int(uid),
        "pid": int(pid),
        "version": int(ver[1:]),
        "fname": fname,
    }
