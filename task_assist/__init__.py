"""AML 待办助手。"""

import os

# 限制 LangGraph 检查点反序列化范围；应在导入 checkpointer 前设置。
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
