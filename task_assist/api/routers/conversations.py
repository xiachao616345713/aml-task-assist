"""会话管理和多轮问答入口。"""

from fastapi import APIRouter, Depends, HTTPException, Request

from task_assist.aml.service import TaskService
from task_assist.api.container import agent_context, state_store
from task_assist.api.deps import get_service, run_traced
from task_assist.api.schemas import Question
from task_assist.infrastructure.state_store import ResourceNotFound

router = APIRouter()

@router.post("/api/conversations")
def create_conversation(request: Request, service: TaskService = Depends(get_service)):
    return run_traced(request, "create_conversation",
                      lambda trace: state_store.create_conversation(service.actor.user_id))


@router.get("/api/conversations")
def conversations(service: TaskService = Depends(get_service)):
    return {"records": state_store.conversations(service.actor.user_id)}


@router.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, service: TaskService = Depends(get_service)):
    try:
        data = state_store.conversation(conversation_id, service.actor.user_id)
        data["messages"] = state_store.messages(conversation_id, service.actor.user_id)
        return data
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/conversations/{conversation_id}/messages")
def add_message(request: Request, conversation_id: str, body: Question,
                service: TaskService = Depends(get_service)):
    return run_traced(request, "conversation_turn", lambda trace: request.app.state.conversation_agent.reply(
        body.question, conversation_id, agent_context(service, trace)))


@router.post("/api/assistant/query")
def query(request: Request, body: Question, service: TaskService = Depends(get_service)):
    # 兼容旧入口；首次无 conversation_id 时创建会话，后续必须传回该 ID。
    conversation_id = body.conversation_id or state_store.create_conversation(service.actor.user_id)["id"]
    return run_traced(request, "conversation_turn", lambda trace: request.app.state.conversation_agent.reply(
        body.question, conversation_id, agent_context(service, trace)))
