"""端口管理：主监听退避池探测 + 扩展固定口预检。

背景：主代理（LocalProxy）端口被占时需自动换后续端口（8899→8910→…），
而扩展接收端（CookieReceiver，8900）是 Edge 扩展硬编码的 POST 目标，
必须固定、不参与退避——启动前预检一次，被占则调用方硬报错退出，
避免扩展静默失效、cookie 悄悄采集不到。
"""
import socket
import sys


class PortManager:
    """端口探测管理器：遍历退避池找可用端口，并对扩展固定口做启动预检。"""

    def __init__(self, pool: list[int], extension_port: int) -> None:
        self.pool = list(pool)  # 复制一份，防外部修改退避池
        self.extension_port = extension_port

    def find_free_port(self) -> int | None:
        """返回退避池中第一个可绑定端口；全部被占返回 None（调用方降级/报错）。"""
        for port in self.pool:
            if self.is_port_free(port):
                return port
        return None

    def precheck_extension(self) -> bool:
        """预检扩展固定口是否可绑定；不可用返回 False（调用方应硬报错退出）。"""
        return self.is_port_free(self.extension_port)

    @staticmethod
    def is_port_free(port: int) -> bool:
        """探测 127.0.0.1:port 是否可绑定（socket bind 测试，测完立即释放）。

        Windows 注意：SO_REUSEADDR 允许两个都开了该选项的 socket 互相抢占
        绑定（劫持语义），导致"被占用却探测为空闲"的误判——而本项目代理
        自身就是 SO_REUSEADDR 监听（HTTPServer allow_reuse_address）。
        故 Windows 用互斥的 SO_EXCLUSIVEADDRUSE：绑定成功即权威判定空闲；
        其余平台用 SO_REUSEADDR（跳过 TIME_WAIT 误判）。
        两种路径下绑定后再追加一次 connect 双保险（POSIX REUSEADDR 可绕过
        TIME_WAIT 绑上无监听端口，此时 connect 必失败，仍判空闲）。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False  # 绑定失败 → 端口被占用
        finally:
            s.close()  # 探测完立即关闭释放端口（try/finally 保证）
        try:
            probe = socket.create_connection(("127.0.0.1", port), timeout=0.2)
        except OSError:
            return True  # 连不上 → 确实空闲
        probe.close()
        return False
