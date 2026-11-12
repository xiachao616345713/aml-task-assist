"""SQLAlchemy 会话、材料任务和审计仓储。"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from task_assist.core.errors import ResourceNotFound, StateConflict
from task_assist.infrastructure.mysql import MySQLDatabase
from task_assist.infrastructure.models import (Conversations, Messages, ReviewWorkflows,
    MaterialTasks, Attachments, AttachmentContents, AuditEvents, as_dict)


class StateStore:
    def __init__(self, database: MySQLDatabase | None = None):
        self.database = database or MySQLDatabase()

    def _db(self):
        return self.database.orm_session()

    def ping(self):
        self.database.ping()

    @staticmethod
    def _owned(db, model, ident, owner_id, *, lock=False):
        query = select(model).where(model.id == ident, model.owner_id == owner_id)
        if lock:
            query = query.with_for_update()
        obj = db.scalar(query)
        if obj is None:
            raise ResourceNotFound("资源不存在或无权访问")
        return obj

    def create_conversation(self, owner_id, title="新对话"):
        now, ident = time.time(), uuid.uuid4().hex
        with self._db() as db:
            obj = Conversations(id=ident, owner_id=owner_id, title=title[:100], created_at=now, updated_at=now)
            db.add(obj)
            return as_dict(obj)

    def conversation(self, conversation_id, owner_id):
        with self._db() as db:
            return as_dict(self._owned(db, Conversations, conversation_id, owner_id))

    def conversations(self, owner_id):
        with self._db() as db:
            return [as_dict(x) for x in db.scalars(select(Conversations).where(
                Conversations.owner_id == owner_id).order_by(Conversations.updated_at.desc()).limit(100))]

    def add_message(self, conversation_id, owner_id, role, content, trace_id):
        if role not in {"user", "assistant"}:
            raise ValueError("消息角色无效")
        now, ident = time.time(), uuid.uuid4().hex
        with self._db() as db:
            conversation = self._owned(db, Conversations, conversation_id, owner_id)
            conversation.updated_at = now
            db.add(Messages(id=ident, conversation_id=conversation_id, owner_id=owner_id,
                            role=role, content=content, trace_id=trace_id, created_at=now))
        return dict(id=ident, role=role, content=content, trace_id=trace_id, created_at=now)

    def messages(self, conversation_id, owner_id, limit=100):
        with self._db() as db:
            self._owned(db, Conversations, conversation_id, owner_id)
            rows = db.scalars(select(Messages).where(Messages.conversation_id == conversation_id,
                Messages.owner_id == owner_id).order_by(Messages.created_at.desc()).limit(min(limit, 200)))
            return [{k: getattr(x, k) for k in ("id", "role", "content", "trace_id", "created_at")} for x in rows][::-1]

    def create_workflow(self, owner_id, process_type, task_id, act_id, conversation_id, required):
        now, workflow_id = time.time(), uuid.uuid4().hex
        material_id = uuid.uuid4().hex if required else None
        slot = hashlib.sha256("\0".join((owner_id, process_type, task_id, act_id)).encode()).hexdigest()
        with self._db() as db:
            if conversation_id:
                self._owned(db, Conversations, conversation_id, owner_id)
            existing = db.scalar(select(ReviewWorkflows).where(ReviewWorkflows.active_slot == slot))
            if existing:
                return as_dict(existing)
            try:
                with db.begin_nested():
                    db.add(ReviewWorkflows(id=workflow_id, owner_id=owner_id, conversation_id=conversation_id,
                        process_type=process_type, task_id=task_id, act_id=act_id, status="inspecting",
                        material_task_id=material_id, trace_id=None, created_at=now, updated_at=now, active_slot=slot))
                    db.flush()
            except IntegrityError as exc:
                if getattr(exc.orig, "args", [None])[0] != 1062:
                    raise
                # 锁定读取获得最新已提交行，避免 REPEATABLE READ 的旧快照。
                existing = db.scalar(select(ReviewWorkflows).where(ReviewWorkflows.active_slot == slot).with_for_update())
                if existing:
                    return as_dict(existing)
                raise
            if material_id:
                db.add(MaterialTasks(id=material_id, owner_id=owner_id, workflow_id=workflow_id,
                    status="open", required_json=required, created_at=now, updated_at=now))
        return self.workflow(workflow_id, owner_id)

    def workflow(self, workflow_id, owner_id):
        with self._db() as db:
            return as_dict(self._owned(db, ReviewWorkflows, workflow_id, owner_id))

    def workflows(self, owner_id):
        with self._db() as db:
            return [as_dict(x) for x in db.scalars(select(ReviewWorkflows).where(
                ReviewWorkflows.owner_id == owner_id).order_by(ReviewWorkflows.updated_at.desc()).limit(100))]

    def set_workflow_status(self, workflow_id, owner_id, status, trace_id):
        with self._db() as db:
            obj = self._owned(db, ReviewWorkflows, workflow_id, owner_id, lock=True)
            obj.status, obj.trace_id, obj.updated_at = status, trace_id, time.time()
            if status in {"completed", "blocked", "uncertain", "error"}:
                obj.active_slot = None
            return as_dict(obj)

    def material_task(self, task_id, owner_id):
        with self._db() as db:
            task = as_dict(self._owned(db, MaterialTasks, task_id, owner_id))
        required = task.pop("required_json")
        task["required"] = json.loads(required) if isinstance(required, str) else required
        task["attachments"] = self.attachments(task_id, owner_id)
        return task

    def material_tasks(self, owner_id):
        with self._db() as db:
            ids = list(db.scalars(select(MaterialTasks.id).where(MaterialTasks.owner_id == owner_id)
                       .order_by(MaterialTasks.updated_at.desc()).limit(100)))
        return [self.material_task(ident, owner_id) for ident in ids]

    def add_attachment(self, task_id, owner_id, filename, content_type, size_bytes, sha256,
                       backend_path, content=None, text_content=""):
        now, ident = time.time(), uuid.uuid4().hex
        with self._db() as db:
            task = self._owned(db, MaterialTasks, task_id, owner_id, lock=True)
            if task.status != "open":
                raise StateConflict("材料任务已提交，不能继续上传")
            db.add(Attachments(id=ident, owner_id=owner_id, material_task_id=task_id, filename=filename,
                content_type=content_type, size_bytes=size_bytes, sha256=sha256, backend_path=backend_path, created_at=now))
            db.flush()
            if content is not None:
                db.add(AttachmentContents(attachment_id=ident, content=content, text_content=text_content))
        return dict(id=ident, filename=filename, content_type=content_type, size_bytes=size_bytes, sha256=sha256, created_at=now)

    def source(self, source_id, owner_id):
        with self._db() as db:
            result = as_dict(self._owned(db, Attachments, source_id, owner_id))
            content = db.get(AttachmentContents, source_id)
            result.update(content=content.content if content else None, text_content=content.text_content if content else None)
            return result

    def material_sources(self, task_id, owner_id):
        with self._db() as db:
            rows = db.execute(select(Attachments, AttachmentContents.text_content).outerjoin(
                AttachmentContents, AttachmentContents.attachment_id == Attachments.id).where(
                Attachments.material_task_id == task_id, Attachments.owner_id == owner_id).order_by(Attachments.created_at))
            return [dict(as_dict(obj), text_content=text) for obj, text in rows]

    def attachments(self, task_id, owner_id):
        with self._db() as db:
            return [as_dict(x) for x in db.scalars(select(Attachments).where(
                Attachments.material_task_id == task_id, Attachments.owner_id == owner_id).order_by(Attachments.created_at))]

    def submit_material(self, task_id, owner_id):
        with self._db() as db:
            task = self._owned(db, MaterialTasks, task_id, owner_id, lock=True)
            if task.status != "open":
                raise StateConflict("材料任务状态不允许再次提交")
            if db.scalar(select(Attachments.id).where(Attachments.material_task_id == task_id,
                         Attachments.owner_id == owner_id).limit(1)) is None:
                raise StateConflict("请先上传至少一份材料")
            task.status, task.updated_at = "submitted", time.time()
        return self.material_task(task_id, owner_id)

    def mark_material_resumed(self, task_id, owner_id):
        with self._db() as db:
            task = self._owned(db, MaterialTasks, task_id, owner_id, lock=True)
            if task.status == "submitted":
                task.status, task.updated_at = "resumed", time.time()

    def audit(self, owner_id, action, target_id, outcome, trace_id):
        with self._db() as db:
            db.add(AuditEvents(id=uuid.uuid4().hex, owner_id=owner_id, action=action,
                target_id=target_id, outcome=outcome, trace_id=trace_id, created_at=time.time()))

    def audit_events(self, owner_id, limit=100):
        with self._db() as db:
            rows = db.scalars(select(AuditEvents).where(AuditEvents.owner_id == owner_id)
                              .order_by(AuditEvents.created_at.desc()).limit(min(max(limit, 1), 200)))
            return [{k: v for k, v in as_dict(x).items() if k != "owner_id"} for x in rows]
