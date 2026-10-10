"""In-memory synchronization service for the CLI and a future desktop window."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import Any

from .config import ScraperConfig
from .models import Issue, SyncResult, now_tokyo_iso, sorted_assignments


class DeadlineService:
    """Own one browser session and serialize all reads/account changes.

    Private assignment data and authentication remain only in memory.  A failed
    refresh preserves the previous snapshot, explicitly marked as stale. Login
    expiry pauses timer reads until a person requests login or manual sync.
    ``scheduler_interval`` is an optional test clock override; normal callers
    use the validated configuration interval.
    """

    def __init__(
        self,
        config: ScraperConfig,
        *,
        transport: Any = None,
        adapter: Any = None,
        transport_factory: Any = None,
        adapter_factory: Any = None,
        scheduler_interval: float | None = None,
    ) -> None:
        self.config = config
        self._provided_transport = transport
        self._provided_adapter = adapter
        self._transport_factory = transport_factory
        self._adapter_factory = adapter_factory
        self._transport: Any = None
        self._adapter: Any = None
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._manual_requested = False
        self._scheduler: asyncio.Task[None] | None = None
        self._login_task: asyncio.Task[bool] | None = None
        self._interval = scheduler_interval or config.interval_seconds
        if self._interval <= 0:
            raise ValueError("同步间隔必须大于零。")
        self._last: SyncResult | None = None
        self._last_success_at: str | None = None
        self._last_sync_at: str | None = None
        self._latest_attempt_at: str | None = None
        self._state = "idle"
        self._message = "尚未同步。请打开登录窗口并使用本人的校园账号登录。"
        self._stale = True
        self._paused = False
        self._closed = False
        self._generation = 0

    async def __aenter__(self) -> DeadlineService:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _open(self) -> None:
        if self._transport is not None:
            return
        if self._provided_transport is not None:
            transport = self._provided_transport
        else:
            if self._transport_factory is None:
                from .browser import BrowserTransport

                self._transport_factory = BrowserTransport
            transport = self._transport_factory(self.config)
        if hasattr(transport, "__aenter__"):
            await transport.__aenter__()
        self._transport = transport
        if self._provided_adapter is not None:
            self._adapter = self._provided_adapter
        else:
            if self._adapter_factory is None:
                from .adapter import ManabaAdapter

                self._adapter_factory = ManabaAdapter
            self._adapter = self._adapter_factory(transport, self.config)

    async def _close_browser(self) -> None:
        transport, self._transport = self._transport, None
        self._adapter = None
        if transport is not None:
            if hasattr(transport, "__aexit__"):
                await transport.__aexit__(None, None, None)
            elif hasattr(transport, "close"):
                await transport.close()

    def status(self) -> dict[str, Any]:
        """Generic diagnostics without titles, bodies, IDs or credentials."""
        return {
            "state": self._state,
            "message_zh": self._message,
            "busy": self._lock.locked(),
            "stale": self._stale,
            "paused": self._paused,
            "last_success_at": self._last_success_at,
            "last_sync_at": self._last_sync_at,
            "latest_attempt_at": self._latest_attempt_at,
            "assignment_count": len(self._last.assignments) if self._last else 0,
            "course_count": len(self._last.courses) if self._last else 0,
            "issue_count": len(self._last.issues) if self._last else 0,
            "complete": self._last.complete if self._last else False,
            "url": self.config.url,
            "interval_seconds": self.config.interval_seconds,
            "time_zone": "Asia/Tokyo",
            "scope": self._last.scope if self._last else "weekday_timetable",
            "scope_complete": self._last.scope_complete if self._last else False,
        }

    def assignments(self, now: Any = None) -> dict[str, Any]:
        """Return a snapshot for a local UI; Japanese source content is intact."""
        return {
            "status": self.status(),
            "assignments": [
                assignment.view(now)
                for assignment in sorted_assignments(self._last.assignments, now)
            ] if self._last else [],
            "courses": [course.as_dict() for course in self._last.courses] if self._last else [],
            # Diagnostics are generic by contract, but never log this response.
            "issues": [issue.as_dict() for issue in self._last.issues] if self._last else [],
            "synced_at": self._last.synced_at if self._last else None,
        }

    @property
    def last_result(self) -> SyncResult | None:
        return self._last

    def request_sync(self) -> bool:
        """Coalesce repeated UI requests into one follow-up refresh.

        Returns False when a read/login already runs. The requested refresh
        still remains queued and runs when the service is available.
        """
        if self._closed:
            return False
        idle = not self._lock.locked()
        self._manual_requested = True
        self._event.set()
        return idle

    async def sync(self, *, manual: bool = True) -> SyncResult | None:
        if self._closed:
            return None
        if self._paused and not manual:
            return None
        if self._lock.locked():
            if manual:
                self.request_sync()
            return None
        generation = self._generation
        async with self._lock:
            # A queued account change may acquire the fair lock before this
            # caller. Discard old-session requests after that change.
            if self._closed or generation != self._generation:
                return None
            self._state = "syncing"
            self._message = "正在只读同步作业。"
            self._latest_attempt_at = now_tokyo_iso()
            try:
                await self._open()
                result = deepcopy(await self._adapter.sync())
                if not result.complete and self._last is not None and result.scope == self._last.scope:
                    current_ids = {assignment.id for assignment in result.assignments}
                    course_ids = {course.id for course in result.courses}
                    for previous in self._last.assignments:
                        if previous.id in current_ids or (result.scope_complete and previous.course_id not in course_ids):
                            continue
                        retained = deepcopy(previous)
                        retained.issues = [issue for issue in retained.issues if issue.code != "snapshot_stale"]
                        issue = Issue("snapshot_stale", "本次未重新读取此作业，显示上次数据。", retained.id, "sync")
                        retained.issues.append(issue)
                        result.assignments.append(retained)
                        result.issues.append(deepcopy(issue))
                    if not result.scope_complete:
                        result.courses.extend(deepcopy(course) for course in self._last.courses if course.id not in course_ids)
                self._last = result
                self._last_sync_at = result.synced_at or now_tokyo_iso()
                if result.complete:
                    self._last_success_at = self._last_sync_at
                self._stale = not result.complete
                self._paused = False
                self._state = "ready" if result.complete else "incomplete"
                self._message = (
                    "同步完成。"
                    if result.complete
                    else "已更新可读取的作业；本次同步不完整，未重新读取的作业可能显示上次数据，请查看提示。"
                )
                return result
            except asyncio.CancelledError:
                self._stale = True
                self._state = "idle"
                self._message = "同步已停止。"
                raise
            except Exception as exc:
                self._record_error(exc)
                return None

    def _record_error(self, exc: Exception) -> None:
        from .adapter import TimetableUnavailable
        from .browser import BrowserUnavailable, FetchError, LoginRequired

        self._stale = True
        if isinstance(exc, LoginRequired):
            self._paused = True
            self._state = "login_required"
            self._message = "登录已失效或尚未登录。请在登录窗口正常登录，再手动同步。"
        elif isinstance(exc, BrowserUnavailable):
            self._state = "browser_unavailable"
            self._message = "浏览器不可用。请安装 Chromium 浏览器后重试。"
        elif isinstance(exc, TimetableUnavailable):
            self._state = "error"
            self._message = "无法识别周一至周六课程表。请在 manaba 课程列表选择「曜日」后重新同步。已保留上次结果。"
        elif isinstance(exc, FetchError):
            self._state = "error"
            self._message = "校园页面读取失败。已保留上次结果，并标记为旧数据。"
        else:
            # An exception may contain a URL, account name or page text. Never
            # send it to the UI or stdout/stderr.
            self._state = "error"
            self._message = "同步失败。已保留上次结果，并标记为旧数据。"

    def request_login(self, timeout_seconds: float = 300) -> bool:
        """Start normal visible-browser login without collecting credentials."""
        if self._closed or self._lock.locked() or (
            self._login_task is not None and not self._login_task.done()
        ):
            return False
        self._login_task = asyncio.create_task(self.login(timeout_seconds))
        return True

    async def login(self, timeout_seconds: float = 300) -> bool:
        if self._closed or self._lock.locked():
            return False
        authenticated = False
        async with self._lock:
            self._state = "waiting_for_login"
            self._message = "请在打开的浏览器中使用本人账号正常登录；等待登录完成。"
            try:
                await self._open()
                outcome = await self._transport.wait_for_login(timeout_seconds=timeout_seconds)
                if outcome is False:
                    from .browser import LoginRequired

                    raise LoginRequired("尚未完成登录。")
                authenticated = True
                self._paused = False
                self._state = "idle"
                self._message = "登录完成，正在准备同步。"
            except asyncio.CancelledError:
                self._stale = True
                raise
            except Exception as exc:
                self._record_error(exc)
        if authenticated:
            await self.sync()
        return authenticated

    async def change_account(self, url: str) -> None:
        """Clear old data/session before opening a new per-user login context."""
        new_config = replace(self.config, url=url)
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
            await asyncio.gather(self._login_task, return_exceptions=True)
        async with self._lock:
            self._generation += 1
            self._state = "changing_account"
            self._last = None
            self._last_success_at = None
            self._last_sync_at = None
            self._latest_attempt_at = None
            self._stale = True
            self._paused = True
            self._event.clear()
            self._manual_requested = False
            try:
                await self._close_browser()
            finally:
                self.config = new_config
                self._state = "idle"
                self._message = "账号会话与旧作业已清空。请打开登录窗口，登录新账号。"

    def start_scheduler(self, *, initial_sync: bool = False) -> None:
        if self._closed:
            raise RuntimeError("服务已关闭。")
        if self._scheduler is None or self._scheduler.done():
            self._scheduler = asyncio.create_task(self._scheduler_loop())
        if initial_sync:
            self._event.set()

    async def _scheduler_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            manual = self._manual_requested
            self._event.clear()
            self._manual_requested = False
            generation = self._generation
            # A direct/manual sync or login can own the lock. Wait once rather
            # than cycling the event repeatedly while that operation runs.
            async with self._lock:
                pass
            if generation != self._generation:
                continue
            await self.sync(manual=manual)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [task for task in (self._scheduler, self._login_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            await self._close_browser()
            self._last = None
            self._last_success_at = None
            self._last_sync_at = None
            self._latest_attempt_at = None
            self._state = "closed"
            self._message = "服务已关闭，内存中的作业已清空。"
