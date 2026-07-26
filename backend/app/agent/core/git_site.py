"""§8: 基于 Git 的站点版本控制 + COS(本地优先, COS 优雅降级)。

设计要点(已与用户逐项确认):
  1. 每轮 agent turn 自动 commit —— 历史最细,精确到每次小改;
  2. 允许实验分支 exp/<name> —— create_branch / checkout 支持自由探索;
  3. 发布态直接走 COS 静态托管 —— 顺便治 P0-2 沙箱逃逸(PreviewPane/RightPanel 已改 sandbox);
  4. 开启 LFS —— 大二进制(图片/字体)走 git-lfs,本地若无 lfs CLI 则降级为普通文件;
  5. 本地工作树全按需从 COS 恢复 —— restore_from_cos 拉 bundle 后克隆回本地。

框架选型: subprocess + git CLI + asyncio.to_thread。
  - git 是 IO 密集型,to_thread 不阻塞事件循环(与 P0-1 的 langchain offload 同思路);
  - 不引入 GitPython(部署多一个 C 依赖),git CLI 普遍自带,本地/容器一致;
  - 所有外部调用(cwd=repo)经 _run 统一封装,失败返回结构化结果,绝不抛未捕获异常。

工作树位置: 复用 generate_site 的落盘目录 `ARTIFACT_DIR/anon/<trace_id>`,
即"仓库根 = 站点目录"。这样无需拷贝,每轮生成完就地 commit,回滚也是原地 checkout。
(若后期改为 `data/sites/<pid>/` 布局,只需改 _repo_path 一处。)
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ..config import settings


logger = logging.getLogger("ai_service.git_site")

# 默认 artifact 目录(与 generate_site._deliver / cos_upload 配置同源)
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
# 版本仓库统一根(与生成物同树,但按 trace 隔离)
GIT_ROOT = ARTIFACT_DIR / "anon"


def _repo_path(trace_id: str) -> Path:
    """站点目录即 git 仓库根(anon/<trace_id>)。"""
    return GIT_ROOT / (trace_id or "site")


def _run(repo: Path, *args: str) -> dict:
    """在 repo 目录执行 git 子命令,返回结构化结果。

    失败(非零退出/异常)一律吞掉返回 {"ok": False, ...},由调用方决定是否告警,
    绝不让版本控制故障影响主生成链路(QC 同策略)。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "out": out,
            "err": err,
        }
    except FileNotFoundError:
        return {"ok": False, "rc": -1, "out": "", "err": "git 未安装"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -2, "out": "", "err": "git 执行超时(60s)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "rc": -3, "out": "", "err": f"{type(e).__name__}: {e}"}


def _has_git() -> bool:
    r = _run(ARTIFACT_DIR, "--version")
    return r["ok"]


def ensure_repo(repo: Path) -> dict:
    """确保目录是 git 仓库:无 .git 则 init + 设本地 user + 尝试 init LFS。

    返回 {"ok", "initialized", "lfs", "err"}。
    """
    repo.mkdir(parents=True, exist_ok=True)
    git_dir = repo / ".git"
    if git_dir.exists():
        return {"ok": True, "initialized": False, "lfs": False, "err": ""}

    init = _run(repo, "init", "-q")
    if not init["ok"]:
        return {"ok": False, "initialized": False, "lfs": False, "err": init["err"]}

    # 设本地提交身份(避免 "Author identity unknown" 导致 commit 失败)
    _run(repo, "config", "user.email", "seedai@local")
    _run(repo, "config", "user.name", "SeedAI Agent")
    # 关掉仓库级 hooks 自动执行(沙箱/容器内 hooks 可能引发意外)
    _run(repo, "config", "core.hooksPath", "/dev/null")

    # 尝试开启 LFS(大二进制走 lfs);无 lfs CLI 则降级为普通文件,不致命
    lfs = False
    lfs_init = _run(repo, "lfs", "install", "--local")
    if lfs_init["ok"]:
        # 默认对常见大二进制类型启用 LFS(track 失败不影响普通文件提交)
        for pat in ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.woff", "*.woff2", "*.ttf", "*.otf"):
            _run(repo, "lfs", "track", pat)
        lfs = True

    return {"ok": True, "initialized": True, "lfs": lfs, "err": ""}


def commit(repo: Path, message: str, paths: Optional[list[str]] = None) -> dict:
    """对当前站点目录做一次 commit(每轮 agent turn 调用)。

    - paths=None → `git add -A`(新增/修改/删除全量快照,历史最细);
    - 若无可提交变更,返回 {"committed": False, "reason": "no changes"},不报错;
    - 返回 {"committed", "sha", "err"}。
    """
    if not repo.exists():
        return {"committed": False, "sha": None, "err": f"repo 不存在: {repo}"}
    # 确保是仓库(幂等)
    if not (repo / ".git").exists():
        e = ensure_repo(repo)
        if not e["ok"]:
            return {"committed": False, "sha": None, "err": e["err"]}

    add = _run(repo, "add", "--", *paths) if paths else _run(repo, "add", "-A")
    if not add["ok"]:
        return {"committed": False, "sha": None, "err": add["err"]}

    # 判断是否有变更(无则跳过,避免空 commit)
    status = _run(repo, "status", "--porcelain")
    if not status["out"].strip():
        return {"committed": False, "sha": None, "reason": "no changes", "err": ""}

    res = _run(repo, "commit", "-q", "-m", message)
    if not res["ok"]:
        return {"committed": False, "sha": None, "err": res["err"]}
    sha = _run(repo, "rev-parse", "HEAD").get("out")
    return {"committed": True, "sha": sha, "err": ""}


def list_versions(repo: Path) -> list[dict]:
    """列出历史版本(commits),新→旧。无仓库/无提交返回 []。"""
    if not (repo / ".git").exists():
        return []
    res = _run(
        repo,
        "log",
        "--pretty=format:%H|%an|%at|%s",
        "-n", "100",
    )
    if not res["ok"] or not res["out"].strip():
        return []
    versions: list[dict] = []
    for line in res["out"].splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        sha, author, ts, msg = parts
        versions.append({"sha": sha, "author": author, "ts": int(ts or 0), "message": msg})
    return versions


def latest_ref(repo: Path) -> Optional[str]:
    if not (repo / ".git").exists():
        return None
    return _run(repo, "rev-parse", "HEAD").get("out") or None


def create_branch(repo: Path, name: str) -> dict:
    """创建并切换到实验分支 exp/<name>(决策 2)。"""
    if not (repo / ".git").exists():
        ensure_repo(repo)
    br = name if name.startswith("exp/") else f"exp/{name}"
    res = _run(repo, "checkout", "-b", br)
    return {"ok": res["ok"], "branch": br, "err": res.get("err", "")}


def checkout(repo: Path, ref: str) -> dict:
    """切换到某分支/commit(实验探索或回看历史)。"""
    res = _run(repo, "checkout", ref)
    return {"ok": res["ok"], "err": res.get("err", "")}


def rollback(repo: Path, ref: str, message: Optional[str] = None) -> dict:
    """回滚到历史版本:用该版本的文件树创建一次新的"回滚提交",历史线性不丢。

    与 `git reset --hard` 不同 —— 后者会丢弃 ref 之后的提交;此处保留完整历史,
    回滚本身也是一条可追溯记录(决策安全的体现)。
    返回 {"ok", "sha", "err"}。
    """
    if not (repo / ".git").exists():
        return {"ok": False, "sha": None, "err": "repo 不存在"}
    # 把工作树恢复到 ref 的文件树(不移动 HEAD)
    co = _run(repo, "checkout", ref, "--", ".")
    if not co["ok"]:
        return {"ok": False, "sha": None, "err": co["err"]}
    msg = message or f"rollback to {ref[:8]}"
    return commit(repo, msg)


def bundle_to_cos(repo: Path, cos_key: Optional[str] = None) -> dict:
    """把整个仓库打包成 bundle(含全部历史+分支)并上传 COS(决策 3/5)。

    COS 密钥/SDK 缺失时**优雅跳过**,返回 {"skipped": True},不影响本地版本控制。
    实现复用 tools.cos_upload.cos_upload(同一套腾讯云 SDK)。
    """
    if not (repo / ".git").exists():
        return {"ok": False, "skipped": True, "err": "repo 不存在,跳过 bundle"}
    if not (settings.cos_secret_id and settings.cos_secret_key):
        return {"ok": False, "skipped": True, "err": "未配置 COS 密钥,跳过 bundle 上传"}

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".bundle", prefix=f"site_{repo.name}_")
        os.close(fd)
        b = _run(repo, "bundle", "create", tmp, "--all")
        if not b["ok"]:
            return {"ok": False, "skipped": False, "err": f"bundle 失败: {b['err']}"}
        key = cos_key or f"{settings.cos_base_path.strip('/')}/git/{repo.name}.bundle"
        from ..tools.cos_upload import cos_upload

        res = cos_upload(tmp, key)
        if res.get("ok"):
            return {"ok": True, "skipped": False, "url": res.get("url"), "key": key}
        # 上传失败(密钥无效 / SDK 缺失 / 网络):视为优雅跳过,COS 只是本地 git 的镜像,
        # 上传失败绝不能阻断生成主链路或丢失本地版本历史。
        return {"ok": False, "skipped": True, "err": res.get("error", "cos_upload 失败")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "skipped": False, "err": f"{type(e).__name__}: {e}"}
    finally:
        with contextlib.suppress(Exception):
            if tmp and os.path.exists(tmp):
                os.remove(tmp)


def restore_from_cos(cos_key: str, dest: Optional[Path] = None) -> dict:
    """按需从 COS 拉取 bundle,克隆回本地工作树(决策 5:本地全按需恢复)。

    COS 缺失时返回 {"skipped": True}。克隆到 dest(默认 GIT_ROOT/<bundle名>)。
    """
    if not (settings.cos_secret_id and settings.cos_secret_key):
        return {"ok": False, "skipped": True, "err": "未配置 COS 密钥"}
    try:
        from qcloud_cos import CosConfig, CosS3Client

        cfg = CosConfig(
            Region=settings.cos_region,
            SecretId=settings.cos_secret_id,
            SecretKey=settings.cos_secret_key,
        )
        client = CosS3Client(cfg)
        tmp = tempfile.mkstemp(suffix=".bundle", prefix="restore_")[1]
        os.close(tempfile.mkstemp(suffix=".bundle", prefix="restore_")[0])
        resp = client.get_object(Bucket=settings.cos_bucket, Key=cos_key)
        resp["Body"].get_stream_to_file(tmp)
        target = dest or (GIT_ROOT / Path(cos_key).stem)
        target.mkdir(parents=True, exist_ok=True)
        if any(target.iterdir()):
            import shutil

            shutil.rmtree(target)
        clone = subprocess.run(
            ["git", "clone", tmp, str(target)],
            capture_output=True, text=True, timeout=120,
        )
        os.remove(tmp)
        if clone.returncode != 0:
            return {"ok": False, "skipped": False, "err": clone.stderr.strip()}
        return {"ok": True, "skipped": False, "path": str(target)}
    except ImportError:
        return {"ok": False, "skipped": True, "err": "cos-python-sdk-v5 未安装"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "skipped": False, "err": f"{type(e).__name__}: {e}"}


def commit_site_for_trace(trace_id: str, skill_name: str, user_prompt: str = "") -> dict:
    """Worker 在每轮生成完成后调用的便捷入口(同步,由 asyncio.to_thread 包裹)。

    - 站点目录 = 仓库根;
    - commit message 含 skill 与用户诉求截断,便于回看"这次改了啥";
    - 完成后顺带 bundle 上传 COS(失败仅告警,不阻断)。
    """
    repo = _repo_path(trace_id)
    if not repo.exists():
        return {"committed": False, "sha": None, "err": f"站点目录尚不存在: {repo}"}
    prompt_snip = (user_prompt or "").replace("\n", " ").strip()[:80]
    msg = f"{skill_name}: {prompt_snip}" if prompt_snip else f"{skill_name}: auto-commit"
    c = commit(repo, msg)
    if c.get("committed"):
        logger.info("[git_site] trace=%s 已提交 %s (%s)", trace_id, c.get("sha", "")[:8], skill_name)
        # 发布态: 把 bundle 推 COS(优雅跳过)
        bundle_to_cos(repo)
    else:
        # 无变更 / 失败: 仅记录,不报错
        logger.debug("[git_site] trace=%s commit 跳过: %s", trace_id, c.get("err") or c.get("reason"))
    return c
