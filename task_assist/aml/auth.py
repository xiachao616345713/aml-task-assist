"""请求身份与业务按钮权限。模型和浏览器都不能声称自己拥有权限。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import BackendClient


class PermissionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Actor:
    user_id: str
    username: str
    permissions: frozenset[str]
    token: str = field(repr=False)

    def require(self, action: str) -> None:
        if action not in self.permissions:
            raise PermissionDenied(f"缺少权限: {action}")


def authenticate(backend: BackendClient) -> Actor:
    identity = backend.current_user()
    permissions = backend.permissions()
    return Actor(
        user_id=str(identity["sysUserId"]),
        username=str(identity["sysUserCode"]),
        permissions=frozenset(permissions),
        token=backend.token,
    )
