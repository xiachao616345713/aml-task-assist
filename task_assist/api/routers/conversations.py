"""会话管理和多轮问答入口。"""

import asyncio
import json
import queue
import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from starlette.responses import StreamingResponse

from task_assist.aml.service import TaskService
from task_assist.api.container import agent_context, state_store
from task_assist.api.deps import get_service, http_error, resolve_service, run_traced
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


@router.post("/api/conversations/{conversation_id}/messages/stream")
def stream_message(request: Request, conversation_id: str, body: Question,
                   x_access_token: str | None = Header(default=None)):
    """SSE 流式问答：实时下发思考增量、工具调用与回答增量。

    Agent 在工作线程中执行（数据库锁与信号量需要线程亲和），事件经队列转交
    异步生成器；服务会话与并发额度在流结束或客户端断开时释放。
    """
    service = resolve_service(request, x_access_token)
    agent = request.app.state.conversation_agent
    trace = request.state.trace
    context = agent_context(service, trace)
    try:
        handle = agent.prepare_turn(body.question, conversation_id, context)
    except HTTPException:
        service.backend.close()
        raise
    except Exception as exc:
        service.backend.close()
        trace.record("operation", "conversation_turn_stream", "error",
                     error_type=type(exc).__name__)
        raise http_error(exc) from exc

    def sse(event: dict) -> str:
        return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    async def event_stream():
        events: queue.Queue = queue.Queue()
        cancelled = threading.Event()

        def worker():
            turn = agent.stream_turn(handle, body.question, conversation_id, context)
            try:
                for event in turn:
                    if cancelled.is_set():
                        break
                    events.put(event)
            except Exception:
                events.put({"type": "error", "trace_id": trace.id,
                            "message": "对话执行失败，请按 Trace ID 查看调用链。"})
            finally:
                turn.close()
                events.put(None)

        threading.Thread(target=worker, daemon=True, name=f"agent-stream-{trace.id[:8]}").start()
        try:
            while True:
                event = await asyncio.to_thread(events.get)
                if event is None:
                    break
                yield sse(event)
        finally:
            cancelled.set()
            service.backend.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/assistant/query")
def query(request: Request, body: Question, service: TaskService = Depends(get_service)):
    # 兼容旧入口；首次无 conversation_id 时创建会话，后续必须传回该 ID。
    conversation_id = body.conversation_id or state_store.create_conversation(service.actor.user_id)["id"]
    return run_traced(request, "conversation_turn", lambda trace: request.app.state.conversation_agent.reply(
        body.question, conversation_id, agent_context(service, trace)))
