"""只有提交对话的入口生成模型观测链路。"""
import re


def is_conversation_turn(method, path):
    return method == "POST" and (path == "/api/assistant/query" or bool(
        re.fullmatch(r"/api/conversations/[^/]+/messages(?:/stream)?", path)))
