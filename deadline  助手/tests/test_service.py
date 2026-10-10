"""Meaningful service concurrency, login-expiry and local API boundaries."""

from __future__ import annotations

import asyncio
import http.client
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manaba_deadline.browser import FetchError, LoginRequired
from manaba_deadline.config import ConfigError, DEFAULT_URL, ScraperConfig
from manaba_deadline.models import Assignment, Course, SyncResult
from manaba_deadline.service import DeadlineService
from manaba_deadline.webui import LocalDashboard


def result(*, complete: bool = True, scope: str = "weekday_timetable", scope_complete: bool = True) -> SyncResult:
    return SyncResult(
        assignments=[Assignment(
            id="report:17", course_id="7", course_name_ja="試験用科目",
            title_ja="確認用課題", kind="report",
            source_url="https://manaba.ryukoku.ac.jp/ct/course_7_report_17",
            content_ja="試験用の説明。", content_status="loaded",
            deadline_at="2026-11-01T18:00:00+09:00", submission_status="unsubmitted",
        )],
        courses=[Course("7", "試験用科目", "https://manaba.ryukoku.ac.jp/ct/course_7")],
        issues=[], synced_at="2026-10-10T10:00:00+09:00", complete=complete, pages_read=3,
        scope=scope, scope_complete=scope_complete,
    )


class FakeTransport:
    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0
        self.logins = 0

    async def __aenter__(self):
        self.opened += 1
        return self

    async def __aexit__(self, *_):
        self.closed += 1

    async def wait_for_login(self, timeout_seconds=300):
        self.logins += 1


class FakeAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.error = None
        self.value = None
        self.block = None
        self.entered = asyncio.Event()

    async def sync(self):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.block:
                await self.block.wait()
            if self.error:
                raise self.error
            return self.value if self.value is not None else result()
        finally:
            self.active -= 1


async def until(predicate, timeout=1):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.002)
    await asyncio.wait_for(poll(), timeout)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport, self.adapter = FakeTransport(), FakeAdapter()
        self.service = DeadlineService(
            ScraperConfig(), transport=self.transport, adapter=self.adapter,
            scheduler_interval=0.025,
        )

    async def asyncTearDown(self):
        await self.service.close()

    async def test_overlap_is_coalesced_and_never_runs_simultaneously(self):
        self.adapter.block = asyncio.Event()
        first = asyncio.create_task(self.service.sync())
        await self.adapter.entered.wait()
        self.service.start_scheduler()
        self.assertIsNone(await self.service.sync())
        for _ in range(8):
            self.assertFalse(self.service.request_sync())
        await asyncio.sleep(0.01)
        self.assertEqual(self.adapter.calls, 1)
        self.adapter.block.set()
        await first
        await until(lambda: self.adapter.calls >= 2)
        self.assertEqual(self.adapter.calls, 2)
        self.assertEqual(self.adapter.max_active, 1)

    async def test_expired_login_stops_timer_retries_until_manual_request(self):
        self.adapter.error = LoginRequired()
        self.service.start_scheduler(initial_sync=True)
        await until(lambda: self.service.status()["state"] == "login_required")
        await asyncio.sleep(0.085)
        self.assertEqual(self.adapter.calls, 1)
        self.assertTrue(self.service.status()["paused"])
        self.adapter.error = None
        self.service.request_sync()
        await until(lambda: self.service.status()["state"] == "ready")
        self.assertEqual(self.adapter.calls, 2)
        self.assertFalse(self.service.status()["stale"])
        self.assertFalse(self.service.status()["paused"])

    async def test_failed_refresh_retains_snapshot_marked_stale(self):
        await self.service.sync()
        previous = self.service.status()["last_success_at"]
        self.adapter.error = FetchError("Sensitive account text must not escape")
        self.assertIsNone(await self.service.sync())
        status = self.service.status()
        self.assertTrue(status["stale"])
        self.assertEqual(status["last_success_at"], previous)
        self.assertEqual(len(self.service.assignments()["assignments"]), 1)
        self.assertNotIn("Sensitive", json.dumps(status))

    async def test_partial_refresh_retains_missing_assignments_and_courses(self):
        previous = await self.service.sync()
        self.adapter.value = SyncResult(
            [], [], [], "2026-10-10T10:05:00+09:00", False, 1,
            scope="weekday_timetable", scope_complete=False,
        )
        merged = await self.service.sync()
        self.assertEqual([assignment.id for assignment in merged.assignments], ["report:17"])
        self.assertEqual([course.id for course in merged.courses], ["7"])
        retained = merged.assignments[0]
        self.assertEqual(retained.deadline_at, "2026-11-01T18:00:00+09:00")
        self.assertEqual(retained.issues[-1].code, "snapshot_stale")
        self.assertEqual(retained.issues[-1].assignment_id, retained.id)
        self.assertEqual(merged.issues[-1].code, "snapshot_stale")
        self.assertIsNot(retained, previous.assignments[0])
        self.assertEqual(previous.assignments[0].issues, [])
        self.assertEqual(self.adapter.value.assignments, [])
        status = self.service.status()
        self.assertTrue(status["stale"])
        self.assertFalse(status["complete"])
        self.assertEqual(status["last_success_at"], "2026-10-10T10:00:00+09:00")
        self.assertEqual(status["last_sync_at"], "2026-10-10T10:05:00+09:00")
        self.assertNotIn("report:17", json.dumps(status))

    async def test_new_partial_record_wins_and_complete_refresh_removes_missing(self):
        old = result()
        old.assignments.append(Assignment(
            id="report:18", course_id="8", course_name_ja="別科目", title_ja="別課題",
            kind="report", source_url="https://manaba.ryukoku.ac.jp/ct/course_8_report_18",
            deadline_at="2026-11-02T18:00:00+09:00",
        ))
        old.courses.append(Course("8", "別科目", "https://manaba.ryukoku.ac.jp/ct/course_8"))
        self.adapter.value = old
        await self.service.sync()
        update = result(complete=False, scope_complete=False)
        update.synced_at = "2026-10-10T10:05:00+09:00"
        update.assignments[0].deadline_at = "2026-11-03T18:00:00+09:00"
        update.assignments[0].content_ja = "変更後の説明。"
        self.adapter.value = update
        merged = await self.service.sync()
        tasks = {assignment.id: assignment for assignment in merged.assignments}
        self.assertEqual(tasks["report:17"].deadline_at, "2026-11-03T18:00:00+09:00")
        self.assertEqual(tasks["report:17"].content_ja, "変更後の説明。")
        self.assertEqual(tasks["report:17"].issues, [])
        self.assertEqual(tasks["report:18"].issues[-1].code, "snapshot_stale")
        self.assertEqual(len(merged.courses), 2)
        update.complete = True
        update.scope_complete = True
        update.synced_at = "2026-10-10T10:10:00+09:00"
        completed = await self.service.sync()
        self.assertEqual(len(completed.assignments), 1)
        self.assertEqual(len(completed.courses), 1)
        self.assertFalse(self.service.status()["stale"])
        self.assertEqual(self.service.status()["last_success_at"], update.synced_at)

    async def test_partial_with_complete_scope_does_not_restore_removed_courses(self):
        old = result()
        old.assignments.append(Assignment(
            id="report:18", course_id="8", course_name_ja="旧時間割の科目",
            title_ja="旧時間割の課題", kind="report",
            source_url="https://manaba.ryukoku.ac.jp/ct/course_8_report_18",
        ))
        old.courses.append(Course("8", "旧時間割の科目", "https://manaba.ryukoku.ac.jp/ct/course_8"))
        self.adapter.value = old
        await self.service.sync()

        # Monday-Saturday discovery is reliable and now includes course 7 only.
        # Assignment discovery failed, so course 7's prior task may be retained.
        update = result(complete=False, scope_complete=True)
        update.assignments.clear()
        self.adapter.value = update
        merged = await self.service.sync()
        self.assertEqual([course.id for course in merged.courses], ["7"])
        self.assertEqual([task.id for task in merged.assignments], ["report:17"])
        self.assertTrue(merged.scope_complete)
        self.assertEqual(merged.scope, "weekday_timetable")
        self.assertEqual(merged.assignments[0].issues[-1].code, "snapshot_stale")
        self.assertEqual(len(old.assignments), 2, "retention must not mutate earlier snapshots")
        self.assertEqual(update.assignments, [], "retention must not mutate adapter output")

    async def test_partial_with_unknown_scope_preserves_confirmed_weekday_membership(self):
        old = result()
        old.assignments.append(Assignment(
            id="report:18", course_id="8", course_name_ja="土曜の科目",
            title_ja="土曜の課題", kind="report",
            source_url="https://manaba.ryukoku.ac.jp/ct/course_8_report_18",
        ))
        old.courses.append(Course("8", "土曜の科目", "https://manaba.ryukoku.ac.jp/ct/course_8"))
        self.adapter.value = old
        await self.service.sync()

        update = result(complete=False, scope_complete=False)
        update.assignments[0].title_ja = "更新された課題"
        self.adapter.value = update
        merged = await self.service.sync()
        self.assertEqual({course.id for course in merged.courses}, {"7", "8"})
        tasks = {task.id: task for task in merged.assignments}
        self.assertEqual(set(tasks), {"report:17", "report:18"})
        self.assertEqual(tasks["report:17"].title_ja, "更新された課題")
        self.assertEqual(tasks["report:17"].issues, [])
        self.assertEqual(tasks["report:18"].issues[-1].code, "snapshot_stale")
        self.assertFalse(merged.scope_complete)
        self.assertEqual(merged.scope, "weekday_timetable")
        self.assertTrue(self.service.status()["stale"])

    async def test_weekday_scope_never_merges_legacy_all_course_snapshot(self):
        for legacy_scope in ("current_courses", "current_courses_and_aggregate"):
            for scope_complete in (True, False):
                with self.subTest(legacy_scope=legacy_scope, scope_complete=scope_complete):
                    legacy = result(scope=legacy_scope)
                    legacy.assignments.append(Assignment(
                        id="report:18", course_id="8", course_name_ja="その他曜日の科目",
                        title_ja="その他曜日の課題", kind="report",
                        source_url="https://manaba.ryukoku.ac.jp/ct/course_8_report_18",
                    ))
                    legacy.courses.append(Course("8", "その他曜日の科目", "https://manaba.ryukoku.ac.jp/ct/course_8"))
                    self.adapter.value = legacy
                    await self.service.sync()
                    update = result(complete=False, scope_complete=scope_complete)
                    update.synced_at = "2026-10-10T10:05:00+09:00"
                    update.assignments[0].id = "report:99"
                    update.assignments[0].source_url = "https://manaba.ryukoku.ac.jp/ct/course_7_report_99"
                    self.adapter.value = update
                    merged = await self.service.sync()
                    # Even a legacy task for the same course is not a verified
                    # weekday-scope snapshot and must not be resurrected.
                    self.assertEqual([task.id for task in merged.assignments], ["report:99"])
                    self.assertEqual([course.id for course in merged.courses], ["7"])
                    self.assertFalse(any(issue.code == "snapshot_stale" for issue in merged.issues))
                    self.assertEqual(merged.scope, "weekday_timetable")
                    self.assertEqual(len(legacy.assignments), 2)

    async def test_initial_partial_result_is_stale_without_complete_timestamp(self):
        self.adapter.value = result(complete=False)
        await self.service.sync()
        self.assertTrue(self.service.status()["stale"])
        self.assertIsNone(self.service.status()["last_success_at"])
        self.assertEqual(self.service.status()["last_sync_at"], self.adapter.value.synced_at)

    async def test_account_change_closes_session_and_clears_old_data(self):
        await self.service.sync()
        await self.service.change_account(DEFAULT_URL)
        self.assertEqual(self.transport.closed, 1)
        self.assertEqual(self.service.assignments()["assignments"], [])
        self.assertIsNone(self.service.status()["last_success_at"])
        self.assertTrue(self.service.status()["stale"])
        self.assertTrue(self.service.status()["paused"])
        await self.service.login()
        self.assertEqual(self.transport.opened, 2)
        self.assertEqual(self.transport.logins, 1)
        self.assertEqual(self.service.status()["state"], "ready")

    async def test_bad_account_url_does_not_destroy_current_snapshot(self):
        await self.service.sync()
        with self.assertRaises(ConfigError):
            await self.service.change_account("https://other-campus.example/ct/home")
        self.assertEqual(self.service.status()["assignment_count"], 1)
        self.assertEqual(self.transport.closed, 0)

    async def test_manual_and_timer_sync_both_work(self):
        self.service.start_scheduler()
        self.service.request_sync()
        await until(lambda: self.adapter.calls == 1)
        await until(lambda: self.adapter.calls == 2)
        self.assertEqual(self.adapter.max_active, 1)
        self.assertFalse(self.service.status()["stale"])

    async def test_login_can_be_cancelled_for_account_switch(self):
        entered = asyncio.Event()
        async def wait_for_login(timeout_seconds=300):
            entered.set()
            await asyncio.Event().wait()
        self.transport.wait_for_login = wait_for_login
        self.assertTrue(self.service.request_login())
        await entered.wait()
        self.assertFalse(self.service.request_login())
        await self.service.change_account(DEFAULT_URL)
        self.assertEqual(self.transport.closed, 1)
        self.assertEqual(self.service.status()["state"], "idle")


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = DeadlineService(ScraperConfig(), transport=FakeTransport(), adapter=FakeAdapter())
        self.dashboard = LocalDashboard(self.service, asyncio.get_running_loop(), port=0)
        self.dashboard.start()

    async def asyncTearDown(self):
        await self.dashboard.close()
        await self.service.close()

    async def request(self, path, *, method="GET", host=None, token=None, origin=None, body=None):
        def perform():
            connection = http.client.HTTPConnection("127.0.0.1", self.dashboard.server.server_port, timeout=3)
            headers = {"Host": host or self.dashboard.host}
            if token:
                headers["X-Manaba-Token"] = token
            if origin:
                headers["Origin"] = origin
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                data = raw.decode("utf-8")
            except UnicodeDecodeError:
                data = raw
            code, response_headers = response.status, dict(response.getheaders())
            connection.close()
            return code, data, response_headers
        return await asyncio.to_thread(perform)

    async def test_data_get_requires_token_and_rejects_foreign_host_and_origin(self):
        code, _, _ = await self.request("/api/assignments")
        self.assertEqual(code, 403)
        code, _, _ = await self.request("/api/status", token=self.dashboard.token, host="evil.example")
        self.assertEqual(code, 403)
        code, _, _ = await self.request("/api/status", token=self.dashboard.token, origin="https://evil.example")
        self.assertEqual(code, 403)
        code, data, headers = await self.request("/api/assignments", token=self.dashboard.token)
        self.assertEqual(code, 200)
        self.assertIn("status", json.loads(data))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    async def test_course_metadata_includes_courses_without_assignments(self):
        self.service._provided_adapter.value = result()
        self.service._provided_adapter.value.courses.append(Course("8", "課題のない試験科目", "https://manaba.ryukoku.ac.jp/ct/course_8"))
        await self.service.sync()
        code, data, _ = await self.request("/api/assignments", token=self.dashboard.token)
        self.assertEqual(code, 200)
        payload = json.loads(data)
        self.assertEqual({c["id"] for c in payload["courses"]}, {"7", "8"})
        self.assertEqual({a["course_id"] for a in payload["assignments"]}, {"7"})

    async def test_template_reload_keeps_session_and_assignment_snapshot(self):
        await self.service.sync()
        snapshot = self.service.last_result
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "dashboard.html"
            page.write_text("<html>FIRST __TOKEN__ __NONCE__ __SCRIPT__</html>")
            with patch("manaba_deadline.webui._PAGE_PATH", page):
                code, first, _ = await self.request("/")
                page.write_text("<html>SECOND __TOKEN__ __NONCE__ __SCRIPT__</html>")
                _, second, _ = await self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("FIRST", first)
        self.assertIn("SECOND", second)
        self.assertNotIn("__SCRIPT__", second)
        self.assertIs(self.service.last_result, snapshot)
        self.assertEqual(self.service._transport.opened, 1)

    async def test_page_has_csp_and_post_validation(self):
        code, html, headers = await self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("textContent", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("img-src blob: 'self' data:;", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        avatar_code, avatar_data, avatar_headers = await self.request("/avatars/karina.jpg")
        self.assertEqual(avatar_code, 200)
        self.assertEqual(avatar_headers["Content-Type"], "image/jpeg")
        code, _, _ = await self.request("/api/account", method="POST", token=self.dashboard.token, body={"url": "https://invalid.example/"})
        self.assertEqual(code, 400)
        self.service.start_scheduler()
        code, _, _ = await self.request("/api/sync", method="POST", token=self.dashboard.token, body={})
        self.assertEqual(code, 202)
        await until(lambda: self.service.status()["state"] == "ready")


if __name__ == "__main__":
    unittest.main()
