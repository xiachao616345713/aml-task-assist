"""外部 HTTP 契约，与工作流内部状态分开。"""

from pydantic import BaseModel, Field


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class RiskApproval(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)
    act_id: str = Field(min_length=1, max_length=128)


class ReviewStart(BaseModel):
    process_type: str
    task_id: str = Field(min_length=1, max_length=128)
    act_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = None


class HumanDecision(BaseModel):
    reason: str = Field(min_length=20, max_length=1000)
