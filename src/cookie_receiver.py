"""本地 HTTP 接收端：接收 Edge 扩展捕获的 opencode.ai auth cookie。

扩展通过 chrome.cookies API 直读 cookie（浏览器自己解密，扩展合法访问），
POST 到本端 http://127.0.0.1:8900/capture，程序保存并触发用量拉取。

与 proxy_capture 是两种互补方式（扩展=直读；代理=拦截请求头）。
均不涉及解密浏览器数据库、不提权。
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8900

logger = logging.getLogger("opencode-ext")

# 捕获到新 cookie 时的回调：cookie_str -> None
_on_cookie_callback = None


def set_cookie_callback(cb):
    """注册 cookie 接收回调（调用方需保证线程安全，UI 侧用信号槽）。"""
    global _on_cookie_callback
    _on_cookie_callback = cb


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, status: int, obj: dict):
        """统一 JSON 响应：HTTP/1.1 必须带 Content-Length，否则客户端 fetch 会挂起。"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)})
            return

        # 调试端点：扩展埋点日志 → 程序日志（排查扩展卡住问题）
        if self.path == "/debug":
            logger.info("[ext] %s", str(data.get("msg", "")))
            self._json_response(200, {"ok": True})
            return

        if self.path != "/capture":
            self._json_response(404, {"ok": False, "error": "not found"})
            return
        cookie = str(data.get("cookie", "")).strip()
        if not cookie:
            self._json_response(400, {"ok": False, "error": "empty cookie"})
            return
        if _on_cookie_callback:
            try:
                _on_cookie_callback(cookie)
            except Exception:
                pass
        self._json_response(200, {"ok": True})


class CookieReceiver:
    """本地接收端封装：start()/stop()。监听 127.0.0.1:8900。"""

    def __init__(self, port: int = PORT):
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self._server:
            return True
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError:
            self._server = None
            return False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def running(self) -> bool:
        return self._server is not None
