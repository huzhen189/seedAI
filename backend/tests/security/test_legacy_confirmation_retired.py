from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agent.core.orchestrator import Orchestrator
from app.agent.roles.orchestrator import RoleOrchestrator
from app.agent.skills.agent_delete import run_delete
from app.projects import delete_all_artifacts, delete_single_file


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_legacy_confirmation_query_values_are_not_forwarded_to_worker() -> None:
    proxy_source = (PROJECT_ROOT / "backend" / "app" / "proxy.py").read_text(encoding="utf-8")
    queue_source = (PROJECT_ROOT / "backend" / "app" / "agent" / "core" / "queue.py").read_text(
        encoding="utf-8"
    )

    assert 'payload["confirmed"] = True' not in proxy_source
    assert 'payload["confirmed_subtasks"] =' not in proxy_source
    assert 'confirmed = bool(job.get("confirmed", False))' not in queue_source
    assert 'confirmed_subtasks = set(job.get("confirmed_subtasks") or [])' not in queue_source


def test_legacy_subtask_confirmation_is_not_an_execution_api() -> None:
    assert "confirmed_subtasks" not in inspect.signature(Orchestrator.execute).parameters
    assert "confirmed_subtasks" not in inspect.signature(RoleOrchestrator._run_one).parameters


def test_legacy_delete_skill_rejects_even_a_forged_confirmed_flag() -> None:
    async def scenario() -> None:
        events = [
            event
            async for event in run_delete(
                "deepseek",
                [{"role": "user", "content": "删除所有产物"}],
                trace_id="security-test",
                site_generated=True,
                confirmed=True,
            )
        ]
        assert events == [
            {
                "event": "approval_required",
                "data": {
                    "code": "APPROVAL_GATE_REQUIRED",
                    "risk_level": "high",
                    "target": "all",
                    "message": "删除操作已迁移到安全审批工作流；当前版本不接受旧 confirmed 重试。",
                },
            }
        ]

    asyncio.run(scenario())


def test_legacy_artifact_delete_routes_require_future_approval_gate() -> None:
    async def scenario() -> None:
        with pytest.raises(HTTPException) as all_exc:
            await delete_all_artifacts(project_id=1, user=object(), db=object())
        assert all_exc.value.status_code == 409
        assert all_exc.value.detail["code"] == "APPROVAL_GATE_REQUIRED"

        with pytest.raises(HTTPException) as file_exc:
            await delete_single_file(project_id=1, name="index.html", user=object(), db=object())
        assert file_exc.value.status_code == 409
        assert file_exc.value.detail["code"] == "APPROVAL_GATE_REQUIRED"

    asyncio.run(scenario())
