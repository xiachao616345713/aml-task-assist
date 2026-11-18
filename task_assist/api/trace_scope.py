"""对话与批量资料写入生成链路；轮询、查询和心跳不生成。"""
import re


def is_conversation_turn(method, path):
    return method == "POST" and (path == "/api/assistant/query" or bool(
        re.fullmatch(r"/api/conversations/[^/]+/messages(?:/stream)?", path)))


def trace_operation(method, path):
    if is_conversation_turn(method, path):
        return "conversation_turn"
    if method == "POST":
        if path == "/api/material-batches":
            return "material_batch_create"
        match = re.fullmatch(r"/api/material-batches/[^/]+/(files|process)", path)
        if match:
            return "material_batch_upload" if match[1] == "files" else "material_batch_process"
    return None
