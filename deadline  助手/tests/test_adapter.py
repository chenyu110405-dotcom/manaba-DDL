"""End-to-end adapter tests with a strictly synthetic, recording GET transport."""

import unittest
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path

from manaba_deadline.adapter import ManabaAdapter
from manaba_deadline.browser import FetchError, LoginRequired, validate_read_url
from manaba_deadline.config import ScraperConfig


ROOT = "https://manaba.ryukoku.ac.jp/ct/"
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def list_html(course_id, route, rows):
    header = "<tr><th>タイトル</th><th>状態</th><th>受付開始日時</th><th>受付終了日時</th></tr>"
    rendered = []
    for number, title, status, starts, deadline in rows:
        rendered.append(
            f"<tr><td><a href='course_{course_id}_{route}_{number}'>{escape(title)}</a></td>"
            f"<td>{escape(status)}</td><td>{escape(starts)}</td><td>{escape(deadline)}</td></tr>"
        )
    return "<table class='stdlist'>" + header + "".join(rendered) + "</table>"


def summary(body, starts="2026/10/01 09:00", deadline="2026/10/15 23:59", status="受付中／未提出"):
    return (
        "<table class='stdlist-report'><tr><th>問題</th><td>" + escape(body) + "</td></tr>"
        "<tr><th>受付開始日時</th><td>" + starts + "</td></tr>"
        "<tr><th>受付終了日時</th><td>" + deadline + "</td></tr>"
        "<tr><th>状態</th><td>" + escape(status) + "</td></tr></table>"
    )


def weekday_table(courses):
    """A synthetic timetable; each course belongs to an explicit weekday cell."""
    header = "<tr><th></th>" + "".join("<th>" + day + "</th>" for day in "月火水木金土") + "</tr>"
    cells = [f"<td><a href='course_{number}'>{escape(name)}</a></td>" for number, name in courses]
    cells += ["<td></td>"] * (6 - len(cells))
    return "<div id='courselistweekly'><table class='stdlist'>" + header + "<tr><th>1限</th>" + "".join(cells) + "</tr></table></div>"


def pages():
    """A two-course, multi-page synthetic session with all supported task types."""
    return {
        ROOT + "home": "<html><body><a href='logout'>ログアウト</a></body></html>",
        ROOT + "home_course": fixture("home_courses.html"),
        ROOT + "home_library_query": fixture("aggregate.html"),
        ROOT + "home_library_query?page=2": fixture("aggregate_page2.html"),
        ROOT + "home_library_query?page=3": fixture("aggregate_page3.html"),
        ROOT + "course_101": fixture("course_navigation.html"),
        ROOT + "course_101_report": fixture("report_list.html"),
        ROOT + "course_101_report?page=2": fixture("report_list_page2.html"),
        ROOT + "course_101_query": list_html("101", "query", [("10012", "練習小テストＡ", "受付中／未解答", "2026/10/09 09:00", "2026/10/12 23:59")]),
        ROOT + "course_101_survey": list_html("101", "survey", [("10013", "授業に関する練習アンケート", "受付中／未回答", "2026/10/09 09:00", "2026/10/13 23:59")]),
        ROOT + "course_101_project": fixture("project_list.html"),
        ROOT + "course_202": "<nav><a href='course_202_report'>レポート</a></nav>",
        ROOT + "course_202_report": list_html("202", "report", [("20011", "別コースの架空課題", "受付中／未提出", "2026/10/01 09:00", "2026/10/15 23:59")]),
        ROOT + "course_101_report_10016": fixture("report_detail.html"),
        ROOT + "course_101_query_10012": fixture("quiz_detail.html"),
        ROOT + "course_101_survey_10013": summary("教員の架空アンケート説明です。", "2026/10/09 09:00", "2026/10/13 23:59", "受付中／未回答"),
        ROOT + "course_101_report_10017": summary("受付開始前でも読める架空の教員説明です。", "2026/11/01 09:00", "2026/11/02 23:59", "受付開始待ち"),
        ROOT + "course_101_report_10019": fixture("report_empty_detail.html"),
        ROOT + "course_202_report_20011": summary("別コースの教員による架空課題説明です。"),
    }


class FakeTransport:
    """Record reads and fail loudly if the adapter asks for an unexpected URL."""

    def __init__(self, responses, initial_pages=0):
        responses.setdefault(ROOT + "home", "<html><body><a href='logout'>ログアウト</a></body></html>")
        self.responses = responses
        self.pages_read = initial_pages
        self.calls = []

    async def get(self, url):
        validate_read_url(url)
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError("Adapter attempted an unexpected synthetic URL: " + url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        self.pages_read += 1
        return response


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_sync_deduplicates_merges_and_preserves_japanese(self):
        transport = FakeTransport(pages())
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertTrue(result.complete)
        self.assertEqual(result.pages_read, 19)
        self.assertEqual(len(result.assignments), 12)
        self.assertEqual({course.id for course in result.courses}, {"101", "202"})
        self.assertEqual(result.scope, "weekday_timetable")
        self.assertTrue(result.scope_complete)
        items = {item.id: item for item in result.assignments}
        self.assertEqual(len(items), len(result.assignments))
        report = items["ryukoku:101:report:10016"]
        self.assertEqual(report.title_ja, "練習レポート")
        self.assertEqual(report.course_name_ja, "サンプル日本語演習")
        self.assertEqual(report.provenance, ["aggregate", "course_list", "detail"])
        self.assertEqual(report.content_status, "loaded")
        self.assertIn("ＡＢＣの資料", report.content_ja)
        self.assertEqual(report.deadline_at, "2026-10-10T12:00:00+09:00")
        self.assertEqual(report.late_deadline_at, "2026-10-11T12:00:00+09:00")
        self.assertEqual(report.view("2026-10-10T12:00:00+09:00")["remaining_seconds"], 86400)
        self.assertEqual(datetime.fromisoformat(result.synced_at).utcoffset().total_seconds(), 9 * 3600)
        self.assertIsNone(result.assignments[-1].deadline_at)

    async def test_course_submission_status_overrides_aggregate_and_skips_detail(self):
        transport = FakeTransport(pages())
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        items = {item.id: item for item in result.assignments}
        submitted = items["ryukoku:101:report:10011"]
        self.assertEqual(submitted.submission_status, "submitted")
        self.assertEqual(submitted.availability, "closed")
        self.assertEqual(submitted.provenance, ["aggregate", "course_list"])
        self.assertIsNone(submitted.content_ja)
        self.assertEqual(submitted.content_status, "not_loaded")
        self.assertNotIn(ROOT + "course_101_report_10011", transport.calls)
        self.assertNotIn(ROOT + "course_101_report_10020", transport.calls)

    async def test_future_task_absent_from_aggregate_is_recovered_from_course_pagination(self):
        transport = FakeTransport(pages())
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        future = next(item for item in result.assignments if item.id == "ryukoku:101:report:10017")
        self.assertEqual(future.provenance, ["course_list", "detail"])
        self.assertEqual(future.availability, "not_started")
        self.assertEqual(future.starts_at, "2026-11-01T09:00:00+09:00")
        self.assertEqual(future.content_status, "loaded")
        self.assertIn("受付開始前", future.content_ja)
        self.assertIn(ROOT + "course_101_report?page=2", transport.calls)

    async def test_external_project_and_start_routes_are_never_read(self):
        transport = FakeTransport(pages())
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        unsafe = [item for item in result.assignments if item.kind in {"external", "project"} and item.submission_status != "submitted"]
        self.assertEqual(len(unsafe), 3)
        for item in unsafe:
            self.assertEqual(item.content_status, "unsafe_to_open")
            self.assertIsNone(item.content_ja)
            self.assertNotIn(item.source_url, transport.calls)
            self.assertIn("unsafe_detail_skipped", [issue.code for issue in item.issues])
        self.assertTrue(all("start=" not in url and "cancel=" not in url for url in transport.calls))

    async def test_multiple_pages_and_pagination_cycles_fetch_each_page_once(self):
        transport = FakeTransport(pages())
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertTrue(result.complete)
        counts = Counter(transport.calls)
        self.assertTrue(all(count == 1 for count in counts.values()))
        self.assertEqual(counts[ROOT + "home_library_query?page=3"], 1)
        self.assertEqual(counts[ROOT + "course_101_report?page=2"], 1)

    async def test_empty_body_and_no_deadline_do_not_become_parse_failures(self):
        result = await ManabaAdapter(FakeTransport(pages()), ScraperConfig()).sync()
        empty = next(item for item in result.assignments if item.id == "ryukoku:101:report:10019")
        self.assertTrue(result.complete)
        self.assertEqual(empty.content_ja, "")
        self.assertEqual(empty.content_status, "empty")
        self.assertIsNone(empty.deadline_at)
        self.assertEqual([issue.code for issue in empty.issues].count("deadline_missing"), 1)
        self.assertNotIn("date_parse_error", [issue.code for issue in result.issues])

    async def test_real_empty_lists_and_markup_failure_are_distinct(self):
        responses = {ROOT + "home_course": "<p>コースはありません。</p>", ROOT + "home_library_query": "<p>課題はありません。</p>"}
        empty = await ManabaAdapter(FakeTransport(responses), ScraperConfig()).sync()
        self.assertTrue(empty.complete)
        self.assertEqual(empty.assignments, [])
        self.assertEqual(empty.courses, [])
        self.assertEqual(empty.pages_read, 3)
        responses[ROOT + "home_library_query"] = "<html><body></body></html>"
        failed = await ManabaAdapter(FakeTransport(responses), ScraperConfig()).sync()
        self.assertFalse(failed.complete)
        self.assertEqual(failed.assignments, [])
        self.assertIn("list_unrecognized", [issue.code for issue in failed.issues])

    async def test_detail_parse_failure_is_distinct_from_empty_body(self):
        responses = pages()
        responses[ROOT + "course_101_report_10016"] = "<main>未知の画面</main>"
        result = await ManabaAdapter(FakeTransport(responses), ScraperConfig()).sync()
        failure = next(item for item in result.assignments if item.id == "ryukoku:101:report:10016")
        self.assertFalse(result.complete)
        self.assertIsNone(failure.content_ja)
        self.assertEqual(failure.content_status, "parse_error")
        self.assertIn("detail_unrecognized", [issue.code for issue in result.issues])

    async def test_fetch_failure_is_partial_and_diagnostic_excludes_private_details(self):
        responses = pages()
        responses[ROOT + "course_101_report_10016"] = FetchError("SYNTHETIC_PRIVATE_ERROR")
        result = await ManabaAdapter(FakeTransport(responses), ScraperConfig()).sync()
        failure = next(item for item in result.assignments if item.id == "ryukoku:101:report:10016")
        self.assertFalse(result.complete)
        self.assertEqual(failure.content_status, "error")
        self.assertIsNone(failure.content_ja)
        self.assertIn("page_read_failed", [issue.code for issue in result.issues])
        self.assertIn("content_read_failed", [issue.code for issue in failure.issues])
        self.assertTrue(all("SYNTHETIC_PRIVATE_ERROR" not in issue.message_zh for issue in result.issues))

    async def test_login_expiry_propagates_for_user_relogin(self):
        responses = pages()
        responses[ROOT + "course_101_report?page=2"] = LoginRequired()
        transport = FakeTransport(responses)
        with self.assertRaises(LoginRequired):
            await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertTrue(all(not url.endswith("_10016") for url in transport.calls))

    async def test_page_limit_retains_already_parsed_metadata(self):
        transport = FakeTransport(pages())
        result = await ManabaAdapter(transport, ScraperConfig(max_pages=4)).sync()
        self.assertFalse(result.complete)
        self.assertEqual(result.pages_read, 4)
        self.assertEqual(len(transport.calls), 4)
        self.assertIn("page_limit", [issue.code for issue in result.issues])
        self.assertIn("ryukoku:101:query:10012", {item.id for item in result.assignments})
        self.assertIn("ryukoku:202:report:20011", {item.id for item in result.assignments})
        self.assertTrue(all(item.content_status == "not_loaded" for item in result.assignments))
        self.assertTrue(result.scope_complete)

    async def test_course_limit_is_explicit_partial_without_reading_extra_course(self):
        transport = FakeTransport(pages())
        result = await ManabaAdapter(transport, ScraperConfig(max_courses=1)).sync()
        self.assertFalse(result.complete)
        self.assertIn("course_limit", [issue.code for issue in result.issues])
        self.assertNotIn(ROOT + "course_202", transport.calls)
        self.assertNotIn(ROOT + "course_202_report", transport.calls)
        self.assertIn("ryukoku:202:report:20011", {item.id for item in result.assignments})
        self.assertTrue(result.scope_complete)

    async def test_each_sync_refreshes_pages_and_reports_its_own_counter_delta(self):
        responses = pages()
        transport = FakeTransport(responses, initial_pages=40)
        adapter = ManabaAdapter(transport, ScraperConfig())
        first = await adapter.sync()
        responses[ROOT + "course_101_report_10016"] = fixture("report_detail.html").replace("ＡＢＣの資料を読んでください。", "更新後の架空説明を読んでください。")
        second = await adapter.sync()
        self.assertEqual(first.pages_read, 19)
        self.assertEqual(second.pages_read, 19)
        self.assertEqual(transport.pages_read, 78)
        old = next(item for item in first.assignments if item.id == "ryukoku:101:report:10016")
        new = next(item for item in second.assignments if item.id == old.id)
        self.assertIn("ＡＢＣ", old.content_ja)
        self.assertIn("更新後", new.content_ja)
        self.assertIsNot(old, new)
        self.assertEqual(Counter(transport.calls)[ROOT + "home_course"], 2)

    async def test_filter_reset_ignores_filtered_rows_and_does_not_follow_their_pagination(self):
        responses = {
            ROOT + "home_course": "<p>コースはありません。</p>",
            ROOT + "home_library_query": "<select><option selected>レポート</option><option value='home_library_query_'>全課題</option></select>" + fixture("aggregate.html"),
            ROOT + "home_library_query_": "<p>課題はありません。</p>",
        }
        transport = FakeTransport(responses)
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertTrue(result.complete)
        self.assertEqual(result.assignments, [])
        self.assertEqual(transport.calls, [ROOT + "home", ROOT + "home_course", ROOT + "home_library_query", ROOT + "home_library_query_"])

    async def test_filter_reset_cycle_reports_incomplete(self):
        responses = {
            ROOT + "home_course": "<p>コースはありません。</p>",
            ROOT + "home_library_query": "<select><option selected>レポート</option><option value='home_library_query'>全課題</option></select><p>課題はありません。</p>",
        }
        result = await ManabaAdapter(FakeTransport(responses), ScraperConfig()).sync()
        self.assertFalse(result.complete)
        self.assertIn("filter_cycle", [issue.code for issue in result.issues])

    async def test_cross_course_navigation_is_skipped_without_read(self):
        responses = pages()
        responses[ROOT + "course_101"] += "<a href='course_909_report'>別コースの入口</a>"
        transport = FakeTransport(responses)
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertFalse(result.complete)
        self.assertIn("navigation_course_mismatch", [issue.code for issue in result.issues])
        self.assertNotIn(ROOT + "course_909_report", transport.calls)

    async def test_all_discovery_pages_failed_raises_instead_of_successful_empty(self):
        responses = {
            ROOT + "home_course": "<main>構造が変わった架空ページ</main>",
            ROOT + "home_library_query": FetchError("SYNTHETIC_REMOTE_FAILURE"),
        }
        with self.assertRaises(FetchError) as raised:
            await ManabaAdapter(FakeTransport(responses), ScraperConfig()).sync()
        self.assertNotIn("SYNTHETIC_REMOTE_FAILURE", str(raised.exception))

    async def test_valid_aggregate_date_survives_unparseable_course_date(self):
        responses = pages()
        responses[ROOT + "course_101_report"] = fixture("report_list.html").replace("2026/10/10 12:00", "架空の壊れた日時")
        responses[ROOT + "course_101_report_10016"] = "<main>未知の画面</main>"
        result = await ManabaAdapter(FakeTransport(responses), ScraperConfig()).sync()
        record = next(item for item in result.assignments if item.id == "ryukoku:101:report:10016")
        self.assertFalse(result.complete)
        self.assertEqual(record.deadline_at, "2026-10-10T12:00:00+09:00")
        self.assertIn("date_parse_error", [issue.code for issue in record.issues])

    async def test_explicit_unset_course_date_clears_old_aggregate_date(self):
        responses = pages()
        responses[ROOT + "course_101_report"] = fixture("report_list.html").replace("2026/10/10 12:00", "未設定")
        responses[ROOT + "course_101_report_10016"] = fixture("report_empty_detail.html")
        result = await ManabaAdapter(FakeTransport(responses), ScraperConfig()).sync()
        record = next(item for item in result.assignments if item.id == "ryukoku:101:report:10016")
        self.assertIsNone(record.deadline_at)
        self.assertIn("deadline_missing", [issue.code for issue in record.issues])

    async def test_unknown_pagination_marks_partial_without_following_other_routes(self):
        responses = pages()
        responses[ROOT + "home_library_query"] += "<a rel='next' href='course_909_report'>未知の次ページ</a>"
        transport = FakeTransport(responses)
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertFalse(result.complete)
        self.assertIn("pagination_unsupported", [issue.code for issue in result.issues])
        self.assertNotIn(ROOT + "course_909_report", transport.calls)

    async def test_outside_timetable_aggregate_records_never_expand_scope_or_trigger_reads(self):
        responses = pages()
        responses[ROOT + "home_library_query"] += fixture("aggregate_outside_timetable.html")
        transport = FakeTransport(responses)
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertTrue(result.complete)
        self.assertTrue(result.scope_complete)
        self.assertEqual(result.scope, "weekday_timetable")
        self.assertEqual({course.id for course in result.courses}, {"101", "202"})
        self.assertEqual({item.course_id for item in result.assignments}, {"101", "202"})
        self.assertEqual(len(result.assignments), 12)
        self.assertTrue(all("course_909" not in url and "course_808" not in url for url in transport.calls))

    async def test_known_empty_timetable_ignores_aggregate_miscellaneous_assignments(self):
        responses = {
            ROOT + "home_course": fixture("home_empty_timetable.html"),
            ROOT + "home_library_query": fixture("aggregate_outside_timetable.html"),
        }
        transport = FakeTransport(responses)
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertTrue(result.complete)
        self.assertTrue(result.scope_complete)
        self.assertEqual(result.scope, "weekday_timetable")
        self.assertEqual(result.courses, [])
        self.assertEqual(result.assignments, [])
        self.assertTrue(all("course_909" not in url and "course_808" not in url for url in transport.calls))

    async def test_unknown_timetable_fails_even_when_aggregate_is_recognized(self):
        responses = {
            ROOT + "home_course": fixture("home_unknown_layout.html"),
            ROOT + "home_library_query": fixture("aggregate_outside_timetable.html"),
        }
        transport = FakeTransport(responses)
        with self.assertRaises(FetchError):
            await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertTrue(all("course_909" not in url and "course_808" not in url for url in transport.calls))

    async def test_weekday_course_pagination_discovers_all_allowed_ids(self):
        responses = pages()
        responses[ROOT + "home_course"] = weekday_table([("101", "サンプル日本語演習")]) + "<a rel='next' href='?page=2'>次へ</a>"
        responses[ROOT + "home_course?page=2"] = weekday_table([("202", "架空の情報基礎")]) + "<a rel='prev' href='home_course'>前へ</a>" + fixture("home_unknown_layout.html")
        transport = FakeTransport(responses)
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        self.assertTrue(result.complete)
        self.assertTrue(result.scope_complete)
        self.assertEqual({course.id for course in result.courses}, {"101", "202"})
        self.assertEqual({item.course_id for item in result.assignments}, {"101", "202"})
        self.assertEqual(result.pages_read, 20)
        self.assertEqual(Counter(transport.calls)[ROOT + "home_course?page=2"], 1)

    async def test_incomplete_timetable_pagination_marks_scope_unreliable(self):
        responses = pages()
        responses[ROOT + "home_course"] = weekday_table([("101", "サンプル日本語演習")]) + "<a rel='next' href='?page=2'>次へ</a>"
        responses[ROOT + "home_course?page=2"] = weekday_table([("202", "架空の情報基礎")])
        transport = FakeTransport(responses)
        result = await ManabaAdapter(transport, ScraperConfig(max_pages=2)).sync()
        self.assertFalse(result.complete)
        self.assertFalse(result.scope_complete)
        self.assertEqual(result.scope, "weekday_timetable")
        self.assertEqual({course.id for course in result.courses}, {"101"})
        self.assertEqual(result.assignments, [])
        self.assertIn("page_limit", [issue.code for issue in result.issues])
        self.assertEqual(result.pages_read, 2)

    async def test_failed_later_timetable_page_keeps_only_confirmed_course_ids(self):
        for failed_page in (FetchError("SYNTHETIC_PAGE_FAILURE"), fixture("home_unknown_layout.html")):
            with self.subTest(failed_page_type=type(failed_page).__name__):
                responses = pages()
                responses[ROOT + "home_course"] = weekday_table([("101", "サンプル日本語演習")]) + "<a rel='next' href='?page=2'>次へ</a>"
                responses[ROOT + "home_course?page=2"] = failed_page
                transport = FakeTransport(responses)
                result = await ManabaAdapter(transport, ScraperConfig()).sync()
                self.assertFalse(result.complete)
                self.assertFalse(result.scope_complete)
                self.assertEqual({course.id for course in result.courses}, {"101"})
                self.assertEqual({item.course_id for item in result.assignments}, {"101"})
                self.assertIn("ryukoku:101:report:10016", {item.id for item in result.assignments})
                self.assertTrue(all("course_202" not in url and "course_909" not in url for url in transport.calls))

    async def test_submission_and_expiry_do_not_filter_weekday_project_records(self):
        transport = FakeTransport(pages())
        result = await ManabaAdapter(transport, ScraperConfig()).sync()
        items = {item.id: item for item in result.assignments}
        submitted = items["ryukoku:101:project:10021"]
        expired = items["ryukoku:101:project:10022"]
        self.assertEqual(submitted.submission_status, "submitted")
        self.assertEqual(expired.submission_status, "unsubmitted")
        self.assertEqual(submitted.availability, "closed")
        self.assertEqual(expired.availability, "closed")
        self.assertEqual(submitted.deadline_at, "2026-09-02T23:59:00+09:00")
        self.assertEqual(expired.deadline_at, "2026-09-02T23:59:00+09:00")
        self.assertNotIn(submitted.source_url, transport.calls)
        self.assertNotIn(expired.source_url, transport.calls)


if __name__ == "__main__":
    unittest.main()
