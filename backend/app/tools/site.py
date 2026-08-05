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
from app.domains.site.workflow import SiteWorkflow
from app.models import Artifact, Deployment, Project
from app.tools._registry import ToolMeta
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolStatus

import logging

logger = logging.getLogger("app.tools.site")


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
        """原子写入项目工作区文件(§9.2 fs_write)。

        写入策略：先落 ``<name>.tmp`` → ``fsync`` → ``os.replace`` 原子改名，
        返回最终路径与 sha256（供上层做幂等/校验）。mid 风险且声明 idempotency=True，
        幂等键缺省用内容 sha256。

        Args:
            path: 目标文件相对工作区的文件名(自动 basename 消毒,防越界)。
            content: 要写入的文本。
            idempotency_key: 可选外部幂等键。
        Returns:
            ``ToolResult.ok({path, sha256, bytes})``；失败返回 failed(绝不抛异常)。
        """
        logger.debug("[fs_write] user=%s project=%s path=%s bytes=%d", ctx.user_id, ctx.project_id, path, len(content.encode("utf-8")))
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
            logger.info("[fs_write] 写入成功 user=%s project=%s -> %s sha256=%s", ctx.user_id, ctx.project_id, target, digest[:12])
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
        """只读项目工作区文件(§9.2 fs_read, low)。

        安全约束：仅允许读取工作区内文件，越界路径(含 symlink/junction 逃逸)或不存在
        一律返回 failed，绝不抛裸异常、绝不读取工作区外数据。

        Args:
            path: 要读取的文件名(自动 basename 消毒)。
        Returns:
            ``ToolResult.ok({path, bytes, sha256})``；越界/缺失/异常返回 failed。
        """
        logger.debug("[fs_read] user=%s project=%s path=%s", ctx.user_id, ctx.project_id, path)
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
        """HTML 结构完整性与注入安全校验(§9.2 html_validate, low)。

        委托 ``SiteWorkflow.verify`` 执行 doctype/闭合/最小体积/危险 token 检查。
        通过返回 ok；失败携带具体失败 code(供上层定位是哪一项没过)。
        """
        logger.debug("[html_validate] 校验 %d bytes", len(html.encode("utf-8")))
        # 委托 domain 的公开 API（权威校验逻辑在 SiteWorkflow.verify，工具只做薄封装，
        # 依赖方向：tool → domain.workflow，单向、显式，杜绝私有函数耦合）。
        ok, code = SiteWorkflow.verify(html)
        if ok:
            logger.info("[html_validate] 校验通过 user=%s project=%s", ctx.user_id, ctx.project_id)
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
        """发布本地不可变预览(§9.2 site_publish, mid)。

        委托 ``SiteWorkflow.preview`` 原子写入版本目录 + 落 Artifact，
        幂等键缺省用 manifest_digest。注意：这只是生成一个本地 preview 版本，
        不代表生产发布(生产由 site_deploy 负责)。

        Args:
            session/project/turn_context: 建站上下文(由 S6 执行器注入)。
            html: 已通过 html_validate 的完整 HTML。
        Returns:
            ``ToolResult.ok({artifact_id, version, preview_path, manifest_digest, message})``。
        """
        logger.debug("[site_publish] project=%s 发布预览 (%d bytes)", project.id, len(html.encode("utf-8")))
        try:
            # 委托 domain 的公开 API（权威发布逻辑在 SiteWorkflow.preview，工具只做薄封装）。
            artifact, message = await SiteWorkflow.preview(session, project, turn_context, html)
            logger.info("[site_publish] 成功 project=%s artifact=%s v%d path=%s", project.id, artifact.id, artifact.version, artifact.preview_path)
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
        """对不可变版本建立 tombstone(§9.2 site_delete, high, 需审批)。

        文件本身不物理删除(不可变版本语义)，仅把 Artifact.status 置为 ``deleted``；
        high 风险且 requires_approval=True，必须由审批闸门放行后才进入此执行。
        幂等：重复调用同一 artifact 结果一致。

        Args:
            artifact_id: 待删除/置 tombstone 的 Artifact id。
        Returns:
            ``ToolResult.ok({artifact_id, status: deleted})``；找不到返回 failed。
        """
        logger.debug("[site_delete] 尝试删除 artifact=%s", artifact_id)
        artifact = await session.get(Artifact, artifact_id)
        if artifact is None:
            logger.warning("[site_delete] 未找到 artifact=%s", artifact_id)
            return ToolResult.fail(
                ErrorEnvelope(code="site_delete_not_found", category="not_found",
                              what="找不到指定版本", why=f"artifact_id={artifact_id}",
                              next="确认版本号", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        artifact.status = "deleted"
        await session.flush()
        logger.info("[site_delete] 已建立 tombstone artifact=%s", artifact_id)
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
        """资产导入(§9.2 asset_import, mid)—— 本环境只做校验与元数据 manifest。

        MIME 白名单消毒 + 尺寸上限(20MB)校验(当前均同步真实执行)；
        像素级转码(WebP/AVIF 压缩)依赖图像库,本环境未装,明确标记 ``transcode: deferred``，
        不静默成功。幂等键缺省用 fingerprint(文件名:尺寸 的 sha256 前 16 位)。

        Args:
            filename: 原始文件名(自动 basename 消毒)。
            mime: 资产 MIME,须为图片类型。
            size_bytes: 资产字节数,须 <= 20MB。
        Returns:
            ``ToolResult.ok({manifest, stored_as})``；类型/大小不合规返回 failed。
        """
        logger.debug("[asset_import] filename=%s mime=%s size=%d", filename, mime, size_bytes)
        if mime not in _ALLOWED_IMAGE_MIME:
            logger.warning("[asset_import] 不支持的 MIME: %s", mime)
            return ToolResult.fail(
                ErrorEnvelope(code="asset_import_bad_mime", category="validation",
                              what="不支持的 MIME 类型", why=mime,
                              next="仅允许图片类型", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        if size_bytes > 20_971_520:
            logger.warning("[asset_import] 超过 20MB: %d", size_bytes)
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
        logger.info("[asset_import] 通过校验 user=%s project=%s stored_as=%s", ctx.user_id, ctx.project_id, safe_name)
        return ToolResult.ok(
            {"manifest": manifest, "stored_as": safe_name},
            idempotency_key=idempotency_key or manifest["fingerprint"],
            metrics={"size_bytes": size_bytes},
        )


def _probe_preview_health(artifact: Artifact) -> tuple[bool, str]:
    """生产发布前的静态产物健康探针（返回 ``(是否健康, 失败原因)``）。

    三级校验，任一不过即拒绝切换 active（保留旧版本，符合 CRITICAL 回滚语义）：

    1. ``preview_path`` 非空，且拼出的绝对路径确实是一个**文件**（不是目录/不存在）；
    2. 文件字节数 > 0（防写了个空壳就上线）；
    3. 若 ``artifact.checksums["index.html"]`` 存在，则实际内容 sha256 必须匹配
       （防产物被外部改写 / 半截写入，保证「不可变版本」名副其实）。

    本地静态托管场景到此为止；接真实 host 时应在其后追加 HTTP 200 探针。
    """
    rel = (artifact.preview_path or "").strip()
    if not rel:
        return False, "artifact.preview_path 为空"
    path = Path(settings.artifact_dir) / rel
    if not path.is_file():
        return False, f"预览产物文件不存在: {rel}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return False, f"预览产物不可读: {str(exc)[:120]}"
    if not raw:
        return False, f"预览产物为空文件: {rel}"
    expected = (artifact.checksums or {}).get("index.html")
    if expected:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            return False, f"预览产物校验和不匹配 expected={expected[:12]}… actual={actual[:12]}…"
    return True, ""


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
        """生产发布(§9.2 site_deploy, CRITICAL, 需审批)。

        前置：必须由审批闸门将 ``approved`` 置 True；否则直接 failed（不静默发布）。
        流程：取得 Artifact → 健康检查(本环境简化为预览产物存在) → 落 Deployment(
        status=succeeded) → 把 project.active_deployment_id 指向它、project.status='active'。
        健康检查失败则保留旧 active，不切换(符合 CRITICAL 回滚语义)。

        Args:
            project/artifact_id: 目标项目与待发布版本。
            approved: 是否已通过审批闸门。
        Returns:
            ``ToolResult.ok({deployment_id, status, rolled_back})``；未审批/缺失/健康检查失败返回 failed。
        """
        logger.debug("[site_deploy] project=%s artifact=%s approved=%s", project.id, artifact_id, approved)
        if not approved:
            logger.warning("[site_deploy] 未审批,拒绝发布 project=%s", project.id)
            return ToolResult.fail(
                ErrorEnvelope(code="site_deploy_requires_approval", category="approval",
                              what="生产发布需先审批", why="approved=False",
                              next="通过审批流程后重试", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        artifact = await session.get(Artifact, artifact_id)
        if artifact is None:
            logger.warning("[site_deploy] 找不到 artifact=%s", artifact_id)
            return ToolResult.fail(
                ErrorEnvelope(code="site_deploy_no_artifact", category="not_found",
                              what="找不到待发布版本", why=f"artifact_id={artifact_id}",
                              next="确认版本", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        # 健康检查（本地静态产物）。
        # 🔴 此前写成 `bool(a.preview_path and Path(dir) / a.preview_path)` —— `Path / str`
        # 永远返回一个非空 Path 对象，恒为真，等于**健康检查从未生效**：产物文件被删、
        # 写了 0 字节、内容被改写，都会照样切 active，CRITICAL 动作失去最后一道防线。
        # 现改为真实三级探针：存在且是文件 → 非空 → 内容 sha256 与 checksums 一致。
        health_ok, health_why = _probe_preview_health(artifact)
        if not health_ok:
            logger.error("[site_deploy] 健康检查失败 project=%s artifact=%s why=%s",
                         project.id, artifact.id, health_why)
            return ToolResult.fail(
                ErrorEnvelope(code="site_deploy_health_fail", category="deploy",
                              what="健康检查失败", why=health_why,
                              next="保留旧 active，不切换", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key,
            )
        logger.info("[site_deploy] 健康检查通过 project=%s artifact=%s path=%s",
                    project.id, artifact.id, artifact.preview_path)
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
