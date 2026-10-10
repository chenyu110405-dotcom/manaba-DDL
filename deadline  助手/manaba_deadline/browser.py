"""Ephemeral normal login plus strict, throttled, read-only HTTP page retrieval.

The application's Chromium session is independent of Codex and other browsers.
Scraping uses BrowserContext.request.get, sharing only this context's cookies and
never evaluating an assignment page's JavaScript or submitting its forms.
"""

from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import math
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

from .config import MANABA_HOST, ScraperConfig


class TransportError(RuntimeError):
    """Base error: all displayed messages intentionally exclude private input."""


class LoginRequired(TransportError):
    def __init__(self, message: str = "登录已失效或尚未登录，请在打开的浏览器中由本人重新登录。") -> None:
        super().__init__(message)


class BrowserUnavailable(TransportError):
    pass


class UnsafeURL(TransportError):
    def __init__(self) -> None:
        super().__init__("已跳过未经确认的页面入口；仅允许龙谷大学 manaba 的已知只读页面。")


class FetchError(TransportError):
    pass


_COURSE_PATH = re.compile(r"/ct/course_[0-9]+(?:_(?:query|survey|report|project))?\Z")
_DETAIL_PATH = re.compile(r"/ct/course_[0-9]+_(?:query|survey|report)_[0-9]+\Z")
_HOME_PATH = re.compile(r"/ct/home_library_query_*\Z")
_QUERY_KEYS = frozenset({"page", "pages", "offset", "start", "limit", "courseyear", "coursestatus", "listtype"})
_LOGIN_PATH = re.compile(r"(?:login|signin|sign-in|auth|sso|shibboleth|saml|session)", re.IGNORECASE)
_MAX_HTML_BYTES = 8 * 1024 * 1024


def validate_read_url(url: str) -> str:
    """Validate an exact GET destination; an arbitrary anchor is never authorization."""
    if not isinstance(url, str) or not url or url != url.strip() or any(ord(char) < 32 for char in url):
        raise UnsafeURL()
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname != MANABA_HOST
            or parts.netloc.lower() != MANABA_HOST
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
            or parts.fragment
        ):
            raise UnsafeURL()
    except ValueError:
        raise UnsafeURL() from None
    if parts.path not in {"/ct/home", "/ct/home_course"} and not (
        _HOME_PATH.fullmatch(parts.path) or _COURSE_PATH.fullmatch(parts.path) or _DETAIL_PATH.fullmatch(parts.path)
    ):
        raise UnsafeURL()
    # Pagination/filter keys are list navigation only. Detail endpoints receive
    # no arguments, so e.g. a quiz's ?start=1 can never be treated as pagination.
    if _DETAIL_PATH.fullmatch(parts.path) and parts.query:
        raise UnsafeURL()
    try:
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise UnsafeURL() from None
    if len(query) > 12 or any(
        key not in _QUERY_KEYS or not re.fullmatch(r"[A-Za-z0-9_-]{0,40}", value)
        for key, value in query
    ):
        raise UnsafeURL()
    if len({key for key, _ in query}) != len(query):
        raise UnsafeURL()
    return url


class _AuthDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.logout = False
        self.password = False
        self.login_redirect = False
        self.login_form = False
        self._anchor: str | None = None
        self._in_title = False
        self._title = ""
        self._ignore = 0
        self.words: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style"}:
            self._ignore += 1
        if tag == "input" and (values.get("type") or "").lower() == "password":
            self.password = True
        if tag == "title":
            self._in_title = True
        if tag == "form":
            self.login_form = self.login_form or bool(_LOGIN_PATH.search(values.get("action") or ""))
        if tag == "a":
            self._anchor = ""
            href = values.get("href") or ""
            try:
                if urlsplit(href).path == "/ct/logout":
                    self.logout = True
            except ValueError:
                pass
        if tag == "meta" and (values.get("http-equiv") or "").lower() == "refresh":
            self.login_redirect = bool(_LOGIN_PATH.search(values.get("content") or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._ignore = max(0, self._ignore - 1)
        if tag == "a":
            if self._anchor is not None and self._anchor.strip().lower() in {"ログアウト", "logout", "log out"}:
                self.logout = True
            self._anchor = None
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignore:
            return
        if self._anchor is not None:
            self._anchor += data
        if self._in_title:
            self._title += data
        self.words.append(data)

    @property
    def login(self) -> bool:
        text = " ".join(self.words)
        expired = any(value in text for value in ("セッションの有効期限", "ログインし直してください", "再度ログインしてください"))
        login_title = bool(re.fullmatch(r"\s*(?:manaba\s*[-:：]\s*)?(?:ログイン|login|sign in)\s*", self._title, re.IGNORECASE))
        return self.password or self.login_redirect or expired or ((self.login_form or login_title) and not self.logout)


def _auth_document(html: str) -> _AuthDocument:
    document = _AuthDocument()
    document.feed(html)
    return document


class BrowserTransport:
    """Own one incognito context for an entire sync/watch session.

    ``context`` is a test/embedding injection point. A borrowed context is never
    closed here; no browser cookie store, profile or hidden storage is imported.
    """

    def __init__(self, config: ScraperConfig, *, context: Any = None) -> None:
        self.config = config
        self._context = context
        self._borrowed = context is not None
        self._playwright: Any = None
        self._browser: Any = None
        self._login_page: Any = None
        self._launched_headless = config.headless
        self._lock = asyncio.Lock()
        self._last_request_at: float | None = None
        self._pages_read = 0

    @property
    def pages_read(self) -> int:
        """Count successful HTML pages for caller's per-sync counter deltas."""
        return self._pages_read

    async def __aenter__(self) -> BrowserTransport:
        if self._context is not None:
            return self
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise BrowserUnavailable("缺少 Playwright。请先安装项目依赖，再运行 python -m playwright install chromium。") from None
        try:
            self._playwright = await async_playwright().start()
            await self._launch(self.config.headless)
        except Exception:
            await self._cleanup()
            raise BrowserUnavailable("浏览器启动失败。请运行 python -m playwright install chromium，并在桌面环境启动程序。") from None
        return self

    async def _launch(self, headless: bool) -> None:
        self._browser = await self._playwright.chromium.launch(headless=headless)
        self._context = await self._browser.new_context(
            locale="ja-JP", timezone_id="Asia/Tokyo", service_workers="block", accept_downloads=False
        )
        self._context.set_default_timeout(self.config.timeout_seconds * 1000)
        self._launched_headless = headless

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._borrowed:
            return
        for resource, method in ((self._context, "close"), (self._browser, "close"), (self._playwright, "stop")):
            if resource is not None:
                try:
                    await getattr(resource, method)()
                except Exception:
                    pass
        self._context = self._browser = self._playwright = self._login_page = None

    async def wait_for_login(self, timeout_seconds: float = 300) -> None:
        """Open the campus home and wait for the user to finish normal UI login.

        Login is the only browser navigation; no account or password is read.
        Authentication requires a school home with a logout DOM link. Scraper
        requests never follow SSO or login redirects and never press quiz Start.
        """
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("登录等待时间必须为正数。")
        if self._context is None:
            raise BrowserUnavailable("浏览器尚未启动，请先进入 BrowserTransport 会话。")
        async with self._lock:
            try:
                if self._launched_headless and not self._borrowed:
                    await self._context.close()
                    await self._browser.close()
                    self._context = self._browser = None
                    await self._launch(False)
                if self._login_page is None or self._login_page.is_closed():
                    self._login_page = await self._context.new_page()
                await self._login_page.goto(self.config.url, wait_until="domcontentloaded", timeout=self.config.timeout_seconds * 1000)
            except Exception:
                raise BrowserUnavailable("登录窗口打开失败，请检查网络并重新启动登录。") from None
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                try:
                    if self._login_page.is_closed():
                        raise LoginRequired("登录窗口已关闭，请重新打开并由本人登录。")
                    current = urlsplit(self._login_page.url)
                    on_home = current.scheme == "https" and current.hostname == MANABA_HOST and current.path in {"/ct/home", "/ct/home_course"}
                    if on_home:
                        document = _auth_document(await self._login_page.content())
                        if document.logout and not document.login:
                            return
                except LoginRequired:
                    raise
                except Exception:
                    # Cross-origin login transitions can briefly invalidate a DOM read.
                    pass
                await asyncio.sleep(0.5)
            raise LoginRequired("登录等待超时，请重新打开登录并由本人完成校园认证。")

    async def _throttle(self) -> None:
        if self._last_request_at is not None:
            remaining = self.config.throttle_seconds - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()

    async def get(self, url: str) -> str:
        """Read one allowed HTML URL with cookies from this session only."""
        current = validate_read_url(url)
        if self._context is None:
            raise BrowserUnavailable("浏览器尚未启动，请先进入 BrowserTransport 会话。")
        async with self._lock:
            visited: set[str] = set()
            for _ in range(6):
                if current in visited:
                    raise FetchError("页面跳转形成循环，请稍后重试同步。")
                visited.add(current)
                await self._throttle()
                try:
                    response = await self._context.request.get(
                        current, timeout=self.config.timeout_seconds * 1000,
                        max_redirects=0, fail_on_status_code=False,
                    )
                except Exception:
                    raise FetchError("页面读取失败，请检查网络后重试；同步保留上次结果。") from None
                try:
                    response_url = getattr(response, "url", current)
                    try:
                        validate_read_url(response_url)
                    except UnsafeURL:
                        raise LoginRequired() from None
                    status = response.status
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    if status in {301, 302, 303, 307, 308}:
                        location = headers.get("location")
                        if not location:
                            raise FetchError("页面跳转缺少目标，请稍后重试同步。")
                        next_url = urljoin(current, location)
                        parts = urlsplit(next_url)
                        if parts.hostname != MANABA_HOST or _LOGIN_PATH.search(parts.path):
                            raise LoginRequired()
                        current = validate_read_url(next_url)
                        continue
                    if status in {401, 403}:
                        raise LoginRequired()
                    if status < 200 or status >= 300:
                        raise FetchError(f"校园平台暂时无法读取页面（HTTP {status}），请稍后重试。")
                    content_type = headers.get("content-type", "text/html").split(";", 1)[0].strip().lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        raise FetchError("页面返回了非 HTML 内容，已停止解析。")
                    length = headers.get("content-length", "")
                    if length.isdigit() and int(length) > _MAX_HTML_BYTES:
                        raise FetchError("页面内容过大，已停止解析。")
                    html = await response.text()
                    if len(html.encode("utf-8")) > _MAX_HTML_BYTES:
                        raise FetchError("页面内容过大，已停止解析。")
                    document = _auth_document(html)
                    if document.login or (urlsplit(current).path in {"/ct/home", "/ct/home_course"} and not document.logout):
                        raise LoginRequired()
                    self._pages_read += 1
                    return html
                except TransportError:
                    raise
                except Exception:
                    raise FetchError("页面内容读取失败，请稍后重试同步。") from None
                finally:
                    try:
                        await response.dispose()
                    except Exception:
                        pass
            raise FetchError("页面跳转过多，请重新登录后重试同步。")
