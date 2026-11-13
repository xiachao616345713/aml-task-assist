"""真实 LangChain 流解析：兼容服务的思考、工具选择、上下文和用量贯穿同一轮。"""
import json
import unittest
from types import SimpleNamespace
import httpx
from langchain_core.messages import AIMessageChunk
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from task_assist.core.conversation import ConversationAgent
from task_assist.core.model_content import model_content
from task_assist.infrastructure.chat_model import ObservableChatOpenAI
from task_assist.infrastructure.telemetry import LangChainTraceHandler
from task_assist.infrastructure.trace_store import Trace


class ModelObservabilityTests(unittest.TestCase):
    def test_real_agent_stream_keeps_reasoning_tools_and_usage_once(self):
        requests = []
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            after_tool = body['messages'][-1]['role'] == 'tool'
            deltas = ([{'role': 'assistant', 'reasoning_content': '根据工具结果回答。'}, {'content': '共有 2 项待办'}]
                if after_tool else [{'role': 'assistant', 'reasoning_content': '先查询待办。'},
                  {'tool_calls': [{'index': 0, 'id': 'call-1', 'type': 'function',
                    'function': {'name': 'lookup_tasks', 'arguments': '{"page":1}'}}]}])
            frames = [{'id': 'chat-test', 'object': 'chat.completion.chunk', 'model': 'test-model',
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]} for delta in deltas]
            frames.append({'id': 'chat-test', 'object': 'chat.completion.chunk', 'model': 'test-model',
                'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop' if after_tool else 'tool_calls'}]})
            frames.append({'id': 'chat-test', 'object': 'chat.completion.chunk', 'model': 'test-model', 'choices': [],
                'usage': {'prompt_tokens': 20 if after_tool else 10, 'completion_tokens': 6 if after_tool else 4,
                          'total_tokens': 26 if after_tool else 14}})
            wire = ''.join('data: ' + json.dumps(frame, ensure_ascii=False) + '\n\n' for frame in frames) + 'data: [DONE]\n\n'
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=wire.encode())
        @tool
        def lookup_tasks(page: int) -> str:
            """查询指定页的待办。"""
            return '{"ok":true,"data":{"count":2}}'
        provider = SimpleNamespace(system_prompt='你是任务助手', version='test', build_tools=lambda _: [lookup_tasks])
        saved = []
        store = SimpleNamespace(conversation=lambda *args: {}, add_message=lambda *args: saved.append(args))
        model = ObservableChatOpenAI(model='test-model', api_key='test-key', base_url='https://model.test/v1',
            http_client=httpx.Client(transport=httpx.MockTransport(respond)), stream_usage=True)
        trace = Trace('conversation_turn', 'alice')
        context = SimpleNamespace(owner_id='alice', trace=trace)
        agent = ConversationAgent(InMemorySaver(), store, provider, lambda: model, LangChainTraceHandler)
        handle = agent.prepare_turn('我有多少待办', 'c1', context)
        events = list(agent.stream_turn(handle, '我有多少待办', 'c1', context))
        self.assertEqual(events[-1]['type'], 'done', events)
        self.assertEqual(events[-1]['answer'], '共有 2 项待办')
        self.assertEqual(''.join(e['text'] for e in events if e['type'] == 'thinking_delta'), '先查询待办。根据工具结果回答。')
        payload = trace.as_dict()
        self.assertEqual(payload['model_calls'], 2)
        self.assertEqual(payload['tool_calls'], 1)
        self.assertEqual(payload['total_tokens'], {'input': 30, 'output': 10, 'total': 40})
        models = [event for event in payload['events'] if event['kind'] == 'model']
        tools = [event for event in payload['events'] if event['kind'] == 'tool']
        self.assertEqual(models[0]['reasoning'], '先查询待办。')
        self.assertEqual(models[0]['tool_calls'][0]['args'], {'page': 1})
        self.assertEqual(tools[0]['model_run_id'], models[0]['run_id'])
        self.assertEqual(models[1]['visible_output'], '共有 2 项待办')
        self.assertTrue(any(m['role'] == 'tool' for m in models[1]['input_messages']))
        self.assertIn('first_token_ms', models[0])
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(saved), 2)

    def test_nonstream_reasoning_and_content_blocks_are_preserved(self):
        model = ObservableChatOpenAI(model='test-model', api_key='test-key')
        result = model._create_chat_result({'model': 'test-model', 'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': '回答', 'reasoning_content': '提供方返回的思考'}}]})
        self.assertEqual(model_content(result.generations[0].message)['reasoning'], '提供方返回的思考')
        parts = model_content(AIMessageChunk(content=[{'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': '推理摘要'}]}, {'type': 'text', 'text': '回答'}]))
        self.assertEqual(parts, {'answer': '回答', 'reasoning': '推理摘要', 'reasoning_summary': ''})
