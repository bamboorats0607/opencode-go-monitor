"""本地 HTTP 代理：自动捕获 opencode.ai 的 auth cookie。

原理：用户在浏览器代理设置指向本代理（如 127.0.0.1:8899），
访问 https://opencode.ai/workspace/{id}/go 时，请求头会携带
opencode.ai 域的 auth cookie。代理在转发的同时把 auth cookie
提取出来回调给主程序保存——无需解密浏览器数据库、无需提权。

仅捕获 opencode.ai 域 cookie；其他流量原样转发，不做任何中间人解密。
"""
import http.server
import threading
import urllib.parse
import urllib.request
import socketserver
import logging

logger = logging.getLogger("opencode-proxy")

TARGET_DOMAIN = "opencode.ai"
AUTH_COOKIE_NAME = "auth"

# 捕获到新 cookie 时的回调：cookie_str -> None
_on_cookie_callback = None


def set_cookie_callback(cb):
    """注册 cookie 捕获回调（主线程/UI 线程安全由调用方保证）。"""
    global _on_cookie_callback
    _on_cookie_callback = cb


def _extract_auth_cookie(header_value: str) -> str | None:
    """从 Cookie 请求头中提取 opencode.ai 的 auth cookie 值。"""
    if not header_value:
        return None
    for part in header_value.split(";"):
        part = part.strip()
        if part.startswith(AUTH_COOKIE_NAME + "="):
            return part[len(AUTH_COOKIE_NAME) + 1:].strip()
    return None


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏

    def _handle(self, method: str):
        """HTTP 明文代理（少数情况）与 CONNECT 隧道共用入口。"""
        # 解析绝对 URL（HTTP 代理请求形如 GET http://host/path）
        try:
            url = urllib.parse.urlsplit(self.path)
        except Exception:
            self.send_error(400)
            return

        # 捕获 opencode.ai 域请求的 auth cookie
        host = url.hostname or ""
        cookie_val = _extract_auth_cookie(self.headers.get("Cookie", ""))
        if TARGET_DOMAIN in host and cookie_val and _on_cookie_callback:
            try:
                _on_cookie_callback(cookie_val)
            except Exception:
                pass

        # 转发（缺 UA 时补浏览器 UA，避免被边缘风控拦截）
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("proxy-connection", "connection")}
        if not any(k.lower() == "user-agent" for k in headers):
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/150.0 Safari/537.36"
            )
        req = urllib.request.Request(
            self.path,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp.read())))
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            try:
                self.send_error(502, "Bad Gateway")
            except Exception:
                pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_CONNECT(self):
        """HTTPS 隧道。CONNECT 阶段的请求行只有 host:port，
        无法读到 cookie，故仅透明转发（隧道内流量原样透传）。"""
        try:
            host, _, port = self.path.partition(":")
            port = int(port or 443)
        except Exception:
            self.send_error(400)
            return
        try:
            self.send_response(200, "Connection Established")
            self.end_headers()
        except Exception:
            return
        try:
            import socket
            upstream = socket.create_connection((host, port), timeout=15)
        except Exception:
            try:
                self.send_error(502)
            except Exception:
                pass
            return
        self.connection.settimeout(60)
        upstream.settimeout(60)
        # 双向透传
        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
        t1 = threading.Thread(target=pump, args=(self.connection, upstream), daemon=True)
        t2 = threading.Thread(target=pump, args=(upstream, self.connection), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            upstream.close()
        except Exception:
            pass


class ThreadingProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LocalProxy:
    """本地代理封装：start()/stop()。监听 127.0.0.1 指定端口。"""

    def __init__(self, port: int = 8899):
        self.port = port
        self._server: ThreadingProxyServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self._server:
            return True
        try:
            self._server = ThreadingProxyServer(("127.0.0.1", self.port), ProxyHandler)
        except OSError:
            logger.error("端口 %d 被占用，代理启动失败", self.port)
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
