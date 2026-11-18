"""为 OpenAI 兼容服务保留提供方返回的推理字段。"""
from langchain_openai import ChatOpenAI

REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_details", "reasoning_summary")


class ObservableChatOpenAI(ChatOpenAI):
    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        generation = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
        if generation is not None and choices:
            delta = choices[0].get("delta") or {}
            for key in REASONING_FIELDS:
                if delta.get(key) is not None:
                    generation.message.additional_kwargs[key] = delta[key]
        return generation

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        # 结构化输出的parsed对象不需要重新序列化；这里只读取提供方的原始推理字段。
        choices = response.get("choices", []) if isinstance(response, dict) else response.choices
        for generation, choice in zip(result.generations, choices):
            message = choice.get("message") or {} if isinstance(choice, dict) else choice.message
            for key in REASONING_FIELDS:
                value = message.get(key) if isinstance(message, dict) else getattr(message, key, None)
                if value is not None:
                    generation.message.additional_kwargs[key] = value
        return result
