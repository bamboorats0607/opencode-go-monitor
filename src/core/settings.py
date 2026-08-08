"""应用设置封装（基于 QSettings）。

统一管理 V2 新增的完整客制化设置项，读写全部收敛到 AppSettings，
避免 UI 层散落直接调用 QSettings。默认值即 V2 基线，改动须经讨论
（尤其 proxy_port_pool / extension_port 与端口退避策略强相关）。

存储位置（Windows）：注册表 HKCU\\Software\\opencode-go-monitor\\opencode-go-monitor，
与现有 main_window 使用的 QSettings("opencode-go-monitor", "opencode-go-monitor") 同一存储。
"""
import os
import sys

from PySide6.QtCore import QSettings

ORG = "opencode-go-monitor"
APP = "opencode-go-monitor"

# 项目根目录：开发态取源码三层上级；PyInstaller 打包态取可执行文件所在目录
# （打包后 __file__ 指向临时解压目录，日志不能写那里）
if getattr(sys, "frozen", False):
    _PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    _PROJECT_ROOT = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


class AppSettings:
    """QSettings 封装：类型化默认值 + get/set/get_all/reset_defaults。"""

    # ---- 默认值（V2 基线） ----
    DEFAULT_WORKSPACE_ID = "wrk_01KZAA0VA6E8JQNDSPW60RX3K8"
    DEFAULT_PROXY_PORT_POOL: list[int] = [8899, 8910, 8920, 8930, 8940]  # 主监听退避池
    DEFAULT_EXTENSION_PORT = 8900        # 扩展固定口：扩展硬编码目标，不进退避池
    DEFAULT_REFRESH_INTERVAL_MS = 500    # db 轮询间隔（毫秒）
    DEFAULT_GO_REFRESH_INTERVAL_S = 60   # 在线用量拉取间隔（秒）
    DEFAULT_TIME_RANGE = "24h"           # 时间范围：24h / 7d / 30d
    # 缓存告警/诊断打分阈值
    DEFAULT_THRESHOLDS: dict[str, float] = {
        "abs_min": 55,                   # 命中率绝对下限（低于即告警）
        "drop_abs": 20,                  # 命中率骤降绝对值（百分点）
        "drop_rel": 0.5,                 # 命中率骤降相对比例（50%）
        "cooldown_min": 30,              # 告警冷却（分钟）
        "suspicious_input_min": 10000,   # 未命中 input 量阈值（字符）
        "miss_ratio": 0.01,              # 冷启动未命中占比阈值
        "consecutive_critical": 3,       # 连续 critical 次数上限
    }
    DEFAULT_NOTIFICATIONS_ENABLED = True   # 托盘气泡通知开关
    DEFAULT_CLOSE_TO_TRAY = True           # 关闭窗口时驻留托盘；False=直接退出（释放单实例锁）
    DEFAULT_LOG_FILE = os.path.join(_PROJECT_ROOT, "logs", "opencode-monitor.log")
    DEFAULT_LOG_BACKUP_DAYS = 7

    # 全部设置项及其默认值（get_all / reset 的依据）
    _DEFAULTS: dict[str, object] = {
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "proxy_port_pool": DEFAULT_PROXY_PORT_POOL,
        "extension_port": DEFAULT_EXTENSION_PORT,
        "refresh_interval_ms": DEFAULT_REFRESH_INTERVAL_MS,
        "go_refresh_interval_s": DEFAULT_GO_REFRESH_INTERVAL_S,
        "time_range": DEFAULT_TIME_RANGE,
        "thresholds": DEFAULT_THRESHOLDS,
        "notifications_enabled": DEFAULT_NOTIFICATIONS_ENABLED,
        "close_to_tray": DEFAULT_CLOSE_TO_TRAY,
        "log_file": DEFAULT_LOG_FILE,
        "log_backup_days": DEFAULT_LOG_BACKUP_DAYS,
    }

    def __init__(self) -> None:
        self._qs = QSettings(ORG, APP)

    def get(self, key: str, default: object = None) -> object:
        """读取设置；key 未保存时返回默认值。

        thresholds 特殊处理：以默认值为底、用已保存值覆盖（自动补全
        旧版本缺的键、忽略非法值），保证始终返回完整 dict。
        """
        dflt = self._DEFAULTS[key] if default is None else default
        value = self._qs.value(key, dflt)
        if key == "thresholds":
            return self._merged_thresholds(value)
        return value

    def set(self, key: str, value: object) -> None:
        """写入设置（立即持久化到注册表）。"""
        self._qs.setValue(key, value)

    def get_all(self) -> dict[str, object]:
        """返回全部设置项的当前值（缺失项用默认值兜底）。"""
        return {key: self.get(key) for key in self._DEFAULTS}

    def reset_defaults(self) -> None:
        """清空全部设置，恢复出厂默认（下次 get 时回到默认值）。"""
        self._qs.clear()

    @staticmethod
    def _merged_thresholds(value: object) -> dict[str, float]:
        """阈值合并：默认值为底 + 已保存值覆盖，防御缺项/非法类型。"""
        merged = dict(AppSettings.DEFAULT_THRESHOLDS)
        if isinstance(value, dict):
            merged.update({k: v for k, v in value.items() if v is not None})
        return merged
