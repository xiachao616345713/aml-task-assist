"""内核和存储共同使用的状态错误。"""


class ResourceNotFound(LookupError):
    pass


class StateConflict(RuntimeError):
    pass


class AgentBusy(RuntimeError):
    """模型执行名额已满，调用方可稍后重新发起整个对话请求。"""
