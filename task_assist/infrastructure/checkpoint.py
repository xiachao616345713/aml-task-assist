"""LangGraph MySQL Checkpointer 的单一导入点，提供清晰的缺依赖提示。"""


def mysql_saver():
    try:
        from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("langgraph.checkpoint.mysql"):
            raise RuntimeError(
                "缺少 langgraph-checkpoint-mysql。请在当前 Python 环境执行："
                "python -m pip install 'langgraph-checkpoint-mysql[pymysql]==3.0.0'"
            ) from exc
        raise
    return PyMySQLSaver


def pooled_mysql_saver(database):
    from contextlib import contextmanager

    class PooledSaver(mysql_saver()):
        @contextmanager
        def _cursor(self, *, pipeline=False):
            # 不使用上游 connection factory 对上下文管理器的特殊处理，明确负责归还。
            with self.lock, database.checkpoint_connection() as connection:
                if pipeline:
                    connection.begin()
                try:
                    with self._get_cursor_from_connection(connection) as cursor:
                        yield cursor
                    if pipeline:
                        connection.commit()
                except Exception:
                    if pipeline:
                        connection.rollback()
                    raise

    return PooledSaver(None)
