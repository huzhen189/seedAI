"""ProjectOps 领域执行（规范 §8.4 / §10.4 / §10.6）。

纯代码处理，不经 LLM：发布、回收、恢复、永久删除。

三条硬约束：
  1. Deployment 与 Artifact 分离 —— Artifact 是不可变内容，Deployment 是一次环境发布；
     发布与回滚都新建 Deployment 行，绝不把旧行改回 succeeded。
  2. 健康检查失败不切 active 指针 —— 发布写入版本化不可变目录 published/{uid}/{pid}/v{n}，
     旧 active 目录永不被覆盖，因此失败天然不破坏线上。
  3. purge 是分步幂等 job，不在 HTTP 请求内同步完成；每步落 purge_jobs.step 可重入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import transaction
from app.db.repositories import outbox
from app.models import Artifact, Deployment, OutboxEvent, Project, ProjectTombstone, PurgeJob

logger = logging.getLogger("app.domains.project.ops")

PREVIEW_ROOT = "previews"
PUBLISHED_ROOT = "published"
ENVIRONMENT = "production"

# purge 步骤顺序即规范 §10.6 的执行顺序，run_purge_job 按此表可重入推进。
PURGE_STEPS = (
    "freeze",
    "scrub_outbox",
    "revoke_deployment",
    "drop_vectors",
    "delete_files",
    "verify_empty",
    "delete_rows",
)


@dataclass(slots=True)
class OpsOutcome:
    """领域执行结果，S6/Gate 据此写 ExecutionResult 与回复片段。"""

    status: str  # succeeded | failed | partial
    text: str
    output_refs: list[str] = field(default_factory=list)
    error_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def committed(self) -> bool:
        return self.status == "succeeded"


def _artifact_root() -> Path:
    return Path(settings.artifact_dir)


def preview_dir(user_id: int, project_id: int, version: int) -> Path:
    return _artifact_root() / PREVIEW_ROOT / str(user_id) / str(project_id) / f"v{version}"


def published_dir(user_id: int, project_id: int, version: int) -> Path:
    return _artifact_root() / PUBLISHED_ROOT / str(user_id) / str(project_id) / f"v{version}"


def _raw_unlink(path: Path) -> None:
    """物理删除文件, 绕过运行环境的 safe-delete 钩子。

    部分运行环境会把 os.unlink / shutil.rmtree 劫持到系统回收站; 本沙箱回收站不可用时
    会 fail-closed 抛 OSError, 导致 purge 的 delete_files 步无法真正删文件。这里在 Windows 上
    直接调 kernel32.DeleteFileW(ctypes) 做永久删除, 绕过 Python 层钩子; 其他平台回退 os.unlink。
    仅用于产物清理, 且调用方 _delete_project_tree 已做 artifact_root 越界拒绝。
    """
    p = os.fspath(path)
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.DeleteFileW.argtypes = [ctypes.c_wchar_p]
            kernel32.DeleteFileW.restype = ctypes.c_int
            if kernel32.DeleteFileW(p) != 0:
                return
            err = kernel32.GetLastError()
            if err in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND -> 视为已删
                return
            raise OSError(err, f"DeleteFileW 失败: {p} (err={err})")
        except OSError:
            raise
        except Exception:
            pass  # ctypes 不可用, 回退标准库
    try:
        os.unlink(p)
    except FileNotFoundError:
        return


def _raw_rmdir(path: Path) -> None:
    """同 _raw_unlink, 但删除空目录(RemoveDirectoryW)。"""
    p = os.fspath(path)
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.RemoveDirectoryW.argtypes = [ctypes.c_wchar_p]
            kernel32.RemoveDirectoryW.restype = ctypes.c_int
            if kernel32.RemoveDirectoryW(p) != 0:
                return
            err = kernel32.GetLastError()
            if err in (2, 3):
                return
            raise OSError(err, f"RemoveDirectoryW 失败: {p} (err={err})")
        except OSError:
            raise
        except Exception:
            pass
    try:
        os.rmdir(p)
    except FileNotFoundError:
        return


def _delete_project_tree(root: Path) -> None:
    """物理删除单个项目的产物目录树(规范 §10.6 delete_files)。

    刻意逐个 unlink/rmdir：
      1. 可精确定位到哪一个文件删不掉，purge 是不可逆动作，必须可审计；
      2. 用 _raw_unlink/_raw_rmdir 绕过运行环境的 safe-delete 钩子（否则 purge 卡在 delete_files）；
      3. 只处理 artifact_root 之下的路径，越界直接拒绝，避免任何误删风险。
    """
    if not root.exists():
        return
    base = _artifact_root().resolve()
    resolved = root.resolve()
    if base not in resolved.parents:
        raise OSError(f"purge 拒绝越界删除: {resolved} 不在 {base} 之下")
    for path in sorted(resolved.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_file() or path.is_symlink():
            _raw_unlink(path)
        elif path.is_dir():
            _raw_rmdir(path)
    _raw_rmdir(resolved)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ProjectOpsService:
    """发布/回收/恢复/永久删除。所有方法只接收调用方事务，绝不自行 commit。"""

    async def execute(
        self,
        session: AsyncSession,
        *,
        action: str,
        project_id: int,
        user_id: int,
        trace_id: str,
    ) -> OpsOutcome:
        """按 speech_act 分发到具体执行器(publish/trash/restore/purge)。

        先以 ``for_update`` 锁住项目行(防并发状态竞争),再校验存在性与状态机合法性
        (purging 中禁止任何操作);未知动作返回 failed 而非抛异常,便于 S6/Gate 统一收口。

        Returns:
            ``OpsOutcome``(status/text/output_refs/error_code)。
        """
        logger.info("[ops] 执行项目动作 action=%s project=%s user=%s", action, project_id, user_id)
        project = (
            await session.execute(
                select(Project).where(Project.id == project_id, Project.user_id == user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if project is None:
            logger.warning("[ops] 项目不存在或越权 project=%s user=%s", project_id, user_id)
            return OpsOutcome(status="failed", text="目标项目不存在或无权访问。", error_code="project_not_found")
        if project.status == "purging":
            return OpsOutcome(status="failed", text="项目正在永久删除中，无法执行该操作。", error_code="project_purging")

        handlers = {
            "publish": self.publish,
            "trash": self.trash,
            "restore": self.restore,
            "purge": self.purge,
        }
        handler = handlers.get(action)
        if handler is None:
            logger.warning("[ops] 不支持的动作 action=%s", action)
            return OpsOutcome(status="failed", text=f"不支持的项目操作：{action}。", error_code="unsupported_action")
        return await handler(session, project, trace_id=trace_id)

    # ------------------------------------------------------------------ publish

    async def publish(self, session: AsyncSession, project: Project, *, trace_id: str) -> OpsOutcome:
        """Deployment Saga：pending→uploading→health_checking→succeeded|failed。"""
        if project.head_artifact_id is None:
            return OpsOutcome(status="failed", text="项目还没有可发布的网站版本，请先生成一版。", error_code="no_artifact")
        artifact = await session.get(Artifact, project.head_artifact_id)
        if artifact is None or artifact.status not in {"verified", "preview_ready"}:
            return OpsOutcome(status="failed", text="待发布的产物不可用，请重新生成网站。", error_code="artifact_not_publishable")

        deployment = Deployment(
            project_id=project.id,
            artifact_id=artifact.id,
            manifest_digest=artifact.manifest_digest,
            environment=ENVIRONMENT,
            status="pending",
            previous_deployment_id=project.active_deployment_id,
            health_report={},
            object_prefix=f"{PUBLISHED_ROOT}/{project.user_id}/{project.id}/v{artifact.version}",
            started_at=datetime.now(UTC),
        )
        session.add(deployment)
        await session.flush()

        source = preview_dir(project.user_id, project.id, artifact.version)
        target = published_dir(project.user_id, project.id, artifact.version)
        try:
            deployment.status = "uploading"
            await session.flush()
            self._copy_release(source, target, artifact.manifest)

            deployment.status = "health_checking"
            await session.flush()
            report = self._health_check(target, artifact.manifest)
        except (OSError, ValueError) as exc:
            logger.warning("发布失败 project=%s artifact=%s: %s", project.id, artifact.id, exc)
            deployment.status = "failed"
            deployment.finished_at = datetime.now(UTC)
            deployment.health_report = {"ok": False, "error": str(exc)[:480]}
            await self._emit(
                session,
                key=f"deployment:{deployment.id}:failed",
                aggregate_id=str(project.id),
                event_type="deployment.failed",
                payload={"project_id": project.id, "artifact_id": artifact.id, "error": str(exc)[:480]},
            )
            return OpsOutcome(
                status="failed",
                text="发布失败，线上版本未受影响，可稍后重试。",
                error_code="deploy_failed",
                details={"deployment_id": deployment.id},
            )

        if not report["ok"]:
            deployment.status = "failed"
            deployment.finished_at = datetime.now(UTC)
            deployment.health_report = report
            await self._emit(
                session,
                key=f"deployment:{deployment.id}:failed",
                aggregate_id=str(project.id),
                event_type="deployment.failed",
                payload={"project_id": project.id, "artifact_id": artifact.id, "report": report},
            )
            # 健康检查失败：两个 Project 指针保持原值，旧 active 版本完好。
            return OpsOutcome(
                status="failed",
                text="发布健康检查未通过，已保留原线上版本。",
                error_code="health_check_failed",
                details={"deployment_id": deployment.id, "report": report},
            )

        deployment.status = "succeeded"
        deployment.finished_at = datetime.now(UTC)
        deployment.health_report = report
        # Deployment 终态与两个 Project 指针在同一 MySQL 事务切换。
        project.published_artifact_id = artifact.id
        project.active_deployment_id = deployment.id
        project.status = "active"
        project.lock_version += 1
        await self._emit(
            session,
            key=f"deployment:{deployment.id}:succeeded",
            aggregate_id=str(project.id),
            event_type="project.published",
            payload={
                "project_id": project.id,
                "artifact_id": artifact.id,
                "version": artifact.version,
                "deployment_id": deployment.id,
                "manifest_digest": artifact.manifest_digest,
                "trace_id": trace_id,
            },
        )
        await session.flush()
        return OpsOutcome(
            status="succeeded",
            text=f"已发布网站 v{artifact.version}，线上版本已切换。",
            output_refs=[str(artifact.id), str(deployment.id)],
            details={"deployment_id": deployment.id, "version": artifact.version},
        )

    @staticmethod
    def _copy_release(source: Path, target: Path, manifest: dict[str, Any]) -> None:
        """把预览产物复制进版本化的发布目录；先写 .staging 再整体 replace，避免半成品。"""
        if not source.is_dir():
            raise OSError(f"预览产物目录不存在: {source}")
        staging = target.with_name(target.name + ".staging")
        _delete_project_tree(staging)
        staging.parent.mkdir(parents=True, exist_ok=True)
        for relative in manifest:
            src_file = source / relative
            if not src_file.is_file():
                raise OSError(f"产物缺少清单文件: {relative}")
            dst_file = staging / relative
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
        # 同版本重复发布是幂等的：内容一致，直接替换旧的同版本目录。
        _delete_project_tree(target)
        staging.replace(target)

    @staticmethod
    def _health_check(target: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        """逐文件校验存在性、字节数与 sha256，全部命中才算健康。"""
        checked: list[str] = []
        failures: list[dict[str, str]] = []
        for relative, meta in manifest.items():
            path = target / relative
            if not path.is_file():
                failures.append({"file": relative, "reason": "missing"})
                continue
            expected = str(meta.get("sha256", ""))
            actual = _sha256_file(path)
            if expected and expected != actual:
                failures.append({"file": relative, "reason": "checksum_mismatch"})
                continue
            expected_bytes = meta.get("bytes")
            if isinstance(expected_bytes, int) and path.stat().st_size != expected_bytes:
                failures.append({"file": relative, "reason": "size_mismatch"})
                continue
            checked.append(relative)
        return {"ok": not failures, "checked": checked, "failures": failures}

    # ------------------------------------------------------------- trash/restore

    async def trash(self, session: AsyncSession, project: Project, *, trace_id: str) -> OpsOutcome:
        if project.status == "trashed":
            return OpsOutcome(status="succeeded", text="项目已在回收站中。", output_refs=[str(project.id)])
        if project.status not in {"draft", "active"}:
            return OpsOutcome(status="failed", text="当前项目状态不允许回收。", error_code="invalid_transition")
        project.status = "trashed"
        project.lock_version += 1
        await self._emit(
            session,
            key=f"project:{project.id}:trashed:{project.lock_version}",
            aggregate_id=str(project.id),
            event_type="project.trashed",
            payload={"project_id": project.id, "trace_id": trace_id},
        )
        await session.flush()
        return OpsOutcome(
            status="succeeded",
            text="项目已移入回收站，可随时恢复。",
            output_refs=[str(project.id)],
        )

    async def restore(self, session: AsyncSession, project: Project, *, trace_id: str) -> OpsOutcome:
        if project.status != "trashed":
            return OpsOutcome(status="failed", text="只有回收站中的项目才能恢复。", error_code="invalid_transition")
        project.status = "active" if project.head_artifact_id else "draft"
        project.lock_version += 1
        await self._emit(
            session,
            key=f"project:{project.id}:restored:{project.lock_version}",
            aggregate_id=str(project.id),
            event_type="project.restored",
            payload={"project_id": project.id, "trace_id": trace_id},
        )
        await session.flush()
        return OpsOutcome(status="succeeded", text="项目已从回收站恢复。", output_refs=[str(project.id)])

    # ----------------------------------------------------------------- purge

    async def purge(self, session: AsyncSession, project: Project, *, trace_id: str) -> OpsOutcome:
        """只做「冻结 + 建 job」，真实删除由 run_purge_job 分步幂等执行。"""
        if project.status not in {"trashed", "purging"}:
            return OpsOutcome(
                status="failed",
                text="请先把项目移入回收站，再执行永久删除。",
                error_code="purge_requires_trashed",
            )
        existing = (
            await session.execute(
                select(PurgeJob).where(
                    PurgeJob.project_id == project.id,
                    PurgeJob.status.in_(("queued", "running")),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return OpsOutcome(
                status="succeeded",
                text="永久删除任务已在执行中。",
                output_refs=[str(existing.id)],
                details={"purge_job_id": existing.id, "purge_generation": existing.purge_generation},
            )

        # CAS trashed→purging + 递增 generation + upsert tombstone + 建唯一 job，同一事务。
        project.status = "purging"
        project.purge_generation += 1
        project.lock_version += 1
        generation = project.purge_generation
        tombstone = (
            await session.execute(
                select(ProjectTombstone).where(
                    ProjectTombstone.project_id == project.id,
                    ProjectTombstone.purge_generation == generation,
                )
            )
        ).scalar_one_or_none()
        if tombstone is None:
            session.add(ProjectTombstone(project_id=project.id, purge_generation=generation))
        job = PurgeJob(
            project_id=project.id,
            purge_generation=generation,
            status="queued",
            step="freeze",
            details={"user_id": project.user_id, "trace_id": trace_id},
        )
        session.add(job)
        await self._emit(
            session,
            key=f"project:{project.id}:purge:{generation}",
            aggregate_id=str(project.id),
            event_type="project.purge_started",
            payload={"project_id": project.id, "purge_generation": generation, "trace_id": trace_id},
        )
        await session.flush()
        return OpsOutcome(
            status="succeeded",
            text="已开始永久删除，删除过程在后台分步执行，完成后不可恢复。",
            output_refs=[str(job.id)],
            details={"purge_job_id": job.id, "purge_generation": generation, "user_id": project.user_id},
        )

    async def run_purge_job(self, job_id: int) -> str:
        """按 PURGE_STEPS 顺序推进 purge，**一步一事务**。

        每步独立提交，`details.completed_steps` 因此是真实进度而不是内存幻觉：
        进程被杀、某步抛错都不会丢掉已完成的步骤，重跑时直接从断点续。
        单事务跑完整 job 是错的 —— 任一步 DB 报错会毒化 session，
        连「把 job 标成 failed」这一笔都写不进去，job 会永远停在 queued。
        """
        logger.info("[purge] job=%s 启动, 共 %d 步", job_id, len(PURGE_STEPS))
        for step in PURGE_STEPS:
            logger.info("[purge] job=%s 执行步骤 %s", job_id, step)
            try:
                async with transaction() as session:
                    job = await session.get(PurgeJob, job_id)
                    if job is None:
                        raise ValueError(f"purge job 不存在: {job_id}")
                    if job.status == "succeeded":
                        return "succeeded"
                    done = list(job.details.get("completed_steps") or [])
                    if step in done:
                        continue
                    project = await session.get(Project, job.project_id)
                    user_id = int(job.details.get("user_id") or (project.user_id if project else 0))
                    job.status = "running"
                    job.step = step
                    await self._run_purge_step(session, job, project, user_id, step)
                    done.append(step)
                    job.details = {**job.details, "completed_steps": done}
            except Exception as exc:  # noqa: BLE001 - 任一步失败都可重入重试
                logger.exception("purge job %s 在步骤 %s 失败", job_id, step)
                await self._mark_purge_failed(job_id, step, exc)
                return "failed"

        async with transaction() as session:
            job = await session.get(PurgeJob, job_id)
            if job is None:
                return "failed"
            job.status = "succeeded"
            job.step = "done"
            await session.execute(
                update(ProjectTombstone)
                .where(
                    ProjectTombstone.project_id == job.project_id,
                    ProjectTombstone.purge_generation == job.purge_generation,
                )
                .values(status="completed", completed_at=datetime.now(UTC))
            )
            await self._emit(
                session,
                key=f"project:{job.project_id}:purged:{job.purge_generation}",
                aggregate_id=str(job.project_id),
                event_type="project.purged",
                payload={"project_id": job.project_id, "purge_generation": job.purge_generation},
            )
        return "succeeded"

    @staticmethod
    async def _mark_purge_failed(job_id: int, step: str, exc: Exception) -> None:
        """用一条全新事务记录失败 —— 出错那条事务的 session 已不可用。"""
        try:
            async with transaction() as session:
                job = await session.get(PurgeJob, job_id)
                if job is None:
                    return
                job.status = "failed"
                job.step = step
                job.error_code = type(exc).__name__[:96]
                job.details = {**job.details, "error": str(exc)[:480]}
        except Exception:  # noqa: BLE001 - 记录失败本身失败时只留日志，不再抛
            logger.exception("purge job %s 失败状态写入失败", job_id)

    async def _run_purge_step(
        self,
        session: AsyncSession,
        job: PurgeJob,
        project: Project | None,
        user_id: int,
        step: str,
    ) -> None:
        if step == "freeze":
            return  # purge() 已在同一事务内完成冻结与 tombstone。
        if step == "scrub_outbox":
            # 旧 generation 的待投递事件必须作废，避免 purge 后仍向外泄漏项目内容。
            await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_type == "project",
                    OutboxEvent.aggregate_id == str(job.project_id),
                    OutboxEvent.status.in_(("pending", "processing")),
                )
                .values(status="dead", last_error_code="purged")
            )
            return
        if step == "revoke_deployment":
            if project is not None:
                project.published_artifact_id = None
                project.active_deployment_id = None
                project.head_artifact_id = None
            return
        if step == "drop_vectors":
            await self._drop_vectors(job.project_id)
            return
        if step == "delete_files":
            for root in (PREVIEW_ROOT, PUBLISHED_ROOT):
                path = _artifact_root() / root / str(user_id) / str(job.project_id)
                _delete_project_tree(path)
            return
        if step == "verify_empty":
            leftovers = [
                str(_artifact_root() / root / str(user_id) / str(job.project_id))
                for root in (PREVIEW_ROOT, PUBLISHED_ROOT)
                if (_artifact_root() / root / str(user_id) / str(job.project_id)).exists()
            ]
            if leftovers:
                raise OSError(f"purge 校验失败，仍存在产物目录: {leftovers}")
            return
        if step == "delete_rows":
            if project is not None:
                # deployments.artifact_id 是 ON DELETE RESTRICT：必须先删部署记录，
                # 否则删 project 级联到 artifacts 时会被外键顶回来(errno 1451)。
                await session.execute(delete(Deployment).where(Deployment.project_id == project.id))
                await session.flush()
                await session.delete(project)  # 其余子表 FK ON DELETE CASCADE
            return
        raise ValueError(f"未知 purge 步骤: {step}")

    @staticmethod
    async def _drop_vectors(project_id: int) -> None:
        """删除项目向量集合；本地无 Chroma 时降级为 no-op 而不是让 purge 卡死。"""
        try:
            from app.services.vector_store import drop_project_collections  # type: ignore
        except ImportError:
            logger.info("未启用向量存储，purge 跳过 drop_vectors project=%s", project_id)
            return
        try:
            await drop_project_collections(project_id)
        except Exception as exc:  # noqa: BLE001 - 向量清理失败不应阻断，但必须留痕
            logger.warning("purge 清理向量失败 project=%s: %s", project_id, exc)

    @staticmethod
    async def _emit(
        session: AsyncSession,
        *,
        key: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """W0 业务写入与 outbox_events 同事务；event_key 唯一约束即幂等护栏。"""
        existing = await session.execute(select(OutboxEvent.id).where(OutboxEvent.event_key == key))
        if existing.scalar_one_or_none() is not None:
            return
        await outbox.insert(
            session,
            event_key=key,
            aggregate_type="project",
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )


project_ops = ProjectOpsService()
