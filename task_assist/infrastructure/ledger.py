"""SQLAlchemy 提交幂等记录；结果不明时不自动重试。"""
import time
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from task_assist.infrastructure.mysql import MySQLDatabase
from task_assist.infrastructure.models import SubmissionsV3


class SubmissionLedger:
    def __init__(self, database=None):
        self.database = database or MySQLDatabase()

    def ping(self):
        self.database.ping()

    def status(self, task_id):
        with self.database.orm_session() as db:
            obj = db.get(SubmissionsV3, task_id)
            return obj.status if obj else None

    def claim(self, task_id, owner_id):
        try:
            with self.database.orm_session() as db:
                db.add(SubmissionsV3(task_id=task_id, owner_id=owner_id, status="submitting", updated_at=time.time()))
            return True
        except IntegrityError as exc:
            if getattr(exc.orig, "args", [None])[0] == 1062:
                return False
            raise

    def mark(self, task_id, status):
        if status not in {"submitted", "uncertain"}:
            raise ValueError("提交状态无效")
        with self.database.orm_session() as db:
            obj = db.get(SubmissionsV3, task_id)
            if obj:
                obj.status, obj.updated_at = status, time.time()

    def recent(self, owner_id, limit=50):
        with self.database.orm_session() as db:
            rows = db.scalars(select(SubmissionsV3).where(SubmissionsV3.owner_id == owner_id)
                              .order_by(SubmissionsV3.updated_at.desc()).limit(limit))
            return [dict(task_id=x.task_id, status=x.status, updated_at=x.updated_at) for x in rows]
