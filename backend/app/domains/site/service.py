from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.turn_context import TurnContext
from app.models import Artifact, Project


class SiteService:
    async def create_or_edit(self, session: AsyncSession, context: TurnContext) -> tuple[Artifact, str]:
        project = await session.get(Project, context.session.project_id)
        if project is None or project.user_id != context.user.user_id:
            raise ValueError("目标项目不存在或无权访问")
        max_version = await session.scalar(select(func.max(Artifact.version)).where(Artifact.project_id == project.id))
        version = int(max_version or 0) + 1
        html = self._render(context.clean_message, project.name)
        manifest = {"index.html": {"sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(), "bytes": len(html.encode("utf-8"))}}
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        root = Path(settings.artifact_dir) / "previews" / str(context.user.user_id) / str(project.id) / f"v{version}"
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / "index.html.tmp"
        target = root / "index.html"
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            file.write(html)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(target)
        artifact = Artifact(
            project_id=project.id,
            conversation_id=context.session.conversation_id,
            parent_artifact_id=project.head_artifact_id,
            version=version,
            site_spec_revision=project.lock_version,
            site_spec_hash=hashlib.sha256(json.dumps(project.site_spec, sort_keys=True).encode("utf-8")).hexdigest(),
            manifest=manifest,
            manifest_digest=digest,
            checksums={"index.html": manifest["index.html"]["sha256"]},
            vendor_manifest_version="seed-premium-v1",
            capability_manifest={},
            status="preview_ready",
            preview_path=str(target.relative_to(Path(settings.artifact_dir))),
            trace_id=context.trace_id,
        )
        session.add(artifact)
        await session.flush()
        project.head_artifact_id = artifact.id
        project.status = "active"
        project.lock_version += 1
        return artifact, f"已生成网站版本 v{version}，预览产物已就绪。"

    @staticmethod
    def _render(requirement: str, project_name: str) -> str:
        safe_title = project_name.replace("<", "&lt;").replace(">", "&gt;")
        safe_requirement = requirement.replace("<", "&lt;").replace(">", "&gt;")
        return f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{safe_title}</title><style>body{{font-family:system-ui,sans-serif;margin:0;background:#f7f7f3;color:#181815}}main{{max-width:860px;margin:0 auto;padding:96px 24px}}h1{{font-size:clamp(2rem,8vw,5rem);letter-spacing:-.06em}}p{{font-size:1.1rem;line-height:1.7}}</style></head><body><main><p>SeedAI 新版预览</p><h1>{safe_title}</h1><p>{safe_requirement}</p></main></body></html>"""


site_service = SiteService()
