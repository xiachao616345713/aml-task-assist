"""凭证与模型配置，本机浏览器可在业务登录前配置。"""
from urllib.parse import urlparse
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from task_assist.api.container import settings_store
from task_assist.api.local_settings import require_local_settings

router = APIRouter(prefix="/api/settings", dependencies=[Depends(require_local_settings)])


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_token: str | None = Field(default=None, max_length=4096)
    model_name: str = Field(min_length=1, max_length=255)
    model_base_url: str = Field(min_length=1, max_length=2048)
    model_api_key: str | None = Field(default=None, max_length=4096)
    model_timeout: float = Field(ge=5, le=600, allow_inf_nan=False)
    model_max_tokens: int = Field(ge=100, le=1000000)

    @field_validator("model_name", "business_token", "model_api_key")
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

    @field_validator("model_base_url")
    @classmethod
    def url(cls, value):
        value = value.strip().rstrip("/")
        url = urlparse(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("请输入有效的 HTTP(S) 模型地址")
        return value


@router.get("")
def get_settings():
    return settings_store.public()


@router.put("")
def save_settings(values: SettingsUpdate):
    return settings_store.save(values.model_dump(exclude_none=True))
