"""本机设置入口；不会把保存的业务凭证提供给远端或嵌入页面。"""
from ipaddress import ip_address
from fastapi import HTTPException, Request


def is_local_request(request: Request) -> bool:
    def loopback(host):
        if host == "localhost":
            return True
        try:
            return ip_address(host or "").is_loopback
        except ValueError:
            return False
    return bool(request.client and loopback(request.client.host) and loopback(request.url.hostname))


def require_local_settings(request: Request):
    if not is_local_request(request):
        raise HTTPException(403, "配置入口仅允许本机访问")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "配置请求来源无效")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "配置请求来源无效")
