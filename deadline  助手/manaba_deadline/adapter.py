"""Ryukoku-specific, paginated, read-only assignment synchronization."""
from __future__ import annotations

from urllib.parse import urlsplit

from .browser import FetchError, LoginRequired, UnsafeURL
from .config import ScraperConfig
from .models import Assignment, Course, Issue, SyncResult, now_tokyo_iso, sorted_assignments
from .parser import ParsedPage, parse_assignment_detail, parse_assignment_list, parse_course_navigation, parse_courses


class _PageLimit(Exception):
    pass


class TimetableUnavailable(FetchError):
    """No course scope could be confirmed from the weekday timetable."""


class ManabaAdapter:
    """The adapter owns no login credentials or account-specific identifiers.

    Keep the transport open for a watch session; each sync reads new pages and
    returns an independent snapshot. ``complete`` describes discovery coverage;
    inspect each task's content_status/issues for unavailable body material.
    """
    def __init__(self, transport, config: ScraperConfig):
        self.transport = transport
        self.config = config

    async def sync(self) -> SyncResult:
        issues: list[Issue] = []
        assignments: dict[str, Assignment] = {}
        courses: dict[str, Course] = {}
        cache: dict[str, str] = {}
        pages_attempted = 0
        initial_count = self.transport.pages_read
        complete = True
        recognized_pages = 0
        scope_complete = False
        limit_reported = False
        root = "https://manaba.ryukoku.ac.jp/ct/"

        async def fetch(url: str, page_kind: str) -> str | None:
            nonlocal pages_attempted, complete, limit_reported
            if url in cache:
                return cache[url]
            if pages_attempted >= self.config.max_pages:
                complete = False
                if not limit_reported:
                    issues.append(Issue("page_limit", "已达到本次页面读取上限，结果不完整。", page_kind="sync"))
                    limit_reported = True
                raise _PageLimit()
            pages_attempted += 1
            try:
                html = await self.transport.get(url)
            except LoginRequired:
                raise
            except (FetchError, UnsafeURL):
                complete = False
                issues.append(Issue("page_read_failed", "一个页面读取失败，结果可能不完整。", page_kind=page_kind))
                return None
            cache[url] = html
            return html

        async def crawl(start_url, parser, page_kind):
            nonlocal complete, recognized_pages
            queue, visited, items = [start_url], set(), []
            coverage_complete, recognized = True, 0
            while queue:
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                try:
                    html = await fetch(url, page_kind)
                except _PageLimit:
                    return items, False
                if html is None:
                    coverage_complete = False
                    continue
                parsed: ParsedPage = parser(html, url)
                issues.extend(parsed.issues)
                if not parsed.recognized or parsed.issues:
                    complete = False
                    coverage_complete = False
                if parsed.filter_urls:
                    fresh = [dest for dest in parsed.filter_urls if dest not in visited]
                    if fresh:
                        queue = fresh + queue
                        # Ignore data from a filtered page; use its reset route.
                        continue
                    complete = False
                    coverage_complete = False
                    issues.append(Issue("filter_cycle", "筛选重置没有得到未筛选页面，结果可能不完整。", page_kind=page_kind))
                items.extend(parsed.items)
                if parsed.recognized:
                    recognized_pages += 1
                    recognized += 1
                queue.extend(dest for dest in parsed.pagination if dest not in visited)
            return items, coverage_complete and recognized > 0

        def merge(task: Assignment):
            old = assignments.get(task.id)
            if old is None:
                assignments[task.id] = task
                return
            # Course lists override aggregate state; null dates remain explicit
            # when a later page authoritatively states an unset deadline.
            for key in ("course_name_ja", "title_ja", "kind", "starts_at", "deadline_at", "late_deadline_at", "raw_status_ja"):
                if key in {"starts_at", "deadline_at", "late_deadline_at"} and key not in getattr(task, "_known_dates", set()):
                    continue
                setattr(old, key, getattr(task, key))
            if task.submission_status != "unknown":
                old.submission_status = task.submission_status
            if task.availability != "unknown":
                old.availability = task.availability
            old.provenance = list(dict.fromkeys([*old.provenance, *task.provenance]))
            old.issues.extend(task.issues)

        try:
            # Authentication is confirmed on home before any discovery. A
            # blank auth splash must never replace an existing task snapshot.
            await fetch(root + "home", "authentication")
            # Canonical paths encode current courses/all years and all task
            # periods. Parse option states as well to detect unexpected filters.
            discovered, scope_complete = await crawl(root + "home_course", parse_courses, "courses")
            courses.update((course.id, course) for course in discovered)
            if not courses and not scope_complete:
                raise TimetableUnavailable("无法确认星期课程表范围，请在课程列表选择「曜日」后同步。")
            aggregate, _ = await crawl(root + "home_library_query", lambda h, u: parse_assignment_list(h, u, aggregate=True), "aggregate")
            for task in aggregate:
                if task.course_id in courses:
                    merge(task)

            discovered_courses = list(courses.values())
            if len(discovered_courses) > self.config.max_courses:
                complete = False
                issues.append(Issue("course_limit", "已达到课程数量上限，结果不完整。", page_kind="courses"))
            for course in discovered_courses[:self.config.max_courses]:
                html = await fetch(course.url, "course")
                if html is None:
                    continue
                navigation = parse_course_navigation(html, course.url)
                issues.extend(navigation.issues)
                if not navigation.recognized:
                    complete = False
                for list_url in navigation.items:
                    if not urlsplit(list_url).path.startswith(f"/ct/course_{course.id}_"):
                        complete = False
                        issues.append(Issue("navigation_course_mismatch", "课程入口不匹配，已跳过。", page_kind="course"))
                        continue
                    items, _ = await crawl(list_url, lambda h, u: parse_assignment_list(h, u, course), "course_list")
                    for task in items:
                        merge(task)

            # Read instructor instructions only at observed pre-start endpoints.
            # Submitted records remain metadata-only to avoid reading answers.
            for task in assignments.values():
                if task.submission_status == "submitted":
                    continue
                if task.kind in {"external", "project"}:
                    task.content_status = "unsafe_to_open"
                    task.issues.append(Issue("unsafe_detail_skipped", "此类详情入口尚未验证只读安全性，请打开来源查看。", task.id, "detail"))
                    continue
                html = await fetch(task.source_url, "detail")
                if html is None:
                    task.content_status = "error"
                    task.issues.append(Issue("content_read_failed", "正文读取失败。", task.id, "detail"))
                    continue
                parse_assignment_detail(html, task.source_url, task)
        except _PageLimit:
            complete = False

        if recognized_pages == 0:
            raise FetchError("未能识别任何作业或课程列表，已保留上次同步结果。")

        for task in assignments.values():
            task.issues = list({(i.code, i.page_kind): i for i in task.issues}.values())
            issues.extend(task.issues)
        failures = {"date_parse_error", "date_column_missing", "detail_unrecognized", "course_name_missing", "course_label_mismatch", "content_read_failed"}
        if any(issue.code in failures for issue in issues):
            complete = False
        return SyncResult(sorted_assignments(list(assignments.values())), list(courses.values()), issues, now_tokyo_iso(), complete,
                          self.transport.pages_read - initial_count, scope="weekday_timetable", scope_complete=scope_complete)
