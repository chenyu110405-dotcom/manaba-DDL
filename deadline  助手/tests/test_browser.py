"""Synthetic HTML and fake HTTP responses; no network, credentials or private text."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from manaba_deadline.browser import BrowserTransport, BrowserUnavailable, FetchError, LoginRequired, UnsafeURL, validate_read_url
from manaba_deadline.config import DEFAULT_URL, ScraperConfig


COURSE_URL = "https://manaba.ryukoku.ac.jp/ct/course_123_report"
HOME_HTML = '<html><a href="/ct/logout">ログアウト</a><div>コース一覧</div></html>'
ASSIGNMENT_HTML = '<html><h1>演習課題</h1><p>脱敏テスト用の本文。</p></html>'
REAL_ASYNC_SLEEP = asyncio.sleep


class FakeResponse:
    def __init__(self, html=ASSIGNMENT_HTML, status=200, headers=None, url=COURSE_URL):
        self.status = status
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.url = url
        self.html = html
        self.disposed = False

    async def text(self):
        return self.html

    async def dispose(self):
        self.disposed = True


class FakeRequest:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.active -= 1
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakePage:
    def __init__(self, html=HOME_HTML, url=DEFAULT_URL):
        self.html = html
        self.url = url
        self.closed = False
        self.gotos = []

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.gotos.append((url, kwargs))

    async def content(self):
        return self.html


class FakeContext:
    def __init__(self, responses=(), page=None):
        self.request = FakeRequest(responses)
        self.page = page or FakePage()
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True

    def set_default_timeout(self, timeout):
        self.timeout = timeout


class ReadURLTests(unittest.TestCase):
    def test_only_confirmed_read_paths(self):
        for suffix in (
            "home", "home_course", "home_library_query___", "course_123", "course_123_query",
            "course_123_survey", "course_123_report", "course_123_project", "course_123_report_456",
            "course_123_query_456", "course_123_survey_456", "home_library_query___?page=2&offset=10",
        ):
            with self.subTest(suffix=suffix):
                url = f"https://manaba.ryukoku.ac.jp/ct/{suffix}"
                self.assertEqual(validate_read_url(url), url)

    def test_mutation_external_entry_and_unconfirmed_details_blocked(self):
        for suffix in (
            "logout", "course_123_query_456_start", "course_123_query_456?start=1", "course_123_report_456?submit=1",
            "course_123_project_456", "course_123_drill_456", "course_123_report?delete=1",
            "home_library_query___?page=1&page=2", "home_library_query___?page=2%26submit%3D1",
            "home_library_query___?page", "home#fragment", "home?account=secret", "../ct/home",
        ):
            with self.subTest(suffix=suffix), self.assertRaises(UnsafeURL):
                validate_read_url(f"https://manaba.ryukoku.ac.jp/ct/{suffix}")
        for url in (
            "http://manaba.ryukoku.ac.jp/ct/home", "https://evil.example/ct/home",
            "https://manaba.ryukoku.ac.jp:443/ct/home", "https://user:secret@manaba.ryukoku.ac.jp/ct/home",
            "https://manaba.ryukoku.ac.jp/ct/home\n", "https://manaba.ryukoku.ac.jp/ct/%68ome", "https://[bad",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeURL):
                validate_read_url(url)


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Keep tests deterministic and fast while preserving the throttle logic.
        self.sleep_patch = patch("manaba_deadline.browser.asyncio.sleep", new_callable=AsyncMock)
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def transport(self, responses, **kwargs):
        context = FakeContext(responses, **kwargs)
        return BrowserTransport(ScraperConfig(), context=context), context

    async def test_get_only_with_manual_redirect_and_disposal(self):
        response = FakeResponse()
        browser, context = self.transport([response])
        async with browser:
            self.assertEqual(await browser.get(COURSE_URL), ASSIGNMENT_HTML)
        self.assertEqual(browser.pages_read, 1)
        self.assertTrue(response.disposed)
        self.assertFalse(context.closed, "borrowed contexts belong to their caller")
        self.assertEqual(context.request.calls[0], (COURSE_URL, {"timeout": 30000, "max_redirects": 0, "fail_on_status_code": False}))

    async def test_unsafe_url_stops_before_network(self):
        browser, context = self.transport([])
        with self.assertRaises(UnsafeURL):
            await browser.get("https://manaba.ryukoku.ac.jp/ct/course_123_query_456?start=1")
        self.assertEqual(context.request.calls, [])

    async def test_login_redirect_never_followed_or_disclosed(self):
        response = FakeResponse(status=302, headers={"location": "https://sso.example/login?token=private"})
        browser, context = self.transport([response])
        with self.assertRaises(LoginRequired) as error:
            await browser.get(COURSE_URL)
        self.assertEqual(len(context.request.calls), 1)
        self.assertNotIn("private", str(error.exception))
        self.assertTrue(response.disposed)

    async def test_same_origin_login_redirect_never_followed(self):
        browser, context = self.transport([FakeResponse(status=302, headers={"location": "/ct/login"})])
        with self.assertRaises(LoginRequired):
            await browser.get(COURSE_URL)
        self.assertEqual(len(context.request.calls), 1)

    async def test_same_origin_mutation_redirect_blocked(self):
        browser, context = self.transport([FakeResponse(status=302, headers={"location": "/ct/course_123_report_456_submit"})])
        with self.assertRaises(UnsafeURL):
            await browser.get(COURSE_URL)
        self.assertEqual(len(context.request.calls), 1)

    async def test_allowed_redirect_is_manual_and_throttled(self):
        response = FakeResponse(status=302, headers={"location": "/ct/home_library_query___"})
        browser, context = self.transport([response, FakeResponse(url="https://manaba.ryukoku.ac.jp/ct/home_library_query___")])
        with patch("manaba_deadline.browser.time.monotonic", side_effect=[100, 100.2, 100.75]):
            await browser.get(COURSE_URL)
        self.assertEqual(len(context.request.calls), 2)
        self.sleep.assert_awaited_once()
        self.assertAlmostEqual(self.sleep.await_args.args[0], 0.55)
        self.assertEqual(browser.pages_read, 1)

    async def test_redirect_loop_stops(self):
        browser, context = self.transport([FakeResponse(status=302, headers={"location": COURSE_URL})])
        with self.assertRaises(FetchError):
            await browser.get(COURSE_URL)
        self.assertEqual(len(context.request.calls), 1)

    async def test_concurrent_reads_do_not_overlap(self):
        browser, context = self.transport([])
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def slow_get(url, **kwargs):
            calls.append(url)
            entered.set()
            await release.wait()
            return FakeResponse()

        context.request.get = slow_get
        first = asyncio.create_task(browser.get(COURSE_URL))
        await entered.wait()
        second = asyncio.create_task(browser.get(COURSE_URL))
        await REAL_ASYNC_SLEEP(0)
        self.assertEqual(len(calls), 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(calls), 2)

    async def test_large_response_is_disposed_without_parsing(self):
        response = FakeResponse(headers={"content-type": "text/html", "content-length": str(9 * 1024 * 1024)})
        browser, _ = self.transport([response])
        with self.assertRaises(FetchError):
            await browser.get(COURSE_URL)
        self.assertTrue(response.disposed)
        self.assertEqual(browser.pages_read, 0)

    async def test_login_forms_and_expiry_detected(self):
        for html in (
            '<html><input type="password" name="password"></html>',
            '<html><meta http-equiv="refresh" content="0; url=/ct/login"></html>',
            '<html>再度ログインしてください</html>',
            '<html><title>manaba - ログイン</title><button>ログイン</button></html>',
            '<html><form action="/sso/login"><button>ログイン</button></form></html>',
        ):
            with self.subTest(html=html):
                browser, _ = self.transport([FakeResponse(html=html)])
                with self.assertRaises(LoginRequired):
                    await browser.get(COURSE_URL)
                self.assertEqual(browser.pages_read, 0)

    async def test_home_requires_logout_confirmation(self):
        browser, _ = self.transport([FakeResponse(html="<html>読み込み中</html>", url=DEFAULT_URL)])
        with self.assertRaises(LoginRequired):
            await browser.get(DEFAULT_URL)
        browser, _ = self.transport([FakeResponse(html=HOME_HTML, url=DEFAULT_URL)])
        self.assertEqual(await browser.get(DEFAULT_URL), HOME_HTML)

    async def test_http_auth_nonhtml_and_network_errors_are_safe(self):
        for response, error_type in (
            (FakeResponse(status=401), LoginRequired), (FakeResponse(status=403), LoginRequired),
            (FakeResponse(status=500), FetchError), (FakeResponse(headers={"content-type": "application/pdf"}), FetchError),
            (RuntimeError("cookie=private; URL=private"), FetchError),
        ):
            with self.subTest(response=response):
                browser, _ = self.transport([response])
                with self.assertRaises(error_type) as error:
                    await browser.get(COURSE_URL)
                self.assertNotIn("private", str(error.exception))

    async def test_unentered_browser_has_actionable_error(self):
        browser = BrowserTransport(ScraperConfig())
        with self.assertRaises(BrowserUnavailable):
            await browser.get(COURSE_URL)

    async def test_normal_login_requires_authenticated_school_home(self):
        browser, context = self.transport([], page=FakePage())
        await browser.wait_for_login()
        self.assertEqual(context.page.gotos[0][0], DEFAULT_URL)
        self.assertEqual(context.request.calls, [], "login uses user-visible page only")

    async def test_login_rejects_closed_window_and_times_out(self):
        page = FakePage()
        page.closed = True
        browser, _ = self.transport([], page=page)
        with self.assertRaises(LoginRequired):
            await browser.wait_for_login()
        browser, _ = self.transport([], page=FakePage(html='<input type="password">', url="https://sso.example/login"))
        with patch("manaba_deadline.browser.time.monotonic", side_effect=[0, 301]):
            with self.assertRaises(LoginRequired):
                await browser.wait_for_login()

    async def test_login_wait_is_finite_positive(self):
        browser, _ = self.transport([])
        for timeout in (0, -1, float("inf"), float("nan"), True, "300"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await browser.wait_for_login(timeout)

    async def test_headless_transport_relaunches_headed_for_user_login(self):
        headless_context = FakeContext()
        headed_context = FakeContext()
        headless_browser = SimpleNamespace(new_context=AsyncMock(return_value=headless_context), close=AsyncMock())
        headed_browser = SimpleNamespace(new_context=AsyncMock(return_value=headed_context), close=AsyncMock())
        chromium = SimpleNamespace(launch=AsyncMock(side_effect=[headless_browser, headed_browser]))
        playwright = SimpleNamespace(chromium=chromium, stop=AsyncMock())
        factory = SimpleNamespace(start=AsyncMock(return_value=playwright))
        module = SimpleNamespace(async_playwright=lambda: factory)
        with patch.dict("sys.modules", {"playwright.async_api": module}):
            async with BrowserTransport(ScraperConfig(headless=True)) as browser:
                await browser.wait_for_login()
        self.assertEqual(chromium.launch.await_args_list[0].kwargs, {"headless": True})
        self.assertEqual(chromium.launch.await_args_list[1].kwargs, {"headless": False})
        self.assertTrue(headless_context.closed)
        self.assertTrue(headed_context.closed)
        context_args = headed_browser.new_context.await_args.kwargs
        self.assertNotIn("storage_state", context_args)
        self.assertEqual(context_args["service_workers"], "block")
        self.assertFalse(context_args["accept_downloads"])


if __name__ == "__main__":
    unittest.main()
