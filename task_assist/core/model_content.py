"""读取提供方实际返回的回答与推理字段；不推断隐藏思维。"""


def _texts(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_texts(item) for item in value)
    if isinstance(value, dict):
        return _texts(value.get("text") or value.get("summary") or value.get("reasoning") or "")
    return ""


def model_content(message):
    message = getattr(message, "message", message)  # ChatGenerationChunk 包含 AIMessageChunk。
    content = getattr(message, "content", "")
    additional = getattr(message, "additional_kwargs", None) or {}
    metadata = getattr(message, "response_metadata", None) or {}
    reasoning = _texts(additional.get("reasoning_content") or additional.get("reasoning")
                       or additional.get("reasoning_details"))
    summary = _texts(additional.get("reasoning_summary") or metadata.get("reasoning_summary"))
    answer, block_reasoning = [], []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in {"reasoning", "thinking", "reasoning_content"}:
                block_reasoning.append(_texts(block.get("reasoning") or block.get("thinking")
                                              or block.get("summary") or block.get("text")))
            elif kind in {"text", "output_text", "text_delta"}:
                answer.append(_texts(block.get("text")))
    elif isinstance(content, str):
        answer.append(content)
    return {"answer": "".join(answer), "reasoning": reasoning or "".join(block_reasoning),
            "reasoning_summary": summary}
