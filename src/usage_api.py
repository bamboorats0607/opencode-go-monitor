"""在线 API 层：从 opencode.ai Go 用量系统拉取订阅用量。

数据源（go 订阅系统，与主控制台是两个独立系统）：
    GET https://opencode.ai/workspace/{workspace_id}/go   (Cookie: auth=...)

Cookie 获取优先级：
    1. 本地代理自动捕获（用户浏览器代理指向本程序）
    2. 手动粘贴（设置对话框）

失败（401/403/网络错误）一律不影响本地诊断，由 UI 展示横幅降级。
红线：禁止任何 cookie 解密/自动导出/提权读取。
"""
import logging

import httpx

GO_PAGE_URL = "https://opencode.ai/workspace/{workspace_id}/go"
GO_USAGE_URL = "https://opencode.ai/workspace/{workspace_id}/usage"
TIMEOUT = 10
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0 Safari/537.36"
)

logger = logging.getLogger("opencode-usage-api")


def validate_cookie(cookie: str) -> bool:
    """基础格式校验：非空且足够长。仅提示，不强制拦截。"""
    c = (cookie or "").strip()
    return len(c) >= 8


def fetch_go_page(cookie: str, workspace_id: str) -> tuple[bool, str, int | None]:
    """拉取 go 用量页面 HTML。

    返回 (ok, html_or_reason, http_status)。
    """
    if not validate_cookie(cookie):
        return False, "Cookie 无效：请粘贴完整 auth cookie（Fe26.2** 开头）", None
    try:
        r = httpx.get(
            GO_PAGE_URL.format(workspace_id=workspace_id),
            headers={"Cookie": f"auth={cookie}", "User-Agent": UA},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
    except httpx.RequestException as e:
        return False, f"网络错误：{e}", None
    if r.status_code in (401, 403):
        return False, f"Cookie 已失效（HTTP {r.status_code}），已降级为纯本地模式", r.status_code
    if r.status_code != 200:
        return False, f"接口返回 HTTP {r.status_code}", r.status_code
    # 校验是否真正拿到数据（未登录时会被重定向/渲染空数据）
    if "rollingUsage" not in r.text and "workspaceID" not in r.text:
        return False, "页面未包含用量数据（可能未登录或 workspace 无效）", 200
    return True, r.text, 200


def fetch_usage_page(cookie: str, workspace_id: str) -> tuple[bool, str, int | None]:
    """拉取 go 订阅在线调用明细页面 HTML（/usage）。

    该页面内嵌 usage.list 序列化数组，包含**所有客户端**（opencode CLI、Trae 等）
    走 go 订阅网关（inf-go.oa-compat）的逐条调用记录。
    返回 (ok, html_or_reason, http_status)。
    """
    if not validate_cookie(cookie):
        return False, "Cookie 无效：请粘贴完整 auth cookie（Fe26.2** 开头）", None
    try:
        r = httpx.get(
            GO_USAGE_URL.format(workspace_id=workspace_id),
            headers={"Cookie": f"auth={cookie}", "User-Agent": UA},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
    except httpx.RequestException as e:
        return False, f"网络错误：{e}", None
    if r.status_code in (401, 403):
        return False, f"Cookie 已失效（HTTP {r.status_code}），已降级为纯本地模式", r.status_code
    if r.status_code != 200:
        return False, f"接口返回 HTTP {r.status_code}", r.status_code
    if "usage.list" not in r.text:
        return False, "页面未包含调用明细（可能未登录或 workspace 无效）", 200
    return True, r.text, 200
