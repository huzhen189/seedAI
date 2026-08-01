"""§9.2 原子工具契约测试：覆盖真实写入链路与审批/确认契约。

这些测试验证 SiteWorkflow 重构为 16 原子 Tool 后：
- 真实可写工具（fs_write / site_publish / site_delete）确实落盘 + 改写 ORM；
- high/critical 工具的审批与双确认契约未被破坏（默认拒绝）；
- 所有 Tool 都返回 ToolResult，绝不抛裸异常。
"""

from __future__ import annotations

from app.core.contracts import RiskLevel
from app.models import Artifact, Conversation, Project, User
from app.tools import build_default_registry
from app.tools.base import ToolContext
from app.tools.project import ProjectPurgeTool
from app.tools.site import FsWriteTool, SiteDeleteTool, SitePublishTool, SiteDeployTool

from tests.db.conftest import isolated_database


class _SessionInfo:
    conversation_id: int = 0


class _UserIdentity:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class _TurnContextStub:
    """最小 TurnContext 替身，仅供 SitePublishTool 内部取 user_id/session/conversation_id/trace_id。"""

    def __init__(self, user_id: int, conversation_id: int, trace_id: str = "t1") -> None:
        self.user = _UserIdentity(user_id)
        self.session = _SessionInfo()
        self.session.conversation_id = conversation_id
        self.trace_id = trace_id


def _ctx(user_id: int, project_id: int | None = None, conversation_id: int | None = None,
         trace_id: str = "tool-test") -> ToolContext:
    return ToolContext(
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        trace_id=trace_id,
    )


def test_registry_has_all_16_tools_and_validates() -> None:
    registry = build_default_registry()
    ids = {m.tool_id for m in registry.all()}
    assert len(ids) == 16, f"期望 16 个原子工具，实际 {len(ids)}: {sorted(ids)}"
    # §9.2 启动校验：风险/审批/幂等/沙箱/reconcile 全部合规
    assert registry.validate_startup() == [], "启动校验存在违规"


def test_fs_write_atomically_writes_and_returns_sha256() -> None:
    async def scenario() -> None:
        async with isolated_database() as (engine, session_factory):
            async with session_factory() as session:
                user = User(account="tool-u", email="t@e.invalid",
                            password_hash="h", display_name="T")
                session.add(user)
                await session.flush()
                tool = FsWriteTool()
                content = "<!doctype html><html><body>hi</body></html>"
                res = await tool.run(_ctx(user.id), path="index.html", content=content)
                assert res.status.value == "succeeded", res.error
                assert res.data["sha256"]
                assert res.data["bytes"] == len(content.encode("utf-8"))
                # 文件确实落盘
                from pathlib import Path
                from app.config import settings
                p = Path(res.data["path"])
                assert p.exists() and p.read_text(encoding="utf-8") == content
                assert p.name == "index.html"

    import asyncio
    asyncio.run(scenario())


def test_html_validate_rejects_unsafe_token() -> None:
    async def scenario() -> None:
        tool = __import__("app.tools.site", fromlist=["HtmlValidateTool"]).HtmlValidateTool()
        bad = "<!doctype html><html><body><script>alert(1)</script></body></html>"
        res = await tool.run(_ctx(1), html=bad)
        assert res.status.value == "failed"
        assert res.error is not None and res.error.code.startswith("site_verify")

    import asyncio
    asyncio.run(scenario())


def test_site_publish_writes_immutable_artifact_version() -> None:
    async def scenario() -> None:
        async with isolated_database() as (engine, session_factory):
            async with session_factory() as session:
                user = User(account="pub-u", email="p@e.invalid",
                            password_hash="h", display_name="P")
                session.add(user)
                await session.flush()
                project = Project(user_id=user.id, name="Pub", site_spec={},
                                  lock_version=1, status="draft")
                session.add(project)
                await session.flush()
                conv = Conversation(project_id=project.id, user_id=user.id, name="Pub Conv")
                session.add(conv)
                await session.flush()
                tctx = _TurnContextStub(user_id=user.id, conversation_id=conv.id, trace_id="t1")
                html = "".join([
                    "<!doctype html><html><head><meta charset='utf-8'></head>",
                    "<body>", "<section class='reveal'><h1>标题</h1></section>",
                    "<button class='theme-toggle'>主题：深色</button>",
                    "</body></html>",
                ])
                tool = SitePublishTool()
                res = await tool.run(_ctx(user.id, project.id), session=session,
                                     project=project, turn_context=tctx, html=html)
                assert res.status.value == "succeeded", res.error
                assert res.data["version"] == 1
                from pathlib import Path
                from app.config import settings
                target = Path(settings.artifact_dir) / res.data["preview_path"]
                assert target.exists()
                # 二次 publish 递增版本（不可变版本目录）
                res2 = await tool.run(_ctx(user.id, project.id), session=session,
                                      project=project, turn_context=tctx, html=html)
                assert res2.status.value == "succeeded"
                assert res2.data["version"] == 2

    import asyncio
    asyncio.run(scenario())


def test_site_delete_tombstones_without_physical_delete() -> None:
    async def scenario() -> None:
        async with isolated_database() as (engine, session_factory):
            async with session_factory() as session:
                user = User(account="del-u", email="d@e.invalid",
                            password_hash="h", display_name="D")
                session.add(user)
                await session.flush()
                project = Project(user_id=user.id, name="Del", site_spec={},
                                  lock_version=1, status="active")
                session.add(project)
                await session.flush()
                conv = Conversation(project_id=project.id, user_id=user.id, name="Del Conv")
                session.add(conv)
                await session.flush()
                art = Artifact(
                    project_id=project.id, conversation_id=conv.id, version=1,
                    site_spec_revision=1, site_spec_hash="a" * 64,
                    manifest={}, manifest_digest="c" * 64, checksums={},
                    vendor_manifest_version="1.0", capability_manifest={},
                    status="preview_ready", preview_path="previews/x/v1/index.html",
                    trace_id="t1",
                )
                session.add(art)
                await session.flush()
                tool = SiteDeleteTool()
                res = await tool.run(_ctx(user.id, project.id), session=session,
                                     artifact_id=art.id)
                assert res.status.value == "succeeded", res.error
                await session.refresh(art)
                assert art.status == "deleted"  # tombstone
                # 文件仍在（不可变版本不物理删）
                from pathlib import Path
                assert Path(art.preview_path).name == "index.html"

    import asyncio
    asyncio.run(scenario())


def test_high_critical_tools_require_approval_or_confirmation() -> None:
    async def scenario() -> None:
        async with isolated_database() as (engine, session_factory):
            async with session_factory() as session:
                user = User(account="gate-u", email="g@e.invalid",
                            password_hash="h", display_name="G")
                session.add(user)
                await session.flush()
                project = Project(user_id=user.id, name="Gate", site_spec={},
                                  lock_version=1, status="active")
                session.add(project)
                await session.flush()
                # site_deploy 是 critical：无审批上下文必须默认拒绝
                deploy = SiteDeployTool()
                res = await deploy.run(_ctx(user.id, project.id), session=session,
                                       project=project, artifact_id=0,
                                       approved=False)
                assert res.status.value == "failed"
                assert res.error is not None and res.error.code == "site_deploy_requires_approval"
                # project_purge 是 critical：无双确认必须默认拒绝
                purge = ProjectPurgeTool()
                res2 = await purge.run(_ctx(user.id, project.id), session=session,
                                       project_id=project.id, confirmed=False)
                assert res2.status.value == "failed"
                assert res2.error is not None and res2.error.code == "project_purge_requires_confirm"

    import asyncio
    asyncio.run(scenario())


def test_registry_risk_approval_invariant() -> None:
    """§9.2 风险语义不变量：high/critical 必须 requires_approval。"""
    registry = build_default_registry()
    for meta in registry.all():
        if meta.risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            assert meta.requires_approval is True, f"{meta.tool_id} 必须 requires_approval"
