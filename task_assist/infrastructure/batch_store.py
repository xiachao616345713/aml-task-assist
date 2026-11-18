"""批量资料和实际写入快照；回执与会话消息在同一事务提交。"""

import hashlib
import json
import time
import uuid
import threading
from types import SimpleNamespace

from sqlalchemy import select, func

from task_assist.core.errors import StateConflict, ResourceNotFound
from task_assist.infrastructure.models import MaterialBatches, BatchSources, BatchSubmissions, BatchTraceLinks, Conversations, Messages, as_dict
from task_assist.infrastructure.state_store import StateStore


def public_source(source):
    return {k: v for k, v in source.items() if k not in {"content", "text_content", "extraction", "owner_id", "backend_path"}}


class BatchStore:
    def __init__(self, database):
        self.database = database
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def ensure_schema(self):
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self.database.lock('material-batches-schema'):
                with self.database.engine.begin() as connection:
                    for model in (MaterialBatches, BatchSources, BatchSubmissions, BatchTraceLinks):
                        model.__table__.create(connection, checkfirst=True)
            self._schema_ready = True

    def _db(self):
        self.ensure_schema()
        return self.database.orm_session()

    def create(self, owner, process_type, conversation_id=None):
        now, ident = time.time(), uuid.uuid4().hex
        with self._db() as db:
            if conversation_id:
                StateStore._owned(db, Conversations, conversation_id, owner)
            else:
                conversation_id = uuid.uuid4().hex
                db.add(Conversations(id=conversation_id, owner_id=owner, title="批量资料 · " + process_type,
                                     created_at=now, updated_at=now))
                db.flush()
            obj = MaterialBatches(id=ident, owner_id=owner, process_type=process_type,
                conversation_id=conversation_id, status="open", created_at=now, updated_at=now)
            db.add(obj)
            db.flush()
            self._message(db, obj, "user", "创建批量资料任务，类型：" + process_type + "；批次：" + ident)
            return as_dict(obj)

    def _message(self, db, batch, role, content, trace_id=None):
        conversation = StateStore._owned(db, Conversations, batch.conversation_id, batch.owner_id, lock=True)
        conversation.updated_at = time.time()
        db.add(Messages(id=uuid.uuid4().hex, owner_id=batch.owner_id, conversation_id=batch.conversation_id,
            role=role, content=content, trace_id=trace_id, created_at=time.time()))

    def get(self, ident, owner):
        with self._db() as db:
            return as_dict(StateStore._owned(db, MaterialBatches, ident, owner))

    def list(self, owner, conversation_id=None, *, page=1, page_size=50, process_type=None, status=None):
        with self._db() as db:
            query=select(MaterialBatches)
            counts=[]
            for name,item_status in [('submitted_count','submitted'),('skipped_count','skipped'),('uncertain_count','uncertain')]:
                counts.append(select(func.count()).select_from(BatchSubmissions).where(
                    BatchSubmissions.batch_id==MaterialBatches.id,BatchSubmissions.status==item_status).scalar_subquery().label(name))
            counts.append(select(func.count()).select_from(BatchSources).where(
                BatchSources.batch_id==MaterialBatches.id).scalar_subquery().label('source_count'))
            query=query.add_columns(*counts).where(MaterialBatches.owner_id==owner)
            if conversation_id:
                StateStore._owned(db,Conversations,conversation_id,owner)
                query=query.where(MaterialBatches.conversation_id==conversation_id)
            if process_type:
                query=query.where(MaterialBatches.process_type==process_type)
            if status:
                query=query.where(MaterialBatches.status==status)
            return [{**as_dict(row[0]),**dict(zip(('submitted_count','skipped_count','uncertain_count','source_count'),row[1:]))}
                for row in db.execute(query.order_by(MaterialBatches.created_at.desc()).offset((page-1)*page_size).limit(page_size))]

    def count(self,owner,conversation_id=None,process_type=None,status=None):
        with self._db() as db:
            query=select(func.count()).select_from(MaterialBatches).where(MaterialBatches.owner_id==owner)
            if conversation_id: query=query.where(MaterialBatches.conversation_id==conversation_id)
            if process_type: query=query.where(MaterialBatches.process_type==process_type)
            if status: query=query.where(MaterialBatches.status==status)
            return db.scalar(query)

    def sources(self, ident, owner, *, content=False):
        self.get(ident, owner)
        with self._db() as db:
            rows = db.scalars(select(BatchSources).where(BatchSources.batch_id == ident,
                BatchSources.owner_id == owner).order_by(BatchSources.created_at, BatchSources.id))
            return [{k: v for k, v in as_dict(o).items() if content or k != "content"} for o in rows]

    def source(self, ident):
        with self._db() as db:
            obj = db.get(BatchSources, ident)
            if not obj:
                raise ResourceNotFound("资料不存在或无权访问")
            return as_dict(obj)

    def add_sources(self, ident, owner, records):
        with self._db() as db:
            batch = StateStore._owned(db, MaterialBatches, ident, owner, lock=True)
            if batch.status != "open":
                raise StateConflict("批次已开始处理，不能继续上传")
            count = db.scalar(select(func.count()).select_from(BatchSources).where(BatchSources.batch_id == ident))
            size = db.scalar(select(func.sum(BatchSources.size_bytes)).where(BatchSources.batch_id == ident)) or 0
            if count + len(records) > 200 or size + sum(r["size_bytes"] for r in records) > 50 * 1024 * 1024:
                raise ValueError("每批最多200份资料且合计原件不超过50MiB")
            for record in records:
                db.add(BatchSources(**record, batch_id=ident, owner_id=owner, created_at=time.time()))
            batch.updated_at = time.time()
            self._message(db, batch, "user", "上传材料：\n" + "\n".join(r["filename"] + " · SHA-256 " + r["sha256"] for r in records))
        return [public_source(r) for r in records]

    def update_source(self, ident, owner, **values):
        with self._db() as db:
            obj = StateStore._owned(db, BatchSources, ident, owner)
            for key, value in values.items():
                if key not in {"backend_path", "extraction"}:
                    raise ValueError("资料更新字段无效")
                setattr(obj, key, value)

    def set_status(self, ident, owner, status):
        with self._db() as db:
            obj = StateStore._owned(db, MaterialBatches, ident, owner, lock=True)
            obj.status, obj.updated_at = status, time.time()

    def items(self, ident, owner):
        self.get(ident, owner)
        with self._db() as db:
            return [as_dict(o) for o in db.scalars(select(BatchSubmissions).where(
                BatchSubmissions.batch_id == ident).order_by(BatchSubmissions.created_at, BatchSubmissions.id))]

    def record_items(self, process_type, act_id, owner=None):
        with self._db() as db:
            query = select(BatchSubmissions).where(BatchSubmissions.process_type == process_type,
                                                   BatchSubmissions.act_id == act_id)
            if owner:
                query = query.where(BatchSubmissions.owner_id == owner)
            return [as_dict(o) for o in db.scalars(query.order_by(BatchSubmissions.created_at.desc()).limit(200))]

    def item(self, ident):
        with self._db() as db:
            obj = db.get(BatchSubmissions, ident)
            if obj is None:
                raise ResourceNotFound("提交记录不存在或无权访问")
            return as_dict(obj)

    def save_item(self, batch, key, status, data, *, task_id=None, act_id=None, receipt=False):
        ident = hashlib.sha256((batch["id"] + ":" + key).encode()).hexdigest()[:32]
        with self._db() as db:
            obj = db.get(BatchSubmissions, ident)
            if obj is None:
                obj = BatchSubmissions(id=ident, batch_id=batch["id"], owner_id=batch["owner_id"],
                    process_type=batch["process_type"], task_id=task_id, act_id=act_id,
                    created_at=time.time())
                db.add(obj)
            obj.status, obj.data, obj.updated_at = status, data, time.time()
            db.flush()
            if receipt:
                self._message(db, SimpleNamespace(**batch), "assistant", self.receipt(as_dict(obj)), trace_id=data.get("trace_id"))
            return as_dict(obj)

    @staticmethod
    def receipt(item):
        data = item["data"]
        return ("批量资料处理回执\n\n批次：" + item["batch_id"] + "；记录：" + item["id"] +
                "\n\n目标任务：" + str(item["task_id"] or "未匹配") + "；业务记录：" + str(item["act_id"] or "未匹配") +
                "\n\n状态：" + item["status"] + "；说明：" + data.get("reason", "") +
                "\n\n实际提交与来源快照：\n```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```")

    def link_trace(self, ident, owner, trace_id, phase):
        if not trace_id:
            return
        with self._db() as db:
            StateStore._owned(db, MaterialBatches, ident, owner)
            if db.get(BatchTraceLinks, trace_id) is None:
                db.add(BatchTraceLinks(trace_id=trace_id,batch_id=ident,owner_id=owner,
                                       phase=phase,created_at=time.time()))
