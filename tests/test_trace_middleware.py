"""仅发送对话生成链路，查询与心跳无 Trace，流式完成后只保存一次。"""
import os
import unittest
from unittest.mock import patch
os.environ.setdefault("MYSQL_DSN", "mysql://agent:test@localhost/aml_task_assist")
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse
from task_assist.api.app import request_trace

class TraceMiddlewareTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.middleware("http")(request_trace)
        @app.post("/api/conversations/{conversation_id}/messages")
        def chat(request: Request, conversation_id: str):
            request.state.trace.link(conversation_id=conversation_id)
            request.state.trace.record("model", "test", visible_output="回答")
            return {"answer": "回答"}
        @app.post("/api/conversations/{conversation_id}/messages/stream")
        def chat_stream(request: Request, conversation_id: str):
            async def body():
                yield b"chunk-1"
                request.state.trace.record("model", "test", visible_output="回答")
                yield b"chunk-2"
            return StreamingResponse(body(), media_type="text/event-stream")
        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT"])
        def unrelated(request: Request, path: str):
            with request.state.trace.step("tool", "should_not_record"):
                request.state.trace.record("model", "should_not_record")
            return {"trace_id": request.state.trace.id, "events": list(request.state.trace.events)}
        self.client = TestClient(app)

    def test_non_dialogue_queries_and_mutations_never_create_trace(self):
        with patch("task_assist.api.app.trace_store") as store:
            for path in ("/health", "/ready", "/api/widget-config", "/api/traces", "/api/traces/dashboard",
                         "/api/conversations", "/api/conversations/c1", "/api/tasks/risk", "/api/settings", "/widget/"):
                response = self.client.get(path)
                self.assertNotIn("X-Trace-Id", response.headers, path)
                self.assertEqual(response.json(), {"trace_id": None, "events": []})
            self.client.post("/api/conversations")
            self.client.post("/api/automation/run")
            self.client.put("/api/settings")
            store.save.assert_not_called()

    def test_chat_saves_one_trace_without_http_request_events(self):
        with patch("task_assist.api.app.trace_store") as store:
            response = self.client.post("/api/conversations/c1/messages")
        self.assertEqual(response.status_code, 200)
        self.assertIn("X-Trace-Id", response.headers)
        store.save.assert_called_once()
        payload = store.save.call_args.args[0].as_dict()
        self.assertEqual(payload["name"], "conversation_turn")
        self.assertEqual([event["kind"] for event in payload["events"]], ["model"])

    def test_stream_saves_completed_model_events_once(self):
        saved = []
        with patch("task_assist.api.app.trace_store") as store:
            store.save.side_effect = lambda trace: saved.append(trace.as_dict())
            response = self.client.post("/api/conversations/c1/messages/stream")
        self.assertEqual(response.content, b"chunk-1chunk-2")
        self.assertEqual(len(saved), 1)
        self.assertEqual([event["kind"] for event in saved[0]["events"]], ["model"])
