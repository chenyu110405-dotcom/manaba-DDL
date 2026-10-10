"""Parser contract tests use only synthetic Japanese courses and assignments."""

import unittest
from pathlib import Path

from manaba_deadline.models import Assignment, Course
from manaba_deadline.parser import (
    absolute_link,
    parse_assignment_detail,
    parse_assignment_list,
    parse_course_navigation,
    parse_courses,
    status_from_text,
)


ROOT = "https://manaba.ryukoku.ac.jp/ct/"
FIXTURES = Path(__file__).parent / "fixtures"
COURSE = Course("101", "サンプル日本語演習", ROOT + "course_101")


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def task(kind="report", route="report", number="10016"):
    return Assignment(
        f"ryukoku:101:{route}:{number}", "101", COURSE.name_ja,
        "練習レポート", kind, ROOT + f"course_101_{route}_{number}",
    )


class CourseParserTests(unittest.TestCase):
    def test_weekday_timetable_deduplicates_courses_and_excludes_other_regions(self):
        result = parse_courses(fixture("home_courses.html"), ROOT + "home_course")
        self.assertTrue(result.recognized)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.filter_urls, [])
        self.assertEqual([(item.id, item.name_ja) for item in result.items], [("101", COURSE.name_ja), ("202", "架空の情報基礎")])

    def test_empty_weekday_timetable_is_known_empty_despite_miscellaneous_courses(self):
        result = parse_courses(fixture("home_empty_timetable.html"), ROOT + "home_course")
        self.assertTrue(result.recognized)
        self.assertEqual(result.items, [])
        self.assertEqual(result.issues, [])

    def test_miscellaneous_only_unknown_layout_is_not_a_timetable(self):
        result = parse_courses(fixture("home_unknown_layout.html"), ROOT + "home_course")
        self.assertFalse(result.recognized)
        self.assertEqual(result.items, [])
        self.assertTrue(result.issues)

    def test_other_section_empty_message_does_not_confirm_empty_scope(self):
        for html in ("<a href='course_101'>架空授業</a><p>その他のコースはありません。</p>", "<p>その他のコースはありません。</p>"):
            with self.subTest(html=html):
                result = parse_courses(html, ROOT + "home_course")
                self.assertFalse(result.recognized)
                self.assertEqual(result.items, [])

    def test_missing_course_label_preserves_membership_with_issue(self):
        html = fixture("home_courses.html").replace("サンプル日本語演習", "")
        result = parse_courses(html, ROOT + "home_course")
        self.assertTrue(result.recognized)
        self.assertEqual({course.id for course in result.items}, {"101", "202"})
        self.assertIn("timetable_course_name_missing", {issue.code for issue in result.issues})

    def test_miscellaneous_colspan_rows_inside_weekday_table_are_excluded(self):
        html = (
            "<table class='stdlist'><tr><th></th><th>月</th><th>火</th><th>水</th><th>木</th><th>金</th><th>土</th></tr>"
            "<tr><th>1限</th><td><a href='course_101'>サンプル日本語演習</a></td><td></td><td></td><td></td><td></td><td></td></tr>"
            "<tr><th colspan='7'>その他の曜日</th></tr><tr><td colspan='7'><a href='course_909'>その他の架空授業</a></td></tr></table>"
        )
        result = parse_courses(html, ROOT + "home_course")
        self.assertTrue(result.recognized)
        self.assertEqual([item.id for item in result.items], ["101"])

    def test_weekday_pagination_is_returned_without_treating_misc_links_as_courses(self):
        html = fixture("home_courses.html") + "<nav class='pagination'><a rel='next' href='?page=2'>次へ</a></nav>"
        result = parse_courses(html, ROOT + "home_course")
        self.assertEqual(result.pagination, [ROOT + "home_course?page=2"])
        self.assertEqual({item.id for item in result.items}, {"101", "202"})

    def test_current_courses_all_years_reset_without_preference_mutation(self):
        result = parse_courses(fixture("home_filtered.html"), ROOT + "home_course?coursestatus=past")
        self.assertEqual(result.filter_urls, [ROOT + "home_course"])
        self.assertTrue(all("chglistformat" not in value for value in result.filter_urls))

    def test_empty_courses_and_failed_markup_are_distinct(self):
        empty = parse_courses("<p>登録されているコースはありません。</p>", ROOT + "home_course")
        failure = parse_courses("<main>予期しない画面</main>", ROOT + "home_course")
        self.assertTrue(empty.recognized)
        self.assertEqual(empty.items, [])
        self.assertFalse(failure.recognized)
        self.assertEqual([issue.code for issue in failure.issues], ["courses_unrecognized"])

    def test_navigation_only_returns_list_routes_and_deduplicates(self):
        result = parse_course_navigation(fixture("course_navigation.html"), COURSE.url)
        self.assertEqual(result.items, [ROOT + f"course_101_{route}" for route in ("query", "survey", "report", "project")])
        self.assertTrue(result.recognized)

    def test_same_origin_https_links_only(self):
        for href in ("http://manaba.ryukoku.ac.jp/ct/course_101", "https://example.invalid/ct/course_101", "https://person:secret@manaba.ryukoku.ac.jp/ct/course_101", "javascript:alert(1)"):
            self.assertIsNone(absolute_link(href, COURSE.url))
        self.assertEqual(absolute_link("course_101#top", ROOT + "home"), COURSE.url)


class AssignmentListParserTests(unittest.TestCase):
    def test_observed_td_headers_extra_period_column_and_exact_ids(self):
        result = parse_assignment_list(fixture("aggregate.html"), ROOT + "home_library_query", aggregate=True)
        self.assertTrue(result.recognized)
        self.assertEqual(result.issues, [])
        self.assertEqual(len(result.items), 6)
        quiz = next(item for item in result.items if item.kind == "quiz")
        self.assertEqual(quiz.id, "ryukoku:101:query:10012")
        self.assertEqual(quiz.course_name_ja, COURSE.name_ja)
        self.assertEqual(quiz.title_ja, "練習小テストＡ")
        self.assertEqual(quiz.source_url, ROOT + "course_101_query_10012")
        self.assertEqual(quiz.starts_at, "2026-10-09T09:00:00+09:00")
        self.assertEqual(quiz.deadline_at, "2026-10-12T23:59:00+09:00")
        self.assertEqual(quiz.submission_status, "unsubmitted")
        self.assertEqual(quiz.provenance, ["aggregate"])
        external = next(item for item in result.items if item.kind == "external")
        self.assertIsNone(external.deadline_at)
        self.assertEqual([issue.code for issue in external.issues], ["deadline_missing"])
        self.assertEqual(result.pagination, [ROOT + "home_library_query?page=2"])

    def test_course_status_dates_and_late_cutoff(self):
        result = parse_assignment_list(fixture("report_list.html"), ROOT + "course_101_report", COURSE)
        submitted, pending = result.items
        self.assertEqual(submitted.submission_status, "submitted")
        self.assertEqual(submitted.availability, "closed")
        self.assertEqual(pending.submission_status, "unsubmitted")
        self.assertEqual(pending.availability, "open")
        self.assertEqual(pending.late_deadline_at, "2026-10-11T12:00:00+09:00")
        self.assertEqual(pending.raw_status_ja, "受付中／未提出")
        self.assertEqual(pending.provenance, ["course_list"])

    def test_future_unopened_course_task_is_parseable(self):
        result = parse_assignment_list(fixture("report_list_page2.html"), ROOT + "course_101_report?page=2", COURSE)
        future = result.items[0]
        self.assertEqual(future.id, "ryukoku:101:report:10017")
        self.assertEqual(future.availability, "not_started")
        self.assertEqual(future.submission_status, "unknown")
        self.assertEqual(future.starts_at, "2026-11-01T09:00:00+09:00")

    def test_only_member_project_links_produce_own_assignments(self):
        result = parse_assignment_list(fixture("project_list.html"), ROOT + "course_101_project", COURSE)
        self.assertEqual([item.id for item in result.items], ["ryukoku:101:project:10014", "ryukoku:101:project:10021", "ryukoku:101:project:10022"])
        self.assertEqual(result.issues, [])

    def test_valid_empty_list_is_different_from_unrecognized_or_failed_identity(self):
        empty = parse_assignment_list("<p>レポートはありません。</p>", ROOT + "course_101_report", COURSE)
        blank = parse_assignment_list("<html><body></body></html>", ROOT + "course_101_report", COURSE)
        unsupported = parse_assignment_list("<table><tr><th>タイトル</th></tr><tr><td><a href='unknown_7'>架空課題</a></td></tr></table>", ROOT + "course_101_report", COURSE)
        self.assertTrue(empty.recognized)
        self.assertEqual(empty.items, [])
        self.assertFalse(blank.recognized)
        self.assertIn("list_unrecognized", [issue.code for issue in blank.issues])
        self.assertEqual(unsupported.items, [])
        self.assertIn("identity_unrecognized", [issue.code for issue in unsupported.issues])

    def test_date_errors_are_explicit_and_do_not_echo_source(self):
        html = "<table><tr><th>タイトル</th><th>受付終了日時</th></tr><tr><td><a href='course_101_report_10016'>練習</a></td><td>架空の壊れた日時</td></tr></table>"
        result = parse_assignment_list(html, ROOT + "course_101_report", COURSE)
        parsed = result.items[0]
        self.assertIsNone(parsed.starts_at)
        self.assertIsNone(parsed.deadline_at)
        self.assertEqual({issue.code for issue in parsed.issues}, {"date_column_missing", "date_parse_error"})
        self.assertTrue(all("架空の壊れた日時" not in issue.message_zh for issue in parsed.issues))

    def test_cross_course_row_is_skipped(self):
        html = "<table><tr><th>タイトル</th></tr><tr><td><a href='course_202_report_20011'>他コース</a></td></tr></table>"
        result = parse_assignment_list(html, ROOT + "course_101_report", COURSE)
        self.assertEqual(result.items, [])
        self.assertEqual([issue.code for issue in result.issues], ["course_mismatch"])

    def test_aggregate_course_label_requires_matching_same_origin_course(self):
        for href in ("course_202", "https://example.invalid/ct/course_101"):
            with self.subTest(href=href):
                html = (
                    "<table class='stdlist'><tr><td>タイプ</td><td>タイトル</td><td>コース</td>"
                    "<td>受付開始日時</td><td>受付終了日時</td></tr><tr><td>レポート</td>"
                    "<td><a href='course_101_report_10016'>練習レポート</a></td>"
                    f"<td><a href='{href}'>別コースの架空名称</a></td>"
                    "<td>2026/10/01 09:00</td><td>2026/10/10 12:00</td></tr></table>"
                )
                result = parse_assignment_list(html, ROOT + "home_library_query", aggregate=True)
                self.assertEqual(len(result.items), 1)
                item = result.items[0]
                self.assertEqual(item.id, "ryukoku:101:report:10016")
                self.assertEqual(item.course_id, "101")
                self.assertEqual(item.course_name_ja, "")
                self.assertEqual({issue.code for issue in item.issues}, {"course_label_mismatch", "course_name_missing"})

    def test_aggregate_filter_reset_and_pagination_keep_only_same_path(self):
        html = "<select><option selected value='home_library_query_report'>レポート</option><option value='home_library_query_'>全課題</option></select><p>課題はありません。</p><a rel='next' href='?page=2'>次へ</a><a rel='next' href='course_101_report'>別入口</a>"
        result = parse_assignment_list(html, ROOT + "home_library_query", aggregate=True)
        self.assertEqual(result.filter_urls, [ROOT + "home_library_query_"])
        self.assertEqual(result.pagination, [ROOT + "home_library_query?page=2"])
        self.assertEqual([issue.code for issue in result.issues], ["pagination_unsupported"])

    def test_permission_errors_are_not_empty_lists(self):
        for html in ("<p>課題を見る権限がありません。</p>", "<p>コースにアクセスできません。課題はありません。</p>", "<p>ログインしてください。課題はありません。</p>"):
            with self.subTest(html=html):
                result = parse_assignment_list(html, ROOT + "course_101_report", COURSE)
                self.assertFalse(result.recognized)
                self.assertIn("list_unrecognized", [issue.code for issue in result.issues])


class AssignmentDetailParserTests(unittest.TestCase):
    def test_report_vertical_summary_plain_br_paragraphs_preserve_japanese(self):
        source = task()
        result = parse_assignment_detail(fixture("report_detail.html"), source.source_url, source)
        self.assertIs(result, source)
        self.assertEqual(result.content_ja, "ＡＢＣの資料を読んでください。\n第１段落を日本語でまとめてください。\n理由も説明してください。")
        self.assertEqual(result.content_status, "loaded")
        self.assertEqual(result.deadline_at, "2026-10-10T12:00:00+09:00")
        self.assertEqual(result.late_deadline_at, "2026-10-11T12:00:00+09:00")
        self.assertEqual(result.submission_status, "unsubmitted")
        self.assertNotIn("SYNTHETIC_PRIVATE_DRAFT", result.content_ja)
        self.assertEqual(result.provenance, ["detail"])

    def test_pre_start_quiz_description_excludes_form_controls(self):
        source = task("quiz", "query", "10012")
        result = parse_assignment_detail(fixture("quiz_detail.html"), source.source_url, source)
        self.assertEqual(result.content_ja, "この説明は開始前に閲覧できます。\n日本語の資料を確認してください。")
        self.assertEqual(result.content_status, "loaded")
        self.assertNotIn("SYNTHETIC", result.content_ja)
        self.assertNotIn("Start", result.content_ja)

    def test_empty_body_and_unset_deadline_are_explicit(self):
        source = task(number="10019")
        result = parse_assignment_detail(fixture("report_empty_detail.html"), source.source_url, source)
        self.assertEqual(result.content_ja, "")
        self.assertEqual(result.content_status, "empty")
        self.assertIsNone(result.deadline_at)
        self.assertEqual([issue.code for issue in result.issues], ["deadline_missing"])

    def test_form_peer_and_later_answer_tables_never_enter_body(self):
        source = task()
        result = parse_assignment_detail(fixture("unsafe_answers.html"), source.source_url, source)
        self.assertEqual(result.content_ja, "教員の課題説明だけを読みます。")
        self.assertNotIn("SYNTHETIC", result.content_ja)

    def test_nested_answer_rows_do_not_override_metadata(self):
        html = "<table class='stdlist-report'><tr><th>受付終了日時</th><td>2026/10/10 12:00</td></tr><tr><th>参考情報</th><td><table><tr><th>問題</th><td>SYNTHETIC_NESTED_ANSWER</td></tr><tr><th>受付終了日時</th><td>2027/01/01 12:00</td></tr></table></td></tr><tr><th>問題</th><td>外側の教員説明</td></tr></table>"
        source = task()
        result = parse_assignment_detail(html, source.source_url, source)
        self.assertEqual(result.content_ja, "外側の教員説明")
        self.assertEqual(result.deadline_at, "2026-10-10T12:00:00+09:00")

    def test_nested_peer_content_is_excluded_from_accepted_instructor_body(self):
        unsafe = (
            "<section class='peer-submissions'><table><tr><th>問題</th>"
            "<td>SYNTHETIC_NESTED_PRIVATE_ANSWER</td></tr>"
            "<tr><th>受付終了日時</th><td>2027/01/01 12:00</td></tr></table></section>"
        )
        for teacher_text in (True, False):
            with self.subTest(teacher_text=teacher_text):
                body = "<p>教員の説明です。</p>" + unsafe + "<p>説明の続きです。</p>" if teacher_text else unsafe
                html = (
                    "<table class='stdlist-report'><tr><th>問題</th><td>" + body + "</td></tr>"
                    "<tr><th>受付開始日時</th><td>2026/10/01 09:00</td></tr>"
                    "<tr><th>受付終了日時</th><td>2026/10/10 12:00</td></tr></table>"
                )
                source = task()
                result = parse_assignment_detail(html, source.source_url, source)
                self.assertNotIn("SYNTHETIC", result.content_ja or "")
                self.assertEqual(result.deadline_at, "2026-10-10T12:00:00+09:00")
                self.assertIn("content_sections_excluded", [issue.code for issue in result.issues])
                if teacher_text:
                    self.assertEqual(result.content_ja, "教員の説明です。\n説明の続きです。")
                    self.assertEqual(result.content_status, "loaded")
                else:
                    self.assertFalse(result.content_ja)
                    self.assertEqual(result.content_status, "unavailable")

    def test_assets_have_explicit_completeness_issue(self):
        html = "<table class='stdlist-report'><tr><th>問題</th><td>画像を確認<img src='asset.png'></td></tr><tr><th>受付終了日時</th><td>2026/10/10 12:00</td></tr><tr><th>添付ファイル</th><td><a href='asset.pdf'>資料</a></td></tr></table>"
        source = task()
        result = parse_assignment_detail(html, source.source_url, source)
        self.assertEqual(result.content_ja, "画像を確認")
        self.assertIn("content_has_assets", [issue.code for issue in result.issues])

    def test_quiz_without_pre_start_description_is_not_read_from_answer_form(self):
        html = "<table class='stdlist-query'><tr><th>受付終了日時</th><td>2026/10/10 12:00</td></tr></table><form><textarea>SYNTHETIC_ANSWER</textarea></form>"
        source = task("quiz", "query", "10012")
        result = parse_assignment_detail(html, source.source_url, source)
        self.assertIsNone(result.content_ja)
        self.assertEqual(result.content_status, "unsafe_to_open")
        self.assertIn("content_unavailable", [issue.code for issue in result.issues])

    def test_unknown_detail_is_parse_error_not_empty_body(self):
        source = task()
        result = parse_assignment_detail("<main>予期しない画面</main>", source.source_url, source)
        self.assertIsNone(result.content_ja)
        self.assertEqual(result.content_status, "parse_error")
        self.assertIn("detail_unrecognized", [issue.code for issue in result.issues])

    def test_submission_status_recognition_prioritizes_unsubmitted(self):
        cases = {
            "受付中／まだ提出していません": ("unsubmitted", "open"),
            "受付終了／提出済み": ("submitted", "closed"),
            "受付開始待ち": ("unknown", "not_started"),
            "受付中／未回答": ("unsubmitted", "open"),
            "解答中": ("in_progress", "unknown"),
            "未知の状態": ("unknown", "unknown"),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(status_from_text(value), expected)


if __name__ == "__main__":
    unittest.main()
