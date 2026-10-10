"""Parse only assignment/course metadata and pre-start instructor instructions.

The DOM contract is based on Ryukoku manaba 2.98. Forms, student answers,
team boards and peer submissions are deliberately outside that contract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .models import Assignment, Course, Issue, parse_manaba_datetime

DETAIL = re.compile(r"/ct/course_(\d+)_(query|survey|report|project|drill)_(\d+)$")
COURSE = re.compile(r"/ct/course_(\d+)$")
LIST = re.compile(r"/ct/course_(\d+)_(query|survey|report|project)$")
KINDS = {"query": "quiz", "survey": "survey", "report": "report", "project": "project", "drill": "external"}
TYPES = {"小テスト": "quiz", "アンケート": "survey", "レポート": "report", "プロジェクト": "project", "外部教材": "external"}
CONTENT_LABELS = {"問題", "課題に関する説明", "課題内容", "説明", "プロジェクトの説明", "プロジェクト内容"}
LATE_LABELS = {"遅延提出期限", "遅延提出締切", "遅延提出受付終了日時", "遅延提出受付終了", "提出可能日時", "提出可能期限", "提出期限（遅延提出）"}
UNSAFE_CONTENT = re.compile(r"student|answer|submission|peer|team|comment|thread|collection", re.I)


@dataclass
class ParsedPage:
    items: list = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    pagination: list[str] = field(default_factory=list)
    filter_urls: list[str] = field(default_factory=list)
    recognized: bool = True


def soup_for(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def label(node: Tag) -> str:
    return re.sub(r"\s+", "", node.get_text(" ", strip=True))


def _unsafe_content_node(node: Tag) -> bool:
    return bool(UNSAFE_CONTENT.search(" ".join(node.get("class", [])) + " " + node.get("id", "")))


def text(node: Tag, *, instructor_content: bool = False) -> str:
    """Keep Japanese characters; preserve paragraph and line-break boundaries."""
    copy = soup_for(str(node))
    for excluded in copy.select("script,style,form,input,button,select,textarea"):
        excluded.decompose()
    if instructor_content:
        for excluded in list(copy.find_all(_unsafe_content_node)):
            if excluded.parent is not None:
                excluded.decompose()
    for br in copy.find_all("br"):
        br.replace_with("\n")
    for block in copy.find_all(["p", "div", "li", "tr"]):
        block.append("\n")
    value = copy.get_text()
    return "\n".join(line.strip() for line in value.splitlines() if line.strip()).strip()


def absolute_link(href: str, base_url: str) -> str | None:
    candidate = urlsplit(urljoin(base_url, href))
    base = urlsplit(base_url)
    if candidate.scheme != "https" or candidate.netloc != base.netloc or candidate.username or candidate.password:
        return None
    return urlunsplit((candidate.scheme, candidate.netloc, candidate.path, candidate.query, ""))


def _pagination(soup: BeautifulSoup, url: str) -> tuple[list[str], list[Issue]]:
    """Follow displayed pagination, never arbitrary numbers/settings links."""
    result, issues = [], []
    for a in soup.find_all("a", href=True):
        parent_classes = " ".join(" ".join(p.get("class", [])) for p in [a, *list(a.parents)[:2]] if isinstance(p, Tag))
        rel = a.get("rel", [])
        is_page = bool(re.search(r"pager|paging|pagination", parent_classes, re.I))
        is_next = bool(set(rel) & {"next", "prev"}) or label(a) in {"次へ", "前へ", "次のページ", "前のページ", "次", "前", ">", "<", "≫", "≪"}
        if not (is_page or is_next):
            continue
        dest = absolute_link(a["href"], url)
        if dest and dest != url and urlsplit(dest).path == urlsplit(url).path:
            result.append(dest)
        elif dest != url:
            issues.append(Issue("pagination_unsupported", "检测到不受支持的分页入口，结果可能不完整。", page_kind="pagination"))
    return list(dict.fromkeys(result)), issues


def _filters(soup: BeautifulSoup, url: str, desired: set[str]) -> tuple[list[str], list[Issue]]:
    urls, issues = [], []
    for select in soup.find_all("select"):
        options = select.find_all("option")
        wanted = next((o for o in options if label(o) in desired), None)
        if wanted is None:
            continue
        selected = next((o for o in options if o.has_attr("selected")), options[0] if options else None)
        if selected is wanted:
            continue
        value = wanted.get("value", "")
        dest = absolute_link(value, url) if value else None
        if dest:
            # Course options also carry a layout-setting parameter. Removing
            # it retains the observed filter route without changing preferences.
            parsed = urlsplit(dest)
            if "chglistformat=" in parsed.query:
                dest = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            urls.append(dest)
        else:
            issues.append(Issue("filter_unresolved", "页面筛选无法重置，可能遗漏作业。", page_kind="list"))
    return list(dict.fromkeys(urls)), issues


def parse_courses(html: str, url: str) -> ParsedPage:
    """Discover courses only in the Monday–Saturday timetable.

    The surrounding course area also contains miscellaneous courses, and home
    has course links in submission history. Neither belongs to this scope.
    Recognize the table by its weekday columns rather than an account's IDs or
    a wrapper class shared by multiple sections.
    """
    soup = soup_for(html)
    pages, issues = _pagination(soup, url)
    result = ParsedPage(pagination=pages, issues=issues)
    result.filter_urls, filter_issues = _filters(soup, url, {"現在のコース", "すべての年度"})
    result.issues.extend(filter_issues)
    courses: dict[str, Course] = {}
    timetable_found = False
    weekdays = ["月", "火", "水", "木", "金", "土"]
    for table in soup.find_all("table"):
        rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        if not rows:
            continue
        headers = [label(cell) for cell in rows[0].find_all(["th", "td"], recursive=False)]
        offset = 1 if len(headers) == 7 else 0
        if headers[offset:] != weekdays:
            continue
        timetable_found = True
        for row in rows[1:]:
            cells = row.find_all(["th", "td"], recursive=False)
            # Some layouts append a miscellaneous section to the same table.
            if any(label(cell) == "その他の曜日" for cell in cells):
                break
            if len(cells) != len(headers) or any(cell.get("colspan", "1") != "1" or cell.get("rowspan", "1") != "1" for cell in cells):
                if row.find("a", href=True):
                    result.issues.append(Issue("timetable_row_unrecognized", "课程表的一行结构无法识别，课程范围可能不完整。", page_kind="courses"))
                continue
            for cell in cells[offset:]:
                for a in cell.find_all("a", href=True):
                    if a.find_parent("table") is not table:
                        continue
                    dest = absolute_link(a["href"], url)
                    match = COURSE.fullmatch(urlsplit(dest).path) if dest else None
                    if match:
                        name = text(a)
                        if not name:
                            result.issues.append(Issue("timetable_course_name_missing", "课程表中的一个课程名称无法读取，已保留其课程入口。", page_kind="courses"))
                        courses.setdefault(match[1], Course(match[1], name, dest))
    result.items = list(courses.values())
    visible = soup.get_text(" ")
    has_course_links = any(
        dest and COURSE.fullmatch(urlsplit(dest).path)
        for a in soup.find_all("a", href=True)
        for dest in [absolute_link(a["href"], url)]
    )
    empty_message = re.compile(r"^(?:登録されている|現在の|該当する)?コース(?:は|が)?(?:ありません|登録されていません|見つかりません)[。.!！]?$" )
    empty = (
        not has_course_links
        and not re.search(r"権限|アクセスでき|ログイン|エラー", visible)
        and any(empty_message.fullmatch(label(node)) for node in soup.find_all(["p", "div", "td"]))
    )
    if not timetable_found and not empty:
        result.recognized = False
        result.issues.append(Issue("courses_unrecognized", "无法识别周一至周六课程表。请在 manaba 课程列表选择「曜日」后同步。", page_kind="courses"))
    return result


def parse_course_navigation(html: str, url: str) -> ParsedPage:
    soup = soup_for(html)
    result = ParsedPage()
    for a in soup.find_all("a", href=True):
        dest = absolute_link(a["href"], url)
        if dest and LIST.fullmatch(urlsplit(dest).path):
            result.items.append(dest)
    result.items = list(dict.fromkeys(result.items))
    if not result.items:
        result.recognized = False
        result.issues.append(Issue("navigation_unrecognized", "无法识别课程作业入口。", page_kind="course"))
    return result


def _read_date(cell: Tag | None, assignment: Assignment, field_name: str) -> None:
    if cell is None:
        assignment.issues.append(Issue("date_column_missing", "页面缺少时间字段。", assignment.id, "date:" + field_name))
        return
    value = text(cell)
    try:
        parsed = parse_manaba_datetime(value)
    except ValueError:
        assignment.issues.append(Issue("date_parse_error", "当前页面时间格式无法解析。", assignment.id, "date:" + field_name))
        return
    setattr(assignment, field_name, parsed)
    # Internal parse evidence distinguishes an explicit unset value from a
    # missing/unparsed field when course metadata is merged with the aggregate.
    assignment._known_dates = {*getattr(assignment, "_known_dates", set()), field_name}
    if parsed is None and field_name == "deadline_at":
        assignment.issues.append(Issue("deadline_missing", "平台没有提供截止时间。", assignment.id, "date"))


def status_from_text(value: str) -> tuple[str, str]:
    normalized = re.sub(r"\s+", "", value)
    if re.search(r"未提出|未回答|未解答|まだ提出していません|まだ回答していません", normalized):
        submission = "unsubmitted"
    elif re.search(r"提出済|回答済|解答済|提出しています|提出しました", normalized):
        submission = "submitted"
    elif re.search(r"回答中|解答中|実施中", normalized):
        submission = "in_progress"
    else:
        submission = "unknown"
    if re.search(r"受付開始待ち|開始前|受付開始前", normalized):
        availability = "not_started"
    elif re.search(r"受付終了|公開終了|終了しました", normalized):
        availability = "closed"
    elif re.search(r"受付中|実施中|提出可能", normalized):
        availability = "open"
    else:
        availability = "unknown"
    return submission, availability


def parse_assignment_list(html: str, url: str, course: Course | None = None, *, aggregate: bool = False) -> ParsedPage:
    soup = soup_for(html)
    pages, issues = _pagination(soup, url)
    result = ParsedPage(pagination=pages, issues=issues)
    if aggregate:
        result.filter_urls, filter_issues = _filters(soup, url, {"全課題", "受付終了まで:全期間"})
        result.issues.extend(filter_issues)
    found_table = False
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [label(c) for c in rows[0].find_all(["th", "td"], recursive=False)]
        if not ({"タイトル", "プロジェクト名", "課題名"} & set(headers)):
            continue
        found_table = True
        def column(cells, names):
            index = next((headers.index(n) for n in names if n in headers), None)
            return cells[index] if index is not None and index < len(cells) else None
        for row in rows[1:]:
            cells = row.find_all(["th", "td"], recursive=False)
            title_cell = column(cells, ("タイトル", "プロジェクト名", "課題名"))
            if title_cell is None:
                if cells and not any("ありません" in text(c) for c in cells):
                    result.issues.append(Issue("row_unrecognized", "无法识别一行作业，可能遗漏记录。", page_kind="list"))
                continue
            dest, match, title_anchor = None, None, None
            for a in title_cell.find_all("a", href=True):
                candidate = absolute_link(a["href"], url)
                candidate_match = DETAIL.fullmatch(urlsplit(candidate).path) if candidate else None
                if candidate_match:
                    dest, match, title_anchor = candidate, candidate_match, a
                    break
            if match is None:
                # Project lists include teams/projects the current user has
                # not joined. Ryukoku renders those titles without anchors.
                if urlsplit(url).path.endswith("_project") and not title_cell.find("a") and "参加しているプロジェクトのみクリック" in soup.get_text():
                    continue
                if text(title_cell) and not re.search(r"ありません|見つかりません", text(title_cell)):
                    result.issues.append(Issue("identity_unrecognized", "作业入口或身份格式不受支持，未猜测链接。", page_kind="list"))
                continue
            course_id, route, task_id = match.groups()
            if course and course_id != course.id:
                result.issues.append(Issue("course_mismatch", "作业链接与课程不匹配，已跳过。", page_kind="list"))
                continue
            course_name = course.name_ja if course else ""
            course_cell = column(cells, ("コース", "コース名"))
            course_label_mismatch = False
            if course_cell:
                for a in course_cell.find_all("a", href=True):
                    candidate = absolute_link(a["href"], url)
                    course_match = COURSE.fullmatch(urlsplit(candidate).path) if candidate else None
                    if course_match and course_match[1] == course_id:
                        course_name = text(a)
                        course_label_mismatch = False
                        break
                    course_label_mismatch = True
            type_cell = column(cells, ("タイプ", "種類"))
            kind = TYPES.get(label(type_cell), KINDS[route]) if type_cell else KINDS[route]
            task = Assignment(f"ryukoku:{course_id}:{route}:{task_id}", course_id, course_name, text(title_anchor), kind, dest,
                              provenance=["aggregate" if aggregate else "course_list"])
            if course_label_mismatch:
                task.issues.append(Issue("course_label_mismatch", "课程名称链接与作业所属课程不匹配，未采用该名称。", task.id, "list"))
            status_cell = column(cells, ("状態", "提出状況"))
            if status_cell:
                task.raw_status_ja = text(status_cell)
                task.submission_status, task.availability = status_from_text(task.raw_status_ja)
            elif aggregate:
                task.submission_status = "unsubmitted"
            _read_date(column(cells, ("受付開始日時", "開始日時")), task, "starts_at")
            _read_date(column(cells, ("受付終了日時", "提出期限", "締切日時")), task, "deadline_at")
            late_cell = column(cells, tuple(LATE_LABELS))
            if late_cell:
                _read_date(late_cell, task, "late_deadline_at")
            if not task.course_name_ja:
                task.issues.append(Issue("course_name_missing", "缺少课程名称。", task.id, "list"))
            result.items.append(task)
    if not found_table:
        # These messages are observed empty-list wording; arbitrary blank pages
        # or a failed login are not accepted as successful empty lists.
        visible = soup.get_text(" ", strip=True)
        empty = not re.search(r"権限|アクセスでき|ログイン|エラー", visible) and bool(re.search(r"(?:小テスト|アンケート|レポート|プロジェクト|課題).*(?:ありません|見つかりません)", visible))
        result.recognized = empty
        if not empty:
            result.issues.append(Issue("list_unrecognized", "无法识别作业列表，未把解析失败当作没有作业。", page_kind="list"))
    return result


def parse_assignment_detail(html: str, url: str, assignment: Assignment) -> Assignment:
    soup = soup_for(html)
    recognized, content_nodes, attachment_seen = False, [], False
    summary = None
    for table in soup.find_all("table", class_=lambda value: value and "stdlist" in value):
        if any(parent.name == "form" or _unsafe_content_node(parent)
               for parent in [table, *table.parents] if isinstance(parent, Tag)):
            continue
        rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
        keys = {label(r.find("th", recursive=False)) for r in rows if r.find("th", recursive=False)}
        # Only the first instructor summary table, recognizable by its date
        # labels, is allowed. Later answer/result tables are never appended.
        if keys & {"受付開始日時", "受付終了日時"}:
            summary = table
            break
    if summary is not None:
        for row in [r for r in summary.find_all("tr") if r.find_parent("table") is summary]:
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) != 2 or cells[0].name != "th":
                continue
            key, value = label(cells[0]), cells[1]
            if key in CONTENT_LABELS:
                recognized = True
                content_nodes.append(value)
            elif key == "受付開始日時":
                recognized = True
                _read_date(value, assignment, "starts_at")
            elif key in {"受付終了日時", "提出期限", "締切日時"}:
                recognized = True
                _read_date(value, assignment, "deadline_at")
            elif key in LATE_LABELS or ("遅延提出" in key and ("期限" in key or "終了" in key)):
                recognized = True
                _read_date(value, assignment, "late_deadline_at")
            elif key == "状態":
                recognized = True
                assignment.raw_status_ja = text(value)
                submission, availability = status_from_text(assignment.raw_status_ja)
                if submission != "unknown":
                    assignment.submission_status = submission
                if availability != "unknown":
                    assignment.availability = availability
            elif key == "添付ファイル":
                attachment_seen = bool(value.find("a", href=True) or value.find("img"))
    if content_nodes:
        excluded_sections = any(_unsafe_content_node(node) or node.find(_unsafe_content_node) for node in content_nodes)
        assignment.content_ja = "\n\n".join(filter(None, (text(node, instructor_content=True) for node in content_nodes)))
        assignment.content_status = "loaded" if assignment.content_ja else "empty"
        if excluded_sections:
            assignment.issues.append(Issue("content_sections_excluded", "正文中包含答题或学生区域，已排除这些区域。", assignment.id, "detail"))
            if not assignment.content_ja:
                assignment.content_ja = None
                assignment.content_status = "unavailable"
        if any(node.find(["img", "iframe", "object", "embed"]) for node in content_nodes) or attachment_seen:
            assignment.issues.append(Issue("content_has_assets", "正文包含图片或附件；当前只提取页面文字，请在来源查看完整材料。", assignment.id, "detail"))
    elif recognized:
        assignment.content_status = "unsafe_to_open" if assignment.kind in {"quiz", "survey"} else "unavailable"
        assignment.issues.append(Issue("content_unavailable", "此入口没有可安全读取的正文说明。", assignment.id, "detail"))
    else:
        assignment.content_status = "parse_error"
        assignment.issues.append(Issue("detail_unrecognized", "无法识别详情结构，未读取答题区域或学生提交内容。", assignment.id, "detail"))
    if recognized:
        assignment.provenance = list(dict.fromkeys([*assignment.provenance, "detail"]))
    # Keep diagnostics unique when the list and detail both state no deadline.
    assignment.issues = list({(i.code, i.page_kind): i for i in assignment.issues}.values())
    return assignment
