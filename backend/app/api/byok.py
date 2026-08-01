"""BYOK(Bring Your Own Key) 用户级密钥管理 HTTP 层(规范 §14.3)。

安全约束(不可放宽):
- provider/model/base_url 必须命中 allowlist(app.security_byok),禁止任意 base_url ——
  否则等价于把用户凭证外送到攻击者控制的域名(SSRF + 凭证泄漏)。
- 明文 Key 只在请求生命周期内存在,落库前立刻 AES-256-GCM 加密;
  响应体永不回显明文,只给 fingerprint(sha256 前 16 位) 与掩码。
- BYOK 失效 / 限流一律返回结构化错误,绝不静默切回平台付费 Key。
- 密钥与非密钥元数据(model / base_url) 一起装进加密信封,存于既有
  user_model_keys.encrypted_key(LongText),不新增列 —— 避免 create_all 无法 ALTER 的坑。

AAD 绑定 user_id + key_id + provider + kek_version,因此 create 走两步:
先 insert 拿到自增 id,再用真实 id 回写密文(同一事务内原子完成)。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import transaction
from app.models import UserModelKey
from app.security import CurrentUser, get_current_user
from app.security_byok import (
    ALLOWED_PROVIDERS,
    ByokValidationError,
    fingerprint_key,
    get_byok_crypto,
    validate_provider_model_base_url,
)

router = APIRouter(prefix="/api/byok", tags=["byok"])

logger = logging.getLogger("app.api.byok")

_PLACEHOLDER = "pending"


# ---------------------------------------------------------------- 契约


class ByokCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str = Field(min_length=8, max_length=4096)


class ByokRotate(BaseModel):
    """轮换。

    - 不带 api_key: 仅做 KEK 版本重加密(信封轮换),明文不变。
    - 带 api_key: 真正的密钥轮换,同时刷新 fingerprint 并复位 status=active。
    """

    api_key: str | None = Field(default=None, min_length=8, max_length=4096)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=1, max_length=512)


# ---------------------------------------------------------------- 信封


def _pack(api_key: str, model: str, base_url: str) -> str:
    return json.dumps(
        {"api_key": api_key, "model": model, "base_url": base_url},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _unpack(plaintext: str) -> dict[str, str]:
    try:
        data = json.loads(plaintext)
    except json.JSONDecodeError:
        # 兼容历史裸密钥(未装信封)记录。
        return {"api_key": plaintext, "model": "", "base_url": ""}
    if not isinstance(data, dict):
        return {"api_key": plaintext, "model": "", "base_url": ""}
    return {
        "api_key": str(data.get("api_key", "")),
        "model": str(data.get("model", "")),
        "base_url": str(data.get("base_url", "")),
    }


def _mask(api_key: str) -> str:
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}{'*' * 8}{api_key[-4:]}"


def _kek_version(token: str) -> int:
    head = token.split(".", 1)[0]
    try:
        return int(head.lstrip("v"))
    except ValueError:
        return 0


def _view(record: UserModelKey, meta: dict[str, str] | None = None) -> dict[str, Any]:
    meta = meta or {}
    return {
        "id": record.id,
        "provider": record.provider,
        "model": meta.get("model") or None,
        "base_url": meta.get("base_url") or None,
        "fingerprint": record.fingerprint,
        "masked_key": _mask(meta["api_key"]) if meta.get("api_key") else None,
        "status": record.status,
        "kek_version": _kek_version(record.encrypted_key),
        "last_validated_at": record.last_validated_at,
    }


def _crypto():  # type: ignore[no-untyped-def]
    try:
        return get_byok_crypto()
    except ByokValidationError as exc:
        # 平台侧未配置 KEK —— 明确 503,不降级为「明文存储」也不切平台 Key。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "BYOK_UNAVAILABLE", "message": str(exc)},
        ) from exc


def _validate(provider: str, model: str, base_url: str) -> None:
    try:
        validate_provider_model_base_url(provider, model, base_url)
    except ByokValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BYOK_NOT_ALLOWED",
                "message": str(exc),
                "allowed_providers": sorted(ALLOWED_PROVIDERS),
            },
        ) from exc


def _decrypt_meta(crypto: Any, user_id: int, record: UserModelKey) -> dict[str, str]:
    try:
        plaintext = crypto.decrypt(user_id, record.id, record.provider, record.encrypted_key)
    except Exception:
        logger.warning("BYOK 解密失败 user=%s key=%s provider=%s", user_id, record.id, record.provider)
        return {}
    return _unpack(plaintext)


# ---------------------------------------------------------------- 端点


@router.get("")
async def list_keys(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    crypto = _crypto()
    async with transaction() as session:
        rows = list(
            (
                await session.execute(
                    select(UserModelKey)
                    .where(UserModelKey.user_id == user.id)
                    .order_by(UserModelKey.id.asc())
                )
            ).scalars()
        )
        items = [_view(r, _decrypt_meta(crypto, user.id, r)) for r in rows]
    return {"items": items, "allowed_providers": sorted(ALLOWED_PROVIDERS)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def upsert_key(
    payload: ByokCreate, user: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    provider = payload.provider.strip().lower()
    _validate(provider, payload.model.strip(), payload.base_url.strip())
    crypto = _crypto()
    envelope = _pack(payload.api_key, payload.model.strip(), payload.base_url.strip())
    fp = fingerprint_key(payload.api_key)

    async with transaction() as session:
        existing = (
            await session.execute(
                select(UserModelKey).where(
                    UserModelKey.user_id == user.id, UserModelKey.provider == provider
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.encrypted_key = crypto.encrypt(user.id, existing.id, provider, envelope)
            existing.fingerprint = fp
            existing.status = "active"
            existing.last_validated_at = None
            await session.flush()
            view = _view(existing, _unpack(envelope))
            created = False
        else:
            record = UserModelKey(
                user_id=user.id,
                provider=provider,
                encrypted_key=_PLACEHOLDER,
                fingerprint=fp,
                status="active",
            )
            session.add(record)
            await session.flush()  # 拿到自增 id,AAD 才能绑定
            record.encrypted_key = crypto.encrypt(user.id, record.id, provider, envelope)
            await session.flush()
            view = _view(record, _unpack(envelope))
            created = True
    logger.info("BYOK 写入 user=%s provider=%s created=%s fp=%s", user.id, provider, created, fp)
    return {"created": created, "key": view}


@router.delete("/{key_id}")
async def delete_key(
    key_id: int, user: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    async with transaction() as session:
        record = await session.get(UserModelKey, key_id)
        if record is None or record.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "BYOK_NOT_FOUND", "id": key_id},
            )
        provider = record.provider
        await session.delete(record)
    logger.info("BYOK 删除 user=%s provider=%s id=%s", user.id, provider, key_id)
    return {"deleted": True, "id": key_id, "provider": provider}


@router.post("/{key_id}/rotate")
async def rotate_key(
    key_id: int,
    payload: ByokRotate | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    payload = payload or ByokRotate()
    crypto = _crypto()
    async with transaction() as session:
        record = await session.get(UserModelKey, key_id)
        if record is None or record.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "BYOK_NOT_FOUND", "id": key_id},
            )
        provider = record.provider
        old_version = _kek_version(record.encrypted_key)
        current = _decrypt_meta(crypto, user.id, record)
        if not current:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "BYOK_UNDECRYPTABLE",
                    "message": "密文无法用当前/上一版本 KEK 解密,请重新提交密钥",
                    "id": key_id,
                },
            )
        api_key = payload.api_key or current.get("api_key", "")
        model = (payload.model or current.get("model") or "").strip()
        base_url = (payload.base_url or current.get("base_url") or "").strip()
        if model and base_url:
            _validate(provider, model, base_url)
        rotated_secret = payload.api_key is not None

        envelope = _pack(api_key, model, base_url)
        record.encrypted_key = crypto.encrypt(user.id, record.id, provider, envelope)
        record.fingerprint = fingerprint_key(api_key)
        record.status = "active"
        record.last_validated_at = datetime.now(UTC).isoformat()
        await session.flush()
        view = _view(record, _unpack(envelope))
    logger.info(
        "BYOK 轮换 user=%s provider=%s id=%s secret_rotated=%s kek %s->%s",
        user.id, provider, key_id, rotated_secret, old_version, view["kek_version"],
    )
    return {
        "rotated": True,
        "secret_rotated": rotated_secret,
        "kek_rotated": old_version != view["kek_version"],
        "key": view,
    }


__all__ = ["router"]
