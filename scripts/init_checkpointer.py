"""执行一次 LangGraph MySQL 检查点建表/升级；使用具备 DDL 权限的账号。"""

from task_assist.infrastructure.checkpoint import mysql_saver

from task_assist import config


def main() -> None:
    with mysql_saver().from_conn_string(config.MYSQL_DSN) as saver:
        saver.setup()
    print("LangGraph MySQL 检查点表已初始化")


if __name__ == "__main__":
    main()
