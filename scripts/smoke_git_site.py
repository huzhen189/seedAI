"""§8 git_site 功能测试:用真实 git 走完整版本控制生命周期。

不依赖 LLM / COS / 服务,纯验证模块逻辑:
  ensure_repo → 3 轮 commit → list_versions(3) → rollback → 分支 → bundle(优雅跳过)。
通过 ARTIFACT_DIR 指向临时目录,不污染真实产物。
"""
import os
import sys
import tempfile
from pathlib import Path

# 用临时 artifact 根,隔离测试
TMP = Path(tempfile.mkdtemp(prefix="git_site_smoke_"))
os.environ["ARTIFACT_DIR"] = str(TMP)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "ai_service"))
from app.core import git_site as G  # noqa: E402


def w(repo, name, content):
    p = repo / name
    p.write_text(content, encoding="utf-8")


def main():
    print("== §8 git_site 功能测试 ==")
    print(f"artifact 根: {TMP}")
    trace = "smoke-trace-git"
    repo = G._repo_path(trace)
    print(f"repo 路径: {repo}")

    # 1) ensure_repo
    r = G.ensure_repo(repo)
    print(f"[1] ensure_repo ok={r['ok']} initialized={r['initialized']} lfs={r['lfs']}")

    # 2) 三轮生成(每轮一次 commit)
    w(repo, "index.html", "<html><body>v1 蓝色名片</body></html>")
    c1 = G.commit_site_for_trace(trace, "generate_site", "做蓝色名片落地页")
    w(repo, "index.html", "<html><body>v2 加了邮箱</body></html>")
    c2 = G.commit_site_for_trace(trace, "generate_site", "加邮箱字段")
    w(repo, "style.css", "body{color:#06f}")
    c3 = G.commit_site_for_trace(trace, "generate_site", "加样式文件")
    print(f"[2] commits: v1={c1.get('sha','')[:8]} v2={c2.get('sha','')[:8]} v3={c3.get('sha','')[:8]}")
    assert c1["committed"] and c2["committed"] and c3["committed"], "三轮 commit 应成功"

    # 3) list_versions
    vers = G.list_versions(repo)
    print(f"[3] list_versions 共 {len(vers)} 条:")
    for v in vers:
        print(f"    {v['sha'][:8]} | {v['message']}")
    assert len(vers) == 3, f"期望 3 条,实际 {len(vers)}"

    # 4) rollback 到 v1
    v1 = vers[-1]["sha"]  # list 新→旧,v1 在最后
    rb = G.rollback(repo, v1, message="rollback-test")
    print(f"[4] rollback -> committed={rb.get('committed')} sha={rb.get('sha','')[:8]}")
    assert rb.get("committed"), "rollback 应产生一次新提交"
    # 工作树应回到 v1 内容
    content = (repo / "index.html").read_text(encoding="utf-8")
    print(f"    回滚后 index.html 内容: {content!r}")
    assert "v1" in content, "回滚后应回到 v1 内容"
    # 历史仍线性(4 条: v1,v2,v3,rollback)
    assert len(G.list_versions(repo)) == 4, "rollback 后应为 4 条(历史不丢)"

    # 5) 实验分支
    default_branch = G._run(repo, "rev-parse", "--abbrev-ref", "HEAD").get("out") or "master"
    br = G.create_branch(repo, "exp/dark-theme")
    print(f"[5] create_branch ok={br['ok']} branch={br.get('branch')}")
    w(repo, "style.css", "body{color:#111;background:#000}")
    G.commit(repo, "exp: dark theme css")
    back = G.checkout(repo, default_branch)
    print(f"    checkout {default_branch} ok={back['ok']}")
    assert br["ok"], "分支创建应成功"

    # 6) bundle_to_cos 优雅跳过(无 COS 密钥)
    b = G.bundle_to_cos(repo)
    print(f"[6] bundle_to_cos skipped={b.get('skipped')} err={b.get('err','')}")
    assert b.get("skipped") or b.get("ok"), "无密钥时应优雅跳过而非崩溃"

    print("\n✅ §8 git_site 功能测试全部通过")


if __name__ == "__main__":
    main()
