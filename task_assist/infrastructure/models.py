"""应用表的 SQLAlchemy 对象映射；既有数据库使用 SQL 迁移管理。"""
from sqlalchemy import Column, String, Text, Float, BigInteger, LargeBinary, ForeignKey, JSON
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT, LONGBLOB

class Base(DeclarativeBase):
    pass

class Conversations(Base):
    __tablename__ = "conversations"
    id = Column(String(32), primary_key=True, nullable=False)
    owner_id = Column(String(128), nullable=False)
    title = Column(String(100), nullable=False)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)

class Messages(Base):
    __tablename__ = "messages"
    id = Column(String(32), primary_key=True, nullable=False)
    conversation_id = Column(String(32), ForeignKey("conversations.id"), nullable=False)
    owner_id = Column(String(128), nullable=False)
    role = Column(String(16), nullable=False)
    content = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    trace_id = Column(String(64), nullable=True)
    created_at = Column(Float, nullable=False)

class LearningConversationStages(Base):
    __tablename__ = "learning_conversation_stages"
    conversation_id = Column(String(32), ForeignKey("conversations.id"), primary_key=True, nullable=False)
    stage = Column(String(2), nullable=False)

class ReviewWorkflows(Base):
    __tablename__ = "review_workflows"
    id = Column(String(32), primary_key=True, nullable=False)
    owner_id = Column(String(128), nullable=False)
    conversation_id = Column(String(32), ForeignKey("conversations.id"), nullable=True)
    process_type = Column(String(100), nullable=False)
    task_id = Column(String(255), nullable=False)
    act_id = Column(String(255), nullable=False)
    status = Column(String(40), nullable=False)
    material_task_id = Column(String(32), nullable=True)
    trace_id = Column(String(64), nullable=True)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    active_slot = Column(String(64), nullable=True, unique=True)

class MaterialTasks(Base):
    __tablename__ = "material_tasks"
    id = Column(String(32), primary_key=True, nullable=False)
    owner_id = Column(String(128), nullable=False)
    workflow_id = Column(String(32), ForeignKey("review_workflows.id"), nullable=False)
    status = Column(String(32), nullable=False)
    required_json = Column(JSON, nullable=False)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)

class Attachments(Base):
    __tablename__ = "attachments"
    id = Column(String(32), primary_key=True, nullable=False)
    owner_id = Column(String(128), nullable=False)
    material_task_id = Column(String(32), ForeignKey("material_tasks.id"), nullable=False)
    filename = Column(String(200), nullable=False)
    content_type = Column(String(160), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    sha256 = Column(String(64), nullable=False)
    backend_path = Column(String(1024), nullable=False)
    created_at = Column(Float, nullable=False)

class AttachmentContents(Base):
    __tablename__ = "attachment_contents"
    attachment_id = Column(String(32), ForeignKey("attachments.id"), primary_key=True, nullable=False)
    content = Column(LargeBinary().with_variant(LONGBLOB(), "mysql"), nullable=False)
    text_content = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)

class AuditEvents(Base):
    __tablename__ = "audit_events"
    id = Column(String(32), primary_key=True, nullable=False)
    owner_id = Column(String(128), nullable=False)
    action = Column(String(80), nullable=False)
    target_id = Column(String(255), nullable=False)
    outcome = Column(String(80), nullable=False)
    trace_id = Column(String(64), nullable=True)
    created_at = Column(Float, nullable=False)

class SubmissionsV3(Base):
    __tablename__ = "submissions_v3"
    task_id = Column(String(255), primary_key=True, nullable=False)
    owner_id = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False)
    updated_at = Column(Float, nullable=False)

class TracesV2(Base):
    __tablename__ = "traces_v2"
    id = Column(String(32), primary_key=True, nullable=False)
    owner_id = Column(String(128), nullable=False)
    created_at = Column(Float, nullable=False)
    data = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    conversation_id = Column(String(32), nullable=True)
    workflow_id = Column(String(32), nullable=True)
    name = Column(String(255), nullable=False)

class AppSettings(Base):
    __tablename__ = "app_settings"
    id = Column(String(32), primary_key=True)
    business_token = Column(Text, nullable=False, default="")
    model_name = Column(String(255), nullable=False)
    model_base_url = Column(String(2048), nullable=False)
    model_api_key = Column(Text, nullable=False, default="")
    model_timeout = Column(Float, nullable=False)
    model_max_tokens = Column(BigInteger, nullable=False)

def as_dict(obj):
    return {column.name: getattr(obj, column.name) for column in obj.__table__.columns}
