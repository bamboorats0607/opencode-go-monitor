"""手写指数退避重试策略（红线：禁止 tenacity，只用标准库）。

用于在线拉取（go 用量页）与 API 请求的失败重试：
- 401/403（cookie 失效/权限）绝不重试，重试无意义；
- 5xx（>=500）与网络错误（status_code is None）按指数退避重试，最多 5 次；
- retry_call 为通用包装：兼容 usage_api.fetch_go_page 的 (ok, payload, status)
  三元组契约，返回 (ok, result_or_error)，纯函数式、可独立单测。
"""
import time
from collections.abc import Callable
from typing import Any

# 各次重试前的等待秒数（attempt 0 → 1s, 1 → 2s, …，上限 16s）
BACKOFF_SECONDS: list[float] = [1.0, 2.0, 4.0, 8.0, 16.0]

# 可重试判定回调：输入 (status_code, exception)，返回是否可重试
Retryable = Callable[[int | None, BaseException | None], bool]


def should_retry(status_code: int | None, attempt: int) -> bool:
    """按状态码 + 尝试次数判定是否应重试（attempt 从 0 起，<5 才允许重试）。

    - 401/403 → 永不重试（cookie 失效类问题）
    - 5xx 或 status_code is None（网络错误）→ attempt < 5 时重试
    - 其余（其他 4xx / 2xx）→ 不重试
    """
    if status_code in (401, 403):
        return False
    if status_code is None or status_code >= 500:
        return attempt < 5
    return False


def backoff_seconds(attempt: int) -> float:
    """attempt 从 0 起 → 返回对应等待秒数（1/2/4/8/16，负值与越界都收敛到合法区间）。"""
    idx = min(max(attempt, 0), len(BACKOFF_SECONDS) - 1)
    return BACKOFF_SECONDS[idx]


def _default_retryable(status: int | None, exc: BaseException | None) -> bool:
    """默认判定：异常（网络错误）与 5xx/网络状态码可重试；401/403 与其余 4xx 不重试。"""
    if exc is not None:
        return True  # 抛出的异常一律视为可重试网络错误
    return should_retry(status, 0)


def retry_call(
    fn: Callable[[], Any],
    retryable: Retryable | None = None,
    max_attempts: int = 5,
    on_retry: Callable[[int, float, Any], None] | None = None,
) -> tuple[bool, Any]:
    """通用指数退避重试包装。

    fn 契约（三选一）：
      - 返回 (ok: bool, payload, status_code: int | None) 三元组 —— 与 usage_api.fetch_go_page 一致；
      - 抛异常 —— 视为网络错误，按 retryable 判定（默认可重试）；
      - 其他返回值 —— 直接视为成功。

    返回 (True, result) 或 (False, error)：
      - 成功 → (True, payload)
      - 重试耗尽 / 判定不可重试 → (False, payload 或异常对象)

    on_retry(attempt, delay, info)：每次决定重试后、sleep 前回调
    （info 为异常对象或 (status, payload)），回调自身异常被吞掉，不影响重试流程。
    """
    if max_attempts <= 0:
        return False, ValueError(f"max_attempts 必须 >= 1，收到 {max_attempts}")
    if retryable is None:
        retryable = _default_retryable
    last_error: Any = None
    for attempt in range(max_attempts):
        try:
            result = fn()
        except Exception as exc:  # 网络错误等异常，按 retryable 判定是否重试
            last_error = exc
            if retryable(None, exc) and attempt < max_attempts - 1:
                delay = backoff_seconds(attempt)
                _notify(on_retry, attempt, delay, exc)
                time.sleep(delay)
                continue
            return False, exc
        # 适配 (ok, payload, status) 三元组（usage_api 风格）
        if isinstance(result, tuple) and len(result) == 3 and isinstance(result[0], bool):
            ok, payload, status = result
            if ok:
                return True, payload
            if retryable(status, None) and attempt < max_attempts - 1:
                delay = backoff_seconds(attempt)
                _notify(on_retry, attempt, delay, (status, payload))
                time.sleep(delay)
                continue
            return False, payload
        return True, result
    return False, last_error  # max_attempts 次全部被 retryable 拒绝/耗尽后的兜底


def _notify(
    on_retry: Callable[[int, float, Any], None] | None,
    attempt: int,
    delay: float,
    info: Any,
) -> None:
    """安全调用 on_retry 回调：回调异常不得影响重试主流程。"""
    if on_retry is None:
        return
    try:
        on_retry(attempt, delay, info)
    except Exception:
        pass
