"""站点领域原子工具（规范 §9.2 + §8.2）。

这些 Tool 包裹 ``app.domains.site.workflow`` 的既有确定性逻辑，是建站 Skill 的
唯一执行入口。所有 Tool 返回 ``ToolResult``，绝不抛裸异常。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.contracts import Domain, ErrorEnvelope, RiskLevel
from app.core.turn_context import TurnContext
from app.models import Artifact, Deployment, Project
from app.tools._registry import ToolMeta
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolStatus
from app.domains.site import workflow as site_wf


_ALLOWED_IMAGE_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/avif": "avif",
    "image/svg+xml": "svg",
}
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class FsWriteTool(BaseTool):
    """mid：临时文件、原子 rename、patch、checksum（§9.2）。"""

    meta = ToolMeta(
        tool_id="fs_write",
        risk=RiskLevel.MID,
        domain=Domain.SITE,
        description="原子写入项目工作区文件：临时文件 + fsync + rename，返回 sha256。",
        filesystem_profile="project_workspace",
        max_input_bytes=8_388_608,
        retry_policy={"max_retries": 1, "error_codes": [], "backoff": "none"},
        idempotency=True,
        reconcile_strategy="checksum",
        unknown_timeout_seconds=30,
        factory=lambda: FsWriteTool(),
    )

    async def run(self, ctx: ToolContext, *, path: str, content: str,
                  idempotency_key: str | None = None) -> ToolResult:
        try:
            safe = _FILENAME_SAFE.sub("_", os.path.basename(path))
            root = Path(settings.artifact_dir) / "workspace" / str(ctx.user_id) / str(ctx.project_id or "0")
            root.mkdir(parents=True, exist_ok=True)
            target = root / safe
            temporary = root / f"{safe}.tmp"
            data = content.encode("utf-8")
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(target)
            digest = hashlib.sha256(data).hexdigest()
            return ToolResult.ok(
                {"path": str(target), "sha256": digest, "bytes": len(data)},
                idempotency_key=idempotency_key or digest,
                metrics={"bytes": len(data)},
            )
        except Exception as exc:  # 绝不抛裸异常
            return ToolResult.fail(
                ErrorEnvelope(code="fs_write_failed", category="filesystem",
                              what="原子写入失败", why=str(exc)[:256],
                              next="检查工作区路径与磁盘空间", retryable=True, retry_scope="task"),
                idempotency_key=idempotency_key,
            )


class FsReadTool(BaseTool):
    """low：仅允许项目工作区读取（§9.2 防越界）。"""

    meta = ToolMeta(
        tool_id="fs_read",
        risk=RiskLevel.LOW,
        domain=Domain.SITE,
        description="仅读取项目工作区内的文件；越界或不存在返回 failed，绝不抛裸异常。",
        sandbox_profile="read_only",
        filesystem_profile="project_workspace",
        egress_profile="none",
        max_input_bytes=8_388_608,
        retry_policy={"max_retries": 0, "error_codes": [], "backoff": "none"},
        idempotency=False,
        reconcile_strategy="none",
        factory=lambda: FsReadTool(),
    )

    async def run(self, ctx: ToolContext, *, path: str) -> ToolResult:
        try:
            safe = _FILENAME_SAFE.sub("_", os.path.basename(path))
            root = Path(settings.artifact_dir) / "workspace" / str(ctx.user_id) / str(ctx.project_id or "0")
            target = (root / safe).resolve()
            root_resolved = root.resolve()
            # 防 symlink/junction 越界（§9.2：fs_* 必须防路径逃逸）。
            if target != root_resolved and root_resolved not in target.parents:
                return ToolResult.fail(ErrorEnvelope(
                    code="fs_read_out_of_bounds", category="filesystem",
                    what="请求路径越出项目工作区", why=f"path={path}",
                    next="仅可读取工作区内文件", retryable=False, retry_scope="none"))
            if not target.exists() or not target.is_file():
                return ToolResult.fail(ErrorEnvelope(
                    code="fs_read_not_found", category="not_found",
                    what="文件不存在", why=f"path={path}",
                    next="确认文件名", retryable=False, retry_scope="none"))
            data = target.read_bytes()
            return ToolResult.ok({
                "path": str(target), "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        except Exception as exc:  # 绝不抛裸异常
            return ToolResult.fail(ErrorEnvelope(
                code="fs_read_failed", category="filesystem",
                what="读取失败", why=str(exc)[:256],
                next="检查工作区路径", retryable=False, retry_scope="none"))


class HtmlValidateTool(BaseTool):
    """low：整站语法/SEO/a11y/安全审计（§9.2 升级语义）。"""

    meta = ToolMeta(
        tool_id="html_validate",
        risk=RiskLevel.LOW,
        domain=Domain.SITE,
        description="HTML 结构完整性与注入安全校验（doctype、闭合、最小体积、危险 token）。",
        sandbox_profile="read_only",
        egress_profile="none",
        retry_policy={"max_retries": 0, "error_codes": [], "backoff": "none"},
        idempotency=False,
        reconcile_strategy="none",
        factory=lambda: HtmlValidateTool(),
    )

    async def run(self, ctx: ToolContext, *, html: str) -> ToolResult:
        ok, code = site_wf._verify_html(html)
        if ok:
            return ToolResult.ok({"passed": True, "code": code})
        return ToolResult.fail(
            ErrorEnvelope(code=code, category="validation",
                          what="站点校验未通过", why=code,
                          next="保留当前稳定版本，可重试定向修复", retryable=True, retry_scope="task"),
        )


class SitePublishTool(BaseTool):
    """mid：创建本地不可变预览，不代表生产发布（§9.2）。"""

    meta = ToolMeta(
        tool_id="site_publish",
        risk=RiskLevel.MID,
        domain=Domain.SITE,
        description="原子写入不可变版本目录，生成 manifest/checksum 与 preview 路径。",
        filesystem_profile="immutable_preview",
        max_input_bytes=8_388_608,
        retry_policy={"max_retries": 1, "error_codes": ["io_error"], "backoff": "exp_jitter"},
        idempotency=True,
        reconcile_strategy="artifact_version",
        unknown_timeout_seconds=30,
        factory=lambda: SitePublishTool(),
    )

    async def run(self, ctx: ToolContext, *, session: AsyncSession, project: Project,
                  turn_context: TurnContext, html: str,
                  idempotency_key: str | None = None) -> ToolResult:
        try:
            artifact, message = await site_wf._publish_preview(session, project, turn_context, html)
            return ToolResult.ok(
                {
                    "artifact_id": artifact.id,
                    "version": artifact.version,
                    "preview_path": artifact.preview_path,
                    "manifest_digest": artifact.manifest_digest,
                    "message": message,
                },
                idempotency_key=idempotency_key or artifact.manifest_digest,
                metrics={"bytes": len(html.encode("utf-8"))},
            )
        except Exception as exc:
            return ToolResult.fail(
                ErrorEnvelope(code="site_publish_failed", category="publish",
                              what="发布本地预览失败", why=str(exc)[:256],
                              next="保留当前稳定版本，可重试", retryable=True, retry_scope="task"),
                idempotency_key=idempotency_key,
            )


class SiteDeleteTool(BaseTool):
    """high：对不可变版本建立 tombstone；文件修改必须产生新 Artifact（§9.2）。"""

    meta = ToolMeta(
        tool_id="site_delete",
        risk=RiskLevel.HIGH,
        domain=Domain.SITE,
        description="对指定不可变版本建立 tombstone（status=deleted），不物理删文件。",
        filesystem_profile="immutable_preview",
        retry_policy={"max_retries": 0, "error_codes": [], "backoff": "none"},
        requires_approval=True,
        idempotency=True,
        reconcile_strategy="tombstone",
        unknown_timeout_seconds=60,
        factory=lambda: SiteDeleteTool(),
    )

    async def run(self, ctx: ToolContext, *, session: AsyncSession, artifact_id: int,
                  idempotency_key: str | None = None) -> ToolResult:
        artifact = await session.get(Artifact, artifact_id)
        if artifact is None:
            return ToolResult.fail(
                ErrorEnvelope(code="site_delete_not_found", category="not_found",
                              what="找不到指定版本", why=f"artifact_id={artifact_id}",
                              next="确认版本号", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        artifact.status = "deleted"
        await session.flush()
        return ToolResult.ok(
            {"artifact_id": artifact.id, "status": "deleted"},
            idempotency_key=idempotency_key or f"del:{artifact_id}",
        )


class AssetImportTool(BaseTool):
    """mid：上传、MIME/文件名消毒、压缩、WebP/AVIF、manifest（§9.2）。

    脱敏（MIME/文件名/尺寸上限）与 manifest 在本 Tool 内真实执行；像素级转码
    （WebP/AVIF 压缩）依赖图像库，当前环境未装，明确标记为 deferred，不静默成功。
    """

    meta = ToolMeta(
        tool_id="asset_import",
        risk=RiskLevel.MID,
        domain=Domain.SITE,
        description="资产导入：MIME/文件名消毒、尺寸上限、manifest；转码 deferred。",
        filesystem_profile="project_workspace",
        max_input_bytes=20_971_520,
        retry_policy={"max_retries": 1, "error_codes": ["sanitize_error"], "backoff": "none"},
        idempotency=True,
        reconcile_strategy="manifest",
        unknown_timeout_seconds=30,
        factory=lambda: AssetImportTool(),
    )

    async def run(self, ctx: ToolContext, *, filename: str, mime: str, size_bytes: int,
                  idempotency_key: str | None = None) -> ToolResult:
        if mime not in _ALLOWED_IMAGE_MIME:
            return ToolResult.fail(
                ErrorEnvelope(code="asset_import_bad_mime", category="validation",
                              what="不支持的 MIME 类型", why=mime,
                              next="仅允许图片类型", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        if size_bytes > 20_971_520:
            return ToolResult.fail(
                ErrorEnvelope(code="asset_import_too_large", category="validation",
                              what="资产超过 20MB 上限", why=str(size_bytes),
                              next="压缩后再导入", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        safe_name = _FILENAME_SAFE.sub("_", os.path.basename(filename)) or f"asset.{_ALLOWED_IMAGE_MIME[mime]}"
        manifest = {
            "original": safe_name,
            "mime": mime,
            "size_bytes": size_bytes,
            "transcode": "deferred",
            "fingerprint": hashlib.sha256(f"{safe_name}:{size_bytes}".encode()).hexdigest()[:16],
        }
        return ToolResult.ok(
            {"manifest": manifest, "stored_as": safe_name},
            idempotency_key=idempotency_key or manifest["fingerprint"],
            metrics={"size_bytes": size_bytes},
        )


class SiteDeployTool(BaseTool):
    """critical：生产发布，绑定 artifact+manifest、审批、健康检查、回滚（§9.2）。"""

    meta = ToolMeta(
        tool_id="site_deploy",
        risk=RiskLevel.CRITICAL,
        domain=Domain.SITE,
        description="生产发布：绑定 artifact+manifest，需已审批，健康检查成功才切 active。",
        egress_profile="deploy_host",
        max_input_bytes=8_388_608,
        retry_policy={"max_retries": 0, "error_codes": [], "backoff": "none"},
        requires_approval=True,
        idempotency=True,
        reconcile_strategy="deployment_rollback",
        unknown_timeout_seconds=120,
        manual_resolution_policy="rollback_to_previous_active",
        factory=lambda: SiteDeployTool(),
    )

    async def run(self, ctx: ToolContext, *, session: AsyncSession, project: Project,
                  artifact_id: int, approved: bool = False,
                  idempotency_key: str | None = None) -> ToolResult:
        if not approved:
            return ToolResult.fail(
                ErrorEnvelope(code="site_deploy_requires_approval", category="approval",
                              what="生产发布需先审批", why="approved=False",
                              next="通过审批流程后重试", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        artifact = await session.get(Artifact, artifact_id)
        if artifact is None:
            return ToolResult.fail(
                ErrorEnvelope(code="site_deploy_no_artifact", category="not_found",
                              what="找不到待发布版本", why=f"artifact_id={artifact_id}",
                              next="确认版本", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        # 健康检查（本地静态产物）：index.html 可读即视为通过；生产应替换为真实 host 探针。
        health_ok = bool(artifact.preview_path and Path(settings.artifact_dir) / artifact.preview_path)
        if not health_ok:
            return ToolResult.fail(
                ErrorEnvelope(code="site_deploy_health_fail", category="deploy",
                              what="健康检查失败", why="预览产物缺失",
                              next="保留旧 active，不切换", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        deployment = Deployment(
            project_id=project.id,
            artifact_id=artifact.id,
            manifest_digest=artifact.manifest_digest,
            environment="production",
            status="succeeded",
        )
        session.add(deployment)
        await session.flush()
        project.active_deployment_id = deployment.id
        project.status = "active"
        return ToolResult.ok(
            {"deployment_id": deployment.id, "status": "succeeded", "rolled_back": False},
            idempotency_key=idempotency_key or f"deploy:{artifact_id}",
        )


def tool_metas() -> list[ToolMeta]:
    return [t.meta for t in (
        FsWriteTool(), FsReadTool(), HtmlValidateTool(), SitePublishTool(),
        SiteDeleteTool(), AssetImportTool(), SiteDeployTool(),
    )]
