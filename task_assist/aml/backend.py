"""只通过现有 aml HTTP 接口访问任务，沿用登录用户的权限范围。"""

from typing import Any
import time

import httpx

from task_assist import config
from task_assist.core.retry import RetryPolicy


class BackendError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class BackendClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None,
                 trace_id: str | None = None, trace=None, base_url: str | None = None):
        if not token:
            raise BackendError("缺少 X-Access-Token")
        self.token = token
        self.trace = trace
        headers = {"X-Access-Token": token}
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        self.client = httpx.Client(
            base_url=(config.AML_BASE_URL if base_url is None else base_url).rstrip("/") + "/",
            headers=headers,
            timeout=15,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _transient(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {429, 502, 503, 504}
        return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError,
                                httpx.ReadError, httpx.RemoteProtocolError))

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not getattr(self.trace, 'id', None):
            return self._request_impl(method,path,**kwargs)
        self.trace.add_secret(self.token)
        started=time.monotonic();at=time.time();status='ok';fields={}
        try:
            result=self._request_impl(method,path,**kwargs)
            fields['output']=result
            return result
        except Exception as exc:
            status='error';fields['error_type']=type(exc).__name__
            raise
        finally:
            self.trace.record('backend',path,status,method=method,
                input={k:v for k,v in kwargs.items() if k in {'params','json'}},
                started_at=at,duration_ms=round((time.monotonic()-started)*1000,2),**fields)

    def _request_impl(self, method: str, path: str, *, allow_retry: bool = True, **kwargs: Any) -> Any:
        # 只读 GET 可以重试。auditTaskLock 虽为 GET 却有副作用，调用方必须禁用重试。
        attempts = 3 if method == "GET" and allow_retry else 1

        def send():
            response = self.client.request(method, path.lstrip("/"), **kwargs)
            response.raise_for_status()
            return response.json()

        def observed(attempt: int, status: str, error: Exception | None, duration_ms: float):
            if self.trace:
                self.trace.record("backend_attempt", path, status, method=method, attempt=attempt,
                                  duration_ms=round(duration_ms, 2),
                                  error_type=type(error).__name__ if error else None,
                                  status_code=error.response.status_code
                                  if isinstance(error, httpx.HTTPStatusError) else None)

        try:
            body = RetryPolicy(max_attempts=attempts).run(send, self._transient, observed)
        except (httpx.HTTPError, ValueError) as exc:
            # httpx 异常可能包含带 query token 的完整 URL，不能回显到 API 或 Trace。
            raise BackendError(f"后端接口 {path} 调用失败 ({type(exc).__name__})",
                               retryable=method == "GET" and allow_retry and self._transient(exc)) from exc
        if not isinstance(body, dict) or body.get("success") is not True:
            if path in {"sys/permission/getUserPermissionByToken", "sys/user/getUserSectionInfoByToken"}:
                raise BackendError("aml 身份或权限校验失败")
            # Java 返回的 message 可能包含客户数据或请求参数，统一返回稳定错误。
            raise BackendError(f"后端接口 {path} 返回失败")
        return body.get("result")

    def summary(self) -> list[dict]:
        result = self._request("GET", "home/indexProcessInfo")
        if not isinstance(result, list):
            raise BackendError("待办汇总响应不是列表")
        return result

    def current_user(self) -> dict:
        result = self._request("GET", "sys/user/getUserSectionInfoByToken")
        if not isinstance(result, dict) or not result.get("sysUserId") or not result.get("sysUserCode"):
            raise BackendError("无法确认当前登录用户")
        return result

    def permissions(self) -> set[str]:
        # 现有 Java 权限接口只接受 query token；Java 侧应改造为仅从请求头取 token。
        result = self._request("GET", "sys/permission/getUserPermissionByToken", params={"token": self.token})
        if not isinstance(result, dict) or not isinstance(result.get("auth"), list):
            raise BackendError("权限响应格式异常")
        return {str(item.get("action")) for item in result["auth"] if isinstance(item, dict) and item.get("action")}

    def risk_page(self, page: int = 1, page_size: int = 20, filters: dict | None = None) -> dict:
        result = self._request("GET", "home/riskList", params={"pageNo": page, "pageSize": page_size, **(filters or {})})
        if not isinstance(result, dict) or not isinstance(result.get("records"), list):
            raise BackendError("风险待办响应格式异常")
        return result

    def bnf_page(self, page: int = 1, page_size: int = 100, filters: dict | None = None) -> dict:
        result = self._request("GET", "custandbnfinfo/bnfReport/queryProcessPage",
                               params={"pageNo": page, "pageSize": page_size, **(filters or {})})
        if not isinstance(result, dict) or not isinstance(result.get("records"), list):
            raise BackendError("受益所有人待办响应格式异常")
        return result

    def risk_levels(self) -> list[dict]:
        result = self._request("GET", "riskLevel/list")
        if not isinstance(result, list):
            raise BackendError("风险等级字典响应格式异常")
        return result

    def risk_detail(self, act_id: str) -> dict:
        result = self._request("GET", f"riskList/riskInfoByActId/{act_id}")
        if not isinstance(result, dict):
            raise BackendError("风险详情响应格式异常")
        return result

    def transitions(self, task_id: str) -> dict:
        result = self._request("GET", "act/task/getProcessTaskTransInfo", params={"taskId": task_id})
        if not isinstance(result, dict):
            raise BackendError("流程流转信息异常")
        return result

    def task_lock(self, task_id: str) -> None:
        # 审批接口约定提交前获取办理锁。
        self._request("GET", "aresoft/act/auditTaskLock", allow_retry=False, params={"taskId": task_id})

    def complete(self, payload: dict) -> Any:
        return self._request("POST", "aresoft/act/processComplete", json=payload)

    def upload_material(self, filename: str, content: bytes, content_type: str) -> str:
        try:
            response = self.client.post("sys/common/upload", files={"file": (filename, content, content_type)})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendError("材料上传到 Java 服务失败") from exc
        if not isinstance(body, dict) or body.get("success") is not True or not body.get("message"):
            raise BackendError("材料上传到 Java 服务失败")
        return str(body["message"])

    def risk_customer_count(self, act_id: str) -> int:
        result = self._request("GET", f"risk/activitiRel/queryActRelCount/{act_id}")
        if not isinstance(result, int) or isinstance(result, bool) or result < 0:
            raise BackendError("风险任务客户范围无法核实")
        return result

    def submit_batch_payload(self, path: str, payload: dict, submission_id: str):
        return self._request("POST", path, allow_retry=False, json=payload,
                             headers={"X-Assist-Submission-Id": submission_id})
