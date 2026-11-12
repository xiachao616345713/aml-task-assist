"""MySQL 8 连接、事务和跨实例命名锁。表结构由 sql/mysql8-init.sql 管理。"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from urllib.parse import unquote, urlparse

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from task_assist import config
from task_assist.core.errors import AgentBusy


class MySQLSession:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql: str, args=()):
        result = self.connection.exec_driver_sql(sql, tuple(args))
        return result.mappings() if result.returns_rows else result


class MySQLDatabase:
    def __init__(self, dsn: str = config.MYSQL_DSN):
        parsed = urlparse(dsn)
        if (parsed.scheme != "mysql" or not parsed.hostname or not parsed.path.strip("/")
                or not parsed.username):
            raise ValueError("MYSQL_DSN 必须是 mysql://user:password@host:3306/database")
        self.options = {
            "host": parsed.hostname,
            "port": parsed.port or 3306,
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "database": unquote(parsed.path.lstrip("/")),
            "charset": "utf8mb4",
            "connect_timeout": 5,
            "read_timeout": 35,
            "write_timeout": 35,
            "autocommit": False,
        }

        self.engine = create_engine(
            URL.create("mysql+pymysql", username=self.options["user"],
                       password=self.options["password"], host=self.options["host"],
                       port=self.options["port"], database=self.options["database"],
                       query={"charset": "utf8mb4"}),
            hide_parameters=True, pool_pre_ping=True, pool_recycle=config.DB_POOL_RECYCLE_SECONDS,
            pool_size=config.DB_POOL_SIZE, max_overflow=config.DB_MAX_OVERFLOW,
            pool_timeout=config.DB_POOL_TIMEOUT_SECONDS,
            connect_args={key: self.options[key] for key in
                          ("connect_timeout", "read_timeout", "write_timeout")},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        """教学代码兼容入口；应用仓储使用 orm_session。"""
        with self.engine.begin() as connection:
            yield MySQLSession(connection)

    @contextmanager
    def orm_session(self):
        with self.sessions.begin() as session:
            yield session

    @contextmanager
    def checkpoint_connection(self):
        # 每次借还，checkout 会检查断线；不在进程启动时永久占用连接。
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            raw = connection.connection.dbapi_connection
            try:
                yield raw
            except Exception as exc:
                if self.engine.dialect.is_disconnect(exc, raw, None):
                    connection.invalidate(exc)
                raise

    def close(self):
        self.engine.dispose()

    def ping(self) -> None:
        with self.session() as db:
            db.execute("SELECT 1").fetchone()

    @contextmanager
    def lock(self, key: str, timeout: int = 2):
        """命名锁与操作始终在同一连接；释放失败时丢弃连接，防止锁回到池中。"""
        name = "aml:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:56]
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            result = connection.exec_driver_sql("SELECT GET_LOCK(%s, %s) AS acquired", (name, timeout)).scalar()
            if result is None:
                raise RuntimeError("MySQL 命名锁不可用")
            if result != 1:
                raise AgentBusy("同一任务正在处理，请稍后重试")
            try:
                yield
            finally:
                try:
                    released = connection.exec_driver_sql("SELECT RELEASE_LOCK(%s)", (name,)).scalar()
                    if released != 1:
                        raise RuntimeError("MySQL 命名锁释放失败")
                except BaseException:
                    connection.invalidate()
                    raise
