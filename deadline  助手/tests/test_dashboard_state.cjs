"use strict";

// Synthetic dates and names only: UI-state checks never need a campus session.
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  effectiveDeadline,
  taskState,
  priorityAssignments,
  groupCourses,
  countdown,
} = require("../manaba_deadline/dashboard.js");

const NOW = Date.parse("2026-10-10T12:00:00+09:00");
const PAST = "2026-10-10T11:00:00+09:00";
const NEXT = "2026-10-10T13:00:00+09:00";
const LATER = "2026-10-11T12:00:00+09:00";

function task(id, fields = {}) {
  return {
    id,
    course_id: "sample-1",
    course_name_ja: "サンプル授業",
    title_ja: "課題 " + id,
    deadline_at: NEXT,
    late_deadline_at: null,
    starts_at: null,
    submission_status: "unsubmitted",
    availability: "open",
    ...fields,
  };
}

function course(id = "sample-1") {
  return { id, name_ja: "サンプル授業 " + id };
}

test("completed work is never shown as an overdue or urgent action", () => {
  for (const fields of [
    { deadline_at: PAST },
    { deadline_at: NEXT, starts_at: LATER, availability: "not_started" },
    { deadline_at: null },
    { deadline_at: PAST, late_deadline_at: LATER, availability: "closed" },
  ]) {
    const state = taskState(task("done", { ...fields, submission_status: "submitted" }), NOW);
    assert.equal(state.state, "submitted");
    assert.equal(state.urgency, "done");
  }
});

test("unopened work still exposes its future deadline and remaining time", () => {
  const assignment = task("future", {
    starts_at: NEXT,
    deadline_at: LATER,
    availability: "not_started",
  });
  const state = taskState(assignment, NOW);
  assert.equal(state.state, "not_started");
  assert.equal(state.deadline, Date.parse(LATER));
  assert.equal(state.remaining, 86400);
});

test("late submission begins precisely at the original deadline", () => {
  const original = Date.parse(NEXT);
  const late = Date.parse(LATER);
  const assignment = task("late", { late_deadline_at: LATER });
  assert.equal(effectiveDeadline(assignment, original - 1), original);
  assert.equal(taskState(assignment, original - 1).isLate, false);
  assert.equal(effectiveDeadline(assignment, original), late);
  const state = taskState(assignment, original);
  assert.equal(state.isLate, true);
  assert.equal(state.state, "pending");
  assert.ok(state.remaining > 0);
  const expired = taskState(assignment, late + 1000);
  assert.equal(expired.state, "overdue");
  assert.equal(expired.urgency, "overdue");
  assert.ok(expired.remaining < 0);
});

test("an earlier late deadline cannot shorten or revive the original deadline", () => {
  const assignment = task("invalid-late-order", { late_deadline_at: PAST });
  assert.equal(effectiveDeadline(assignment, NOW), Date.parse(NEXT));
  assert.equal(taskState(assignment, NOW).isLate, false);
  assert.equal(effectiveDeadline(assignment, Date.parse(NEXT) + 1000), Date.parse(NEXT));
  assert.equal(taskState(assignment, Date.parse(NEXT) + 1000).state, "overdue");
});

test("an available late deadline keeps work actionable after the original period closes", () => {
  const assignment = task("late-after-close", {deadline_at: PAST, late_deadline_at: LATER, availability: "closed"});
  assert.equal(taskState(assignment, NOW).state, "pending");
  assert.strictEqual(groupCourses([course()], [assignment], NOW)[0].next, assignment);
});

test("missing and malformed deadline values remain unknown", () => {
  for (const deadline_at of [null, undefined, "", "not-a-date"]) {
    const assignment = task("unknown", { deadline_at });
    assert.equal(effectiveDeadline(assignment, NOW), null);
    const state = taskState(assignment, NOW);
    assert.equal(state.state, "unknown");
    assert.equal(state.deadline, null);
    assert.equal(state.remaining, null);
    assert.equal(state.urgency, "unknown");
  }
});

test("course grouping retains raw work and chooses the nearest future usable deadline", () => {
  const overdue = task("overdue", { deadline_at: PAST });
  const completed = task("completed", {
    deadline_at: "2026-10-10T12:01:00+09:00",
    submission_status: "submitted",
  });
  const closed = task("closed", {
    deadline_at: "2026-10-10T12:02:00+09:00",
    availability: "closed",
  });
  const next = task("next");
  const later = task("later", { deadline_at: LATER });
  const unknown = task("unknown", { deadline_at: null });
  const source = [unknown, later, completed, overdue, closed, next];
  const groups = groupCourses([course()], source, NOW);
  assert.equal(groups.length, 1);
  const group = groups[0];
  assert.equal(group.id, "sample-1");
  assert.equal(group.name, course().name_ja);
  assert.equal(group.items.length, source.length);
  assert.ok(source.every((assignment) => group.items.includes(assignment)));
  assert.strictEqual(group.next, next);
  assert.equal(group.overdueCount, 1);
  assert.equal(group.unknownCount, 1);
  assert.deepEqual(group.submitted, [completed]);
  assert.ok(group.pending.includes(overdue));
  assert.ok(group.pending.includes(unknown));
  assert.ok(!group.pending.includes(completed));
});

test("a course with only completed or unknown work has no upcoming deadline", () => {
  const groups = groupCourses([course()], [
    task("done", { submission_status: "submitted" }),
    task("unknown", { deadline_at: null }),
  ], NOW);
  assert.equal(groups[0].next, null);
});

test("empty timetable courses stay visible and course colors survive refreshes", () => {
  const courses = Array.from({ length: 12 }, (_, index) => course("sample-" + index));
  const first = groupCourses(courses, [], NOW);
  const refreshed = groupCourses([...courses].reverse(), [], NOW + 1000);
  assert.equal(first.length, courses.length);
  assert.equal(new Set(first.map((group) => group.color)).size, 12);
  for (const group of first) {
    assert.equal(typeof group.color, "number");
    assert.deepEqual(group.items, []);
    assert.deepEqual(group.pending, []);
    assert.deepEqual(group.submitted, []);
    assert.equal(group.next, null);
    assert.equal(group.overdueCount, 0);
    assert.equal(group.unknownCount, 0);
    assert.equal(refreshed.find((candidate) => candidate.id === group.id).color, group.color);
  }
});

test("countdown distinguishes future, expired, exact-boundary, and absent deadlines", () => {
  assert.match(countdown(Date.parse(NEXT), NOW), /^剩余 /);
  assert.match(countdown(Date.parse(PAST), NOW), /^已逾期 /);
  assert.equal(countdown(NOW, NOW), "已截止");
  assert.match(countdown(NOW + 1, NOW), /^剩余 /);
  assert.match(countdown(NOW - 1, NOW), /^已逾期 /);
  for (const value of [null, NaN, Infinity]) {
    assert.equal(countdown(value, NOW), "截止时间未知");
  }
});

test("priority work is confirmed unsubmitted with a usable future deadline, sorted by deadline", () => {
  const next = task("next");
  const late = task("late", {
    deadline_at: PAST,
    late_deadline_at: "2026-10-10T14:00:00+09:00",
    availability: "closed",
  });
  const unopened = task("unopened", {
    starts_at: NEXT,
    deadline_at: LATER,
    availability: "not_started",
  });
  const excluded = [
    task("overdue", { deadline_at: PAST }),
    task("submitted", { submission_status: "submitted" }),
    task("in-progress", { submission_status: "in_progress" }),
    task("submission-unknown", { submission_status: "unknown" }),
    task("closed", { availability: "closed" }),
    task("deadline-unknown", { deadline_at: null }),
    task("deadline-invalid", { deadline_at: "not-a-date" }),
  ];
  const source = [unopened, ...excluded, late, next];
  const before = [...source];
  const priority = priorityAssignments(source, NOW);
  assert.deepEqual(priority, [next, late, unopened]);
  assert.strictEqual(priority[0], next);
  assert.strictEqual(priority[1], late);
  assert.strictEqual(priority[2], unopened);
  assert.deepEqual(source, before);
});

test("priority work leaves at its exact deadline unless a later submission deadline is still valid", () => {
  const boundary = Date.parse(NEXT);
  const ordinary = task("ordinary");
  const late = task("late", { late_deadline_at: LATER, availability: "closed" });
  assert.deepEqual(priorityAssignments([ordinary], boundary - 1), [ordinary]);
  assert.deepEqual(priorityAssignments([ordinary, late], boundary), [late]);
  assert.deepEqual(priorityAssignments([late], Date.parse(LATER) - 1), [late]);
  assert.deepEqual(priorityAssignments([ordinary, late], Date.parse(LATER)), []);
});

test("courses with unsubmitted future work precede overdue and unconfirmed courses", () => {
  const near = task("near", { course_id: "priority-near" });
  const later = task("later", { course_id: "priority-later", deadline_at: LATER });
  const overdue = task("overdue", { course_id: "overdue-only", deadline_at: PAST });
  const unknown = task("unknown", {
    course_id: "unconfirmed",
    deadline_at: "2026-10-10T12:01:00+09:00",
    submission_status: "unknown",
  });
  const groups = groupCourses([
    course("overdue-only"), course("unconfirmed"),
    course("priority-later"), course("priority-near"),
  ], [overdue, unknown, later, near], NOW);
  assert.deepEqual(groups.slice(0, 2).map(group => group.id), ["priority-near", "priority-later"]);
  assert.equal(groups.length, 4);
  assert.ok(groups.flatMap(group => group.items).includes(overdue));
  assert.ok(groups.flatMap(group => group.items).includes(unknown));
});

test("each course puts its unsubmitted future work before overdue work and retains every task", () => {
  const overdue = task("overdue", { deadline_at: PAST });
  const next = task("next");
  const later = task("later", { deadline_at: LATER });
  const unknown = task("unknown", { submission_status: "unknown" });
  const closed = task("closed", { availability: "closed" });
  const submitted = task("submitted", { submission_status: "submitted" });
  const source = [overdue, unknown, closed, submitted, later, next];
  const [group] = groupCourses([course()], source, NOW);
  assert.deepEqual(group.pending.slice(0, 2), [next, later]);
  assert.equal(group.pending.length, 5);
  assert.ok(group.pending.includes(overdue));
  assert.ok(group.pending.includes(unknown));
  assert.ok(group.pending.includes(closed));
  assert.deepEqual(group.submitted, [submitted]);
  assert.equal(group.items.length, source.length);
  assert.ok(source.every(assignment => group.items.includes(assignment)));
});
