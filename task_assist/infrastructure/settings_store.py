"""本机调试凭证和模型设置；读取时不向浏览器返回凭证。"""
import os
import threading
from sqlalchemy import inspect
from sqlalchemy.schema import CreateColumn
from sqlalchemy.dialects.mysql import insert
from task_assist import config
from task_assist.infrastructure.models import AppSettings, as_dict


class SettingsStore:
    def __init__(self, database):
        self.database = database
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def ensure_schema(self):
        """兼容已有安装：首次使用设置时补齐本次新增的可空配置列。"""
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            engine = self.database.engine
            with engine.begin() as connection:
                mysql = engine.dialect.name == "mysql"
                if mysql and connection.exec_driver_sql("SELECT GET_LOCK('aml_settings_schema', 10)").scalar() != 1:
                    raise RuntimeError("配置表升级繁忙，请稍后重试")
                try:
                    AppSettings.__table__.create(connection, checkfirst=True)
                    existing = {column["name"] for column in inspect(connection).get_columns("app_settings")}
                    for name in ("aml_base_url", "scheduler_aml_token", "auto_submit_enabled",
                                 "auto_submit_interval_seconds", "max_auto_submit_per_run"):
                        if name not in existing:
                            definition = str(CreateColumn(AppSettings.__table__.c[name]).compile(dialect=engine.dialect))
                            connection.exec_driver_sql("ALTER TABLE app_settings ADD COLUMN " + definition)
                finally:
                    if mysql:
                        connection.exec_driver_sql("SELECT RELEASE_LOCK('aml_settings_schema')")
            self._schema_ready = True

    def get(self):
        self.ensure_schema()
        defaults = dict(business_token="", aml_base_url=config.AML_BASE_URL,
                    scheduler_aml_token=config.SCHEDULER_AML_TOKEN,
                    auto_submit_enabled=config.AUTO_SUBMIT_ENABLED,
                    auto_submit_interval_seconds=config.AUTO_SUBMIT_INTERVAL_SECONDS,
                    max_auto_submit_per_run=config.MAX_AUTO_SUBMIT_PER_RUN,
                    model_name=config.MODEL_NAME, model_base_url=config.MODEL_BASE_URL,
                    model_api_key=os.getenv("OPENAI_API_KEY", ""), model_timeout=config.MODEL_TIMEOUT_SECONDS,
                    model_max_tokens=config.MODEL_MAX_OUTPUT_TOKENS)
        with self.database.orm_session() as db:
            obj = db.get(AppSettings, "default")
            if obj:
                defaults.update({k: v for k, v in as_dict(obj).items() if v is not None})
        return defaults

    def public(self):
        settings = self.get()
        return {**{k: v for k, v in settings.items() if k not in {"id", "business_token", "model_api_key", "scheduler_aml_token"}},
                "scheduler_aml_token_configured": bool(settings["scheduler_aml_token"]),
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
        from task_assist.infrastructure.chat_model import ObservableChatOpenAI
        values = self.get()
        # stream_usage 让流式响应末片携带 token 用量，看板与 Trace 才能统计流式轮次。
        return ObservableChatOpenAI(model=values["model_name"], base_url=values["model_base_url"],
            api_key=values["model_api_key"], timeout=values["model_timeout"],
            max_tokens=values["model_max_tokens"], temperature=0, max_retries=0,
            stream_usage=True)
