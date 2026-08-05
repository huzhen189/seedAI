from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ToolCall

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.tool_calls")


class ToolCallsRepo(BaseRepo[ToolCall]):
    model = ToolCall

    async def by_message(
        self, session: AsyncSession, message_id: int, *, limit: int = 100
    ) -> list[ToolCall]:
        if message_id <= 0:
            raise ValueError("message_id 必须为正整数")
        return await self.list(session, message_id=message_id, limit=limit)

    async def by_idempotency_key(
        self, session: AsyncSession, idempotency_key: str
    ) -> ToolCall | None:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key 不得为空")
        return await self.get_by(session, idempotency_key=idempotency_key)

    async def upsert_by_operation_key(
        self,
        session: AsyncSession,
        *,
        tool_name: str,
        operation_key: str,
        args_hash: str,
        turn_id: str,
        fencing_token: str,
        task_id: int | None = None,
        status: str = "running",
        result_ref: str | None = None,
        error_code: str | None = None,
    ) -> ToolCall:
        """W0 账本的 MID 式幂等写入：存在则更新终态，不存在则插入 running。

        - 幂等工具(``idempotency=True``)的 ``operation_key`` 在多次调用间保持稳定，
          因此同一动作只会落一条账本记录（先写 running，执行后改终态）。
        - 非幂等工具每次调用 ``operation_key`` 带随机后缀，天然不冲突。
        - 不依赖乐观锁版本列（``ToolCall`` 无 ``version`` 字段），``expected_version=None``
          的 ``update`` 直接按主键覆盖。
        """
        if not operation_key.strip():
            raise ValueError("operation_key 不得为空")
        existing = await self.get_by(session, operation_key=operation_key)
        if existing is not None:
            return await self.update(
                session, existing,
                status=status, result_ref=result_ref, error_code=error_code,
            )
        return await self.insert(
            session,
            tool_name=tool_name,
            operation_key=operation_key,
            args_hash=args_hash,
            turn_id=turn_id,
            fencing_token=fencing_token,
            task_id=task_id,
            status=status,
            result_ref=result_ref,
            error_code=error_code,
        )
