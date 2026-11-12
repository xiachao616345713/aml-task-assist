"""本机调试凭证和模型设置；读取时不向浏览器返回凭证。"""
import os
from sqlalchemy.dialects.mysql import insert
from task_assist import config
from task_assist.infrastructure.models import AppSettings, as_dict


class SettingsStore:
    def __init__(self, database):
        self.database = database

    def get(self):
        with self.database.orm_session() as db:
            obj = db.get(AppSettings, "default")
            if obj:
                return as_dict(obj)
        return dict(business_token="", model_name=config.MODEL_NAME, model_base_url=config.MODEL_BASE_URL,
                    model_api_key=os.getenv("OPENAI_API_KEY", ""), model_timeout=config.MODEL_TIMEOUT_SECONDS,
                    model_max_tokens=config.MODEL_MAX_OUTPUT_TOKENS)

    def public(self):
        settings = self.get()
        return {**{k: v for k, v in settings.items() if k not in {"id", "business_token", "model_api_key"}},
                "business_token_configured": bool(settings["business_token"]),
                "model_api_key_configured": bool(settings["model_api_key"])}

    def save(self, values):
        # 单条原子 upsert；未提交的密码字段保持原值，空字符串表示清除。
        defaults = self.get()
        defaults.pop("id", None)
        defaults.update(values)
        with self.database.orm_session() as db:
            stmt = insert(AppSettings).values(id="default", **defaults)
            db.execute(stmt.on_duplicate_key_update(**{k: stmt.inserted[k] for k in values}))
        return self.public()

    def model(self):
        from langchain_openai import ChatOpenAI
        values = self.get()
        return ChatOpenAI(model=values["model_name"], base_url=values["model_base_url"],
            api_key=values["model_api_key"], timeout=values["model_timeout"],
            max_tokens=values["model_max_tokens"], temperature=0, max_retries=0)
