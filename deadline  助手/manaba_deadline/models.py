"""Shared scraper data and Chinese display helpers.

Dates read from manaba are local to Japan and are serialized with an explicit
``+09:00`` offset.  Japanese course names, titles, bodies and source status text
are kept verbatim; only presentation labels and date strings are transformed.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


try:
    TOKYO = ZoneInfo("Asia/Tokyo")
except ZoneInfoNotFoundError:
    # Windows need not have an IANA timezone database. Modern Japanese course
    # dates use UTC+09:00 throughout the year, with no daylight-saving changes.
    TOKYO = timezone(timedelta(hours=9), name="Asia/Tokyo")


class _Serializable:
    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready dictionary, recursively converting dataclasses."""
        return asdict(self)


@dataclass
class Issue(_Serializable):
    """A generic diagnostic; messages must not contain private page content."""

    code: str
    message_zh: str
    assignment_id: str | None = None
    page_kind: str | None = None


@dataclass
class Course(_Serializable):
    id: str
    name_ja: str
    url: str


@dataclass
class Assignment(_Serializable):
    id: str
    course_id: str
    course_name_ja: str
    title_ja: str
    kind: str
    source_url: str
    content_ja: str | None = None
    content_status: str = "not_loaded"
    starts_at: str | None = None
    deadline_at: str | None = None
    late_deadline_at: str | None = None
    submission_status: str = "unknown"
    availability: str = "unknown"
    raw_status_ja: str | None = None
    provenance: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def view(self, now: datetime | str | None = None) -> dict[str, Any]:
        """Return source fields plus labels suitable for a Chinese desktop UI.

        ``now`` may be an aware datetime or an ISO 8601 datetime with an offset.
        A stated late deadline takes over once the original deadline is reached.
        No deadline is invented from a date without a time or missing source data.
        """
        current = _current_time(now)
        original = _iso_time(self.deadline_at)
        effective, is_late = _effective_deadline(self, current)
        starts = _iso_time(self.starts_at)
        data = self.as_dict()

        if effective is None:
            remaining = None
            deadline_display = "截止时间未知"
            countdown = "截止时间未知"
        else:
            difference = (effective - current).total_seconds()
            # Round away from zero so a fractional last second is not shown as
            # already expired, and a fractional overdue second is not hidden.
            remaining = int(math.copysign(math.ceil(abs(difference)), difference))
            date_text = _display_date(effective)
            if is_late:
                deadline_display = f"迟交截止：{date_text}（日本时间）"
                if original is not None:
                    deadline_display += f"；原截止：{_display_date(original)}"
            else:
                deadline_display = f"{date_text}（日本时间）"
            if remaining > 0:
                countdown = f"剩余 {_duration(remaining)}"
            elif remaining < 0:
                countdown = f"已逾期 {_duration(-remaining)}"
            else:
                countdown = "已截止"

        submission = _SUBMISSION_LABELS.get(self.submission_status, "提交状态未知")
        if self.submission_status == "submitted":
            status = submission
        else:
            if effective is not None and current >= effective:
                availability = "已截止"
            elif starts is not None and current < starts:
                availability = "尚未开放"
            elif is_late:
                availability = "可迟交"
            else:
                availability = _AVAILABILITY_LABELS.get(self.availability)
            status = f"{submission} · {availability}" if availability else submission

        data.update(
            deadline_display_zh=deadline_display,
            countdown_zh=countdown,
            remaining_seconds=remaining,
            status_zh=status,
            kind_zh=_KIND_LABELS.get(self.kind, "其他"),
            content_status_zh=_CONTENT_LABELS.get(self.content_status, "内容状态未知"),
        )
        return data


@dataclass
class SyncResult(_Serializable):
    assignments: list[Assignment]
    courses: list[Course]
    issues: list[Issue]
    synced_at: str
    complete: bool
    pages_read: int
    scope: str = "current_courses"
    # Independent of task/detail failures: can this course set safely replace
    # the previous allowed scope, including courses removed from the timetable?
    scope_complete: bool = False


_KIND_LABELS = {
    "quiz": "小测",
    "survey": "问卷",
    "report": "报告",
    "project": "项目",
    "external": "外部教材",
    "other": "其他",
    "unknown": "类型未知",
}
_SUBMISSION_LABELS = {
    "submitted": "已提交",
    "unsubmitted": "未提交",
    "pending": "未提交",
    "in_progress": "进行中",
    "unknown": "提交状态未知",
}
_AVAILABILITY_LABELS = {
    "open": "开放中",
    "not_started": "尚未开放",
    "closed": "已关闭",
}
_CONTENT_LABELS = {
    "not_loaded": "尚未读取内容",
    "loaded": "内容已读取",
    "empty": "没有正文内容",
    "unavailable": "无法读取内容",
    "unsafe_to_open": "需要启动作业，未读取正文",
    "not_available": "正文未提供",
    "parse_error": "正文解析失败",
    "error": "正文读取失败",
}

_WEEKDAY = r"(?:日|月|火|水|木|金|土)(?:曜日|曜)?"
_BRACKETED_WEEKDAY = rf"(?:\({_WEEKDAY}\)|\[{_WEEKDAY}\]|【{_WEEKDAY}】)"
_TIME = r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
# Numeric dates need a visible boundary before the time: whitespace, a weekday
# bracket, or ISO's T. Otherwise 2026/1/112:30 has ambiguous day/hour digits.
_NUMERIC_SUFFIX = rf"(?:\s*T\s*|\s+(?:{_BRACKETED_WEEKDAY}\s*)?|{_BRACKETED_WEEKDAY}\s*){_TIME}"
_JAPANESE_SUFFIX = rf"\s*(?:{_BRACKETED_WEEKDAY}\s*)?(?:T\s*)?{_TIME}"
_DATE_PATTERNS = (
    re.compile(r"(?P<year>\d{4})/(?P<month>\d{1,2})/(?P<day>\d{1,2})" + _NUMERIC_SUFFIX),
    re.compile(r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})" + _NUMERIC_SUFFIX),
    re.compile(r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日" + _JAPANESE_SUFFIX),
)
_NO_DATE = {"", "-", "--", "—", "–", "ー", "無期限", "未設定", "設定なし", "期限なし", "なし"}


def parse_manaba_datetime(text: str) -> str | None:
    """Parse an explicit manaba date *and* time in the Japan timezone.

    Supports slash, hyphen and Japanese date notation, optional Japanese
    weekdays in brackets, fullwidth date characters and ``24:00``. Empty and
    explicit no-deadline values return ``None``; malformed or partial dates
    raise ``ValueError``. NFKC normalization applies only inside this helper.
    """
    normalized = unicodedata.normalize("NFKC", text).strip()
    if re.sub(r"\s+", "", normalized) in _NO_DATE:
        return None
    match = next((match for pattern in _DATE_PATTERNS if (match := pattern.fullmatch(normalized))), None)
    if match is None:
        raise ValueError("Unrecognized manaba date/time format.")
    values = {key: int(value or "0") for key, value in match.groupdict().items()}
    hour = values.pop("hour")
    next_day = hour == 24
    if next_day and (values["minute"] != 0 or values["second"] != 0):
        raise ValueError("24:00 must have zero minutes and seconds.")
    try:
        parsed = datetime(hour=0 if next_day else hour, tzinfo=TOKYO, **values)
        if next_day:
            parsed += timedelta(days=1)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Invalid manaba date/time values.") from exc
    return parsed.isoformat(timespec="seconds")


def now_tokyo_iso() -> str:
    return datetime.now(TOKYO).isoformat(timespec="seconds")


def sorted_assignments(
    assignments: list[Assignment], now: datetime | str | None = None
) -> list[Assignment]:
    """Return a new list ordered by effective deadline, with undated work last."""
    current = _current_time(now)

    def key(assignment: Assignment) -> tuple[bool, datetime, str]:
        deadline, _ = _effective_deadline(assignment, current)
        return deadline is None, deadline or datetime.max.replace(tzinfo=TOKYO), assignment.id

    return sorted(assignments, key=key)


def _iso_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("ISO datetime must include a timezone offset.")
    return parsed.astimezone(TOKYO)


def _current_time(now: datetime | str | None) -> datetime:
    if now is None:
        return datetime.now(TOKYO)
    current = _iso_time(now) if isinstance(now, str) else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Current datetime must include a timezone offset.")
    return current.astimezone(TOKYO)


def _effective_deadline(assignment: Assignment, now: datetime) -> tuple[datetime | None, bool]:
    original = _iso_time(assignment.deadline_at)
    late = _iso_time(assignment.late_deadline_at)
    if late is not None and (original is None or (now >= original and late > original)):
        return late, True
    return original, False


def _display_date(value: datetime) -> str:
    text = value.strftime("%Y年%m月%d日 %H:%M")
    return text + value.strftime(":%S") if value.second else text


def _duration(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if days or hours:
        parts.append(f"{hours}小时")
    if days or hours or minutes:
        parts.append(f"{minutes}分")
    parts.append(f"{seconds}秒")
    return " ".join(parts)
