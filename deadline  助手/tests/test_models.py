"""Synthetic model and timestamp cases; no real accounts or course content."""

import unittest
from datetime import datetime, timezone

from manaba_deadline.models import (
    Assignment,
    Course,
    Issue,
    SyncResult,
    now_tokyo_iso,
    parse_manaba_datetime,
    sorted_assignments,
)


def assignment(identifier="sample-report", **changes):
    fields = dict(
        id=identifier,
        course_id="sample-course",
        course_name_ja="サンプル授業",
        title_ja="第１回　練習課題",
        kind="report",
        source_url="https://example.invalid/ct/page_sample_report",
    )
    fields.update(changes)
    return Assignment(**fields)


class DateParsingTests(unittest.TestCase):
    def test_supported_formats(self):
        for value in (
            "2026/10/10 12:34",
            "2026-10-10 12:34",
            "2026年10月10日(土)12:34",
            "２０２６／１０／１０（土曜日）　１２：３４",
            "2026/10/10 [土] 12:34",
            "2026/10/10【土曜】12:34",
            "2026-10-10T12:34:00",
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_manaba_datetime(value), "2026-10-10T12:34:00+09:00")

    def test_seconds_are_not_discarded(self):
        self.assertEqual(parse_manaba_datetime("2026/1/2 9:04:56"), "2026-01-02T09:04:56+09:00")

    def test_24_hour_rollover(self):
        self.assertEqual(parse_manaba_datetime("2026/12/31 24:00"), "2027-01-01T00:00:00+09:00")
        self.assertEqual(parse_manaba_datetime("2024/2/29 24:00:00"), "2024-03-01T00:00:00+09:00")

    def test_missing_dates_remain_none(self):
        for value in ("", " ", "-", "－", "—", "無期限", "未設定", "設定なし", "期限なし", "なし"):
            with self.subTest(value=value):
                self.assertIsNone(parse_manaba_datetime(value))

    def test_partial_or_invalid_dates_raise_without_echoing_source(self):
        for value in (
            "2026/10/10", "23:59", "受付終了", "2026/2/29 12:00",
            "2026/13/1 00:00", "2026/1/0 00:00", "2026/1/1 24:01",
            "2026/1/1 24:00:01", "2026/1/1 25:00", "2026/1/1 12:60",
            "2026年10/10 12:00", "2026/10/10(土] 12:00", "9999/12/31 24:00",
            "2026/1/112:30", "2026-10-1012:34",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    parse_manaba_datetime(value)
                self.assertNotIn(value, str(raised.exception))

    def test_current_time_has_japan_offset(self):
        result = datetime.fromisoformat(now_tokyo_iso())
        self.assertEqual(result.utcoffset().total_seconds(), 9 * 3600)


class AssignmentViewTests(unittest.TestCase):
    def test_countdown_and_japanese_fields(self):
        source = assignment(
            content_ja="ＡＢＣの問題を読み、考察を書いてください。\n次の段落。",
            content_status="loaded",
            deadline_at="2026-10-11T12:34:05+09:00",
            submission_status="unsubmitted",
            availability="open",
            raw_status_ja="受付中／未提出",
        )
        result = source.view("2026-10-10T09:30:00+09:00")
        self.assertEqual(result["remaining_seconds"], 97445)
        self.assertEqual(result["countdown_zh"], "剩余 1天 3小时 4分 5秒")
        self.assertEqual(result["deadline_display_zh"], "2026年10月11日 12:34:05（日本时间）")
        self.assertEqual(result["status_zh"], "未提交 · 开放中")
        self.assertEqual(result["kind_zh"], "报告")
        self.assertEqual(result["content_status_zh"], "内容已读取")
        for field in ("course_name_ja", "title_ja", "content_ja", "raw_status_ja"):
            self.assertEqual(result[field], getattr(source, field))

    def test_countdown_boundary_and_overdue(self):
        source = assignment(deadline_at="2026-10-10T12:00:00+09:00", submission_status="unsubmitted")
        cases = (
            ("2026-10-10T11:59:59+09:00", 1, "剩余 1秒"),
            ("2026-10-10T12:00:00+09:00", 0, "已截止"),
            ("2026-10-10T12:00:01+09:00", -1, "已逾期 1秒"),
        )
        for now, seconds, label in cases:
            with self.subTest(now=now):
                result = source.view(now)
                self.assertEqual(result["remaining_seconds"], seconds)
                self.assertEqual(result["countdown_zh"], label)
        self.assertEqual(source.view(cases[-1][0])["status_zh"], "未提交 · 已截止")

    def test_fractional_last_second(self):
        source = assignment(deadline_at="2026-10-10T12:00:00+09:00")
        self.assertEqual(source.view("2026-10-10T11:59:59.5+09:00")["remaining_seconds"], 1)
        self.assertEqual(source.view("2026-10-10T12:00:00.5+09:00")["remaining_seconds"], -1)

    def test_aware_now_is_converted_from_utc(self):
        source = assignment(deadline_at="2026-10-10T12:00:00+09:00")
        result = source.view(datetime(2026, 10, 10, 2, 59, 59, tzinfo=timezone.utc))
        self.assertEqual(result["remaining_seconds"], 1)

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            assignment().view(datetime(2026, 10, 10, 12))

    def test_unknown_deadline_and_unknown_status_are_explicit(self):
        result = assignment().view("2026-10-10T12:00:00+09:00")
        self.assertIsNone(result["deadline_at"])
        self.assertIsNone(result["content_ja"])
        self.assertIsNone(result["remaining_seconds"])
        self.assertEqual(result["deadline_display_zh"], "截止时间未知")
        self.assertEqual(result["countdown_zh"], "截止时间未知")
        self.assertEqual(result["status_zh"], "提交状态未知")

    def test_future_start_and_submitted_status(self):
        source = assignment(starts_at="2026-10-11T12:00:00+09:00", submission_status="unsubmitted")
        self.assertEqual(source.view("2026-10-10T12:00:00+09:00")["status_zh"], "未提交 · 尚未开放")
        source.submission_status = "submitted"
        self.assertEqual(source.view("2026-10-10T12:00:00+09:00")["status_zh"], "已提交")

    def test_late_deadline_takes_over_at_original_deadline(self):
        source = assignment(
            deadline_at="2026-10-10T12:00:00+09:00",
            late_deadline_at="2026-10-11T12:00:00+09:00",
            submission_status="unsubmitted",
        )
        before = source.view("2026-10-10T11:59:59+09:00")
        self.assertEqual(before["remaining_seconds"], 1)
        self.assertNotIn("迟交", before["deadline_display_zh"])
        late = source.view("2026-10-10T12:00:00+09:00")
        self.assertEqual(late["remaining_seconds"], 86400)
        self.assertEqual(late["status_zh"], "未提交 · 可迟交")
        self.assertIn("迟交截止：2026年10月11日 12:00", late["deadline_display_zh"])
        self.assertIn("原截止：2026年10月10日 12:00", late["deadline_display_zh"])
        after = source.view("2026-10-11T12:00:01+09:00")
        self.assertEqual(after["remaining_seconds"], -1)
        self.assertEqual(after["status_zh"], "未提交 · 已截止")
        self.assertEqual(late["deadline_at"], source.deadline_at)

    def test_earlier_late_date_does_not_override_original(self):
        source = assignment(
            deadline_at="2026-10-10T12:00:00+09:00",
            late_deadline_at="2026-10-09T12:00:00+09:00",
        )
        self.assertEqual(source.view("2026-10-10T12:00:01+09:00")["remaining_seconds"], -1)

    def test_sorting_uses_late_date_and_places_unknown_last(self):
        no_date = assignment("undated")
        earlier = assignment("earlier", deadline_at="2026-10-10T18:00:00+09:00")
        late = assignment("late", deadline_at="2026-10-10T09:00:00+09:00", late_deadline_at="2026-10-11T12:00:00+09:00")
        original = [no_date, late, earlier]
        result = sorted_assignments(original, "2026-10-10T12:00:00+09:00")
        self.assertEqual([item.id for item in result], ["earlier", "late", "undated"])
        self.assertEqual(original, [no_date, late, earlier])

    def test_recursive_serialization_and_independent_lists(self):
        first = assignment()
        first.issues.append(Issue("content_missing", "正文未提供", assignment_id=first.id, page_kind="report"))
        second = assignment("second")
        self.assertEqual(second.issues, [])
        self.assertEqual(second.provenance, [])
        result = SyncResult(
            assignments=[first],
            courses=[Course("sample-course", "サンプル授業", "https://example.invalid/ct/course_sample")],
            issues=[],
            synced_at="2026-10-10T12:00:00+09:00",
            complete=True,
            pages_read=3,
        ).as_dict()
        self.assertEqual(result["assignments"][0]["issues"][0]["code"], "content_missing")
        self.assertEqual(result["courses"][0]["name_ja"], "サンプル授業")
        self.assertEqual(result["scope"], "current_courses")
        result["assignments"][0]["issues"].clear()
        self.assertEqual(len(first.issues), 1)


if __name__ == "__main__":
    unittest.main()
