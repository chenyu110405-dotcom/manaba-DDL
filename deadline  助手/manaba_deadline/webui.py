"""Small loopback-only dashboard; no passwords, exports or persistent cache."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import ConfigError
from .service import DeadlineService


import sys

_PACKAGE_DIR = Path(__file__).resolve().parent
_BASE_DIR = Path(getattr(sys, "_MEIPASS", _PACKAGE_DIR))
_PAGE_PATH = (_BASE_DIR / "dashboard.html") if (_BASE_DIR / "dashboard.html").is_file() else (_PACKAGE_DIR / "dashboard.html")
_SCRIPT_PATH = (_BASE_DIR / "dashboard.js") if (_BASE_DIR / "dashboard.js").is_file() else (_PACKAGE_DIR / "dashboard.js")
_AVATARS_DIR = (_BASE_DIR / "avatars") if (_BASE_DIR / "avatars").is_dir() else (_PACKAGE_DIR / "avatars")


def dashboard_page(token: str, nonce: str) -> str:
    """Reload presentation files without restarting the authenticated session."""
    page = _PAGE_PATH.read_text(encoding="utf-8")
    script = _SCRIPT_PATH.read_text(encoding="utf-8")
    return page.replace("__SCRIPT__", script).replace("__TOKEN__", token).replace("__NONCE__", nonce)


class LocalDashboard:
    """HTTP threads marshal all service work into one existing asyncio loop."""

    def __init__(self, service: DeadlineService, loop: asyncio.AbstractEventLoop, port: int = 8765) -> None:
        self.service = service
        self.loop = loop
        self.token = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(18)
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DeadlineLocal"
            sys_version = ""

            def log_message(self, *_: Any) -> None:
                # URLs and response content are not retained in request logs.
                pass

            def _headers_allowed(self) -> bool:
                return (
                    self.headers.get("Host") == dashboard.host
                    and self.headers.get("Origin", dashboard.origin) == dashboard.origin
                    and self.headers.get("Sec-Fetch-Site", "same-origin") != "cross-site"
                )

            def _authorized(self) -> bool:
                return self._headers_allowed() and hmac.compare_digest(
                    self.headers.get("X-Manaba-Token", ""), dashboard.token
                )

            def _send(self, code: int, data: Any, *, html: bool = False) -> None:
                payload = data.encode("utf-8") if html else json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" + dashboard.nonce + "'; style-src 'nonce-" + dashboard.nonce + "'; img-src blob: 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _invoke(self, callback: Any) -> Any:
                async def call() -> Any:
                    return callback()

                return asyncio.run_coroutine_threadsafe(call(), dashboard.loop).result(timeout=10)

            def do_GET(self) -> None:
                if not self._headers_allowed():
                    self._send(403, {"message_zh": "仅允许本机页面访问。"})
                    return
                path = urlsplit(self.path).path
                if path == "/":
                    try:
                        self._send(200, dashboard_page(dashboard.token, dashboard.nonce), html=True)
                    except OSError:
                        self._send(503, {"message_zh": "页面暂时无法加载，请刷新重试。"})
                    return
                if path.startswith("/avatars/") and path.endswith(".jpg"):
                    avatar_name = Path(path).name
                    avatar_file = _AVATARS_DIR / avatar_name
                    if avatar_file.is_file() and avatar_name in {"karina.jpg", "winter.jpg", "giselle.jpg", "ningning.jpg"}:
                        try:
                            payload = avatar_file.read_bytes()
                            self.send_response(200)
                            self.send_header("Content-Type", "image/jpeg")
                            self.send_header("Content-Length", str(len(payload)))
                            self.send_header("Cache-Control", "public, max-age=86400")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.send_header("Referrer-Policy", "no-referrer")
                            self.end_headers()
                            self.wfile.write(payload)
                            return
                        except (OSError, BrokenPipeError, ConnectionResetError):
                            return
                    self._send(404, {"message_zh": "头像不存在。"})
                    return
                if not self._authorized():
                    self._send(403, {"message_zh": "本地访问校验失败。"})
                    return
                try:
                    if path == "/api/status":
                        data = self._invoke(dashboard.service.status)
                    elif path == "/api/assignments":
                        data = self._invoke(dashboard.service.assignments)
                    else:
                        self._send(404, {"message_zh": "页面不存在。"})
                        return
                    self._send(200, data)
                except Exception:
                    self._send(503, {"message_zh": "本地服务暂时不可用。"})

            def do_POST(self) -> None:
                if not self._authorized():
                    self._send(403, {"message_zh": "本地访问校验失败。"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > 2048 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError
                    data = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"message_zh": "请求格式无效。"})
                    return
                path = urlsplit(self.path).path
                try:
                    if path == "/api/sync":
                        idle = self._invoke(dashboard.service.request_sync)
                        self._send(202, {"message_zh": "已请求同步。" if idle else "操作正在进行；已合并为一次后续同步。"})
                    elif path == "/api/login":
                        started = self._invoke(dashboard.service.request_login)
                        self._send(202 if started else 409, {"message_zh": "请在浏览器中正常登录本人账号。" if started else "操作正在进行，请稍后重试。"})
                    elif path == "/api/account":
                        url = data.get("url")
                        if not isinstance(url, str):
                            raise ValueError
                        future = asyncio.run_coroutine_threadsafe(dashboard.service.change_account(url), dashboard.loop)
                        future.result(timeout=30)
                        self._send(200, {"message_zh": "旧账号会话与作业已清空，请登录新账号。"})
                    else:
                        self._send(404, {"message_zh": "接口不存在。"})
                except (ConfigError, ValueError):
                    self._send(400, {"message_zh": "网页 URL 无效。第一版仅支持 https://manaba.ryukoku.ac.jp/ct/home。"})
                except FutureTimeout:
                    self._send(202, {"message_zh": "操作已排队，等待当前同步结束。"})
                except Exception:
                    self._send(503, {"message_zh": "本地服务暂时不可用。"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.origin = f"http://{self.host}"
        self.url = self.origin + "/"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
        self._thread.start()

    async def close(self) -> None:
        if self._thread is not None:
            await asyncio.to_thread(self.server.shutdown)
            await asyncio.to_thread(self._thread.join, 2)
        self.server.server_close()
