"""凭证与模型配置，本机浏览器可在业务登录前配置。"""
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from task_assist.api.container import settings_store
from task_assist.api.local_settings import require_local_settings

def require_settings_store():
    try:
        settings_store.ensure_schema()
    except SQLAlchemyError:
        raise HTTPException(503, "配置数据库不可用；请检查数据库连接及配置表升级权限，或执行业务配置迁移脚本") from None


router = APIRouter(prefix="/api/settings", dependencies=[Depends(require_local_settings), Depends(require_settings_store)])


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_token: str | None = Field(default=None, max_length=4096)
    aml_base_url: str | None = Field(default=None, max_length=2048)
    scheduler_aml_token: str | None = Field(default=None, max_length=4096)
    auto_submit_enabled: bool | None = None
    auto_submit_interval_seconds: int | None = Field(default=None, ge=60, le=604800)
    max_auto_submit_per_run: int | None = Field(default=None, ge=1, le=100)
    model_name: str | None = Field(default=None, min_length=1, max_length=255)
    model_base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    model_api_key: str | None = Field(default=None, max_length=4096)
    model_timeout: float | None = Field(default=None, ge=5, le=600, allow_inf_nan=False)
    model_max_tokens: int | None = Field(default=None, ge=100, le=1000000)

    @field_validator("model_name", "business_token", "model_api_key", "scheduler_aml_token")
    @classmethod
    def clean(cls, value, info):
        if value is None:
            return None
        value = value.strip()
        if info.field_name == "model_name" and not value:
            raise ValueError("模型名称不能为空")
        if any(ord(c) < 32 for c in value):
            raise ValueError("不能包含控制字符")
        return value

    @field_validator("model_base_url", "aml_base_url")
    @classmethod
    def url(cls, value, info):
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value and info.field_name == "aml_base_url":
            return ""
        url = urlparse(value)
        if (any(ord(c) <= 32 for c in value) or url.scheme not in {"http", "https"}
                or not url.hostname or url.username or url.password or url.query or url.fragment):
            raise ValueError("请输入有效的 HTTP(S) 服务地址")
        return value


@router.get("")
def get_settings():
    return settings_store.public()


@router.put("")
def save_settings(values: SettingsUpdate):
    updates = values.model_dump(exclude_none=True)
    effective = {**settings_store.get(), **updates}
    if effective["auto_submit_enabled"] and (not effective["aml_base_url"] or not effective["scheduler_aml_token"]):
        raise HTTPException(422, "启用自动提交需要配置 AML 服务地址和调度专用 Token")
    return settings_store.save(updates) if updates else settings_store.public()
