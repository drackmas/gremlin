"""Persistent plan state for autonomous coding and self-refactoring.

``plan.json`` at the project root is the single machine-readable source of
truth. It lives outside the LLM context, so the plan survives context
compaction and process restarts; the store re-reads it on every operation.
``plan.md`` is an optional human-readable view rendered from the same state.

A plan has a goal, a status, and an ordered list of phases. Each phase holds
tasks with a status, optional progress (0-100), free-form notes, a recorded
test result, and dependencies on other task ids. Phase status is always
derived from its tasks so the two can never disagree.

This is deliberately plain: dataclasses + one JSON file + a git subprocess
for checkpoints. No database, no scheduler, no new framework.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from utils import atomic_write_json, now_utc

log = logging.getLogger("gremlin.planning")

PLAN_VERSION = 1

# --- task statuses -------------------------------------------------------
TASK_PENDING = "pending"
TASK_IN_PROGRESS = "in_progress"
TASK_DONE = "done"
TASK_BLOCKED = "blocked"
TASK_SKIPPED = "skipped"
TASK_STATUSES = (TASK_PENDING, TASK_IN_PROGRESS, TASK_DONE, TASK_BLOCKED, TASK_SKIPPED)

# --- plan statuses ---------------------------------------------------------
PLAN_ACTIVE = "active"
PLAN_COMPLETED = "completed"
PLAN_ABANDONED = "abandoned"
PLAN_STATUSES = (PLAN_ACTIVE, PLAN_COMPLETED, PLAN_ABANDONED)

# Allowed transitions: current status -> statuses it may move to.
# ``reset`` (back to pending) and the dependency gate on ``in_progress`` are
# handled separately in the store.
_TRANSITIONS: dict[str, frozenset[str]] = {
    TASK_PENDING: frozenset({TASK_IN_PROGRESS, TASK_DONE, TASK_BLOCKED, TASK_SKIPPED}),
    TASK_IN_PROGRESS: frozenset({TASK_DONE, TASK_BLOCKED, TASK_SKIPPED}),
    TASK_BLOCKED: frozenset({TASK_SKIPPED}),
    TASK_DONE: frozenset(),
    TASK_SKIPPED: frozenset(),
}

_CHECKPOINT_TIMEOUT = 30  # seconds; git on a local repo should be well under


class PlanError(Exception):
    """Raised for invalid plan operations (bad ids, transitions, deps)."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TestResult:
    """One recorded test run against a task."""

    command: str
    ok: bool
    summary: str = ""
    at: str = field(default_factory=now_utc)


@dataclass
class Task:
    id: str
    title: str
    status: str = TASK_PENDING
    depends_on: list[str] = field(default_factory=list)
    progress: int = 0
    notes: list[str] = field(default_factory=list)
    test: TestResult | None = None


@dataclass
class Phase:
    id: str
    title: str
    tasks: list[Task] = field(default_factory=list)


@dataclass
class Checkpoint:
    """A git commit created before self-modifying work, for safe reverts."""

    commit: str
    message: str
    at: str = field(default_factory=now_utc)


@dataclass
class Plan:
    goal: str
    version: int = PLAN_VERSION
    revision: int = 0
    status: str = PLAN_ACTIVE
    created: str = field(default_factory=now_utc)
    updated: str = field(default_factory=now_utc)
    notes: list[str] = field(default_factory=list)
    phases: list[Phase] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Structure building & validation
# ---------------------------------------------------------------------------


def build_phases(phases_in: list[Any]) -> list[Phase]:
    """Build the phase/task tree from tool input, validating structure.

    Task ids default to ``t1``, ``t2`` ... in document order (unique across
    the whole plan); explicit ids are accepted. Dependencies must name known
    tasks and must not form a cycle.
    """
    if not isinstance(phases_in, list) or not phases_in:
        raise PlanError("phases must be a non-empty list")
    phases: list[Phase] = []
    used: set[str] = set()
    auto = 1
    for i, pin in enumerate(phases_in, 1):
        if not isinstance(pin, dict):
            raise PlanError(f"phase {i} must be an object")
        title = str(pin.get("title") or "").strip()
        if not title:
            raise PlanError(f"phase {i} is missing a title")
        tasks_in = pin.get("tasks")
        if not isinstance(tasks_in, list) or not tasks_in:
            raise PlanError(f"phase {i} ({title!r}) needs at least one task")
        tasks: list[Task] = []
        for t in tasks_in:
            if not isinstance(t, dict):
                raise PlanError(f"a task in phase {title!r} must be an object")
            ttitle = str(t.get("title") or "").strip()
            if not ttitle:
                raise PlanError(f"a task in phase {title!r} is missing a title")
            tid = str(t.get("id") or "").strip()
            if not tid:
                while f"t{auto}" in used:
                    auto += 1
                tid = f"t{auto}"
                auto += 1
            if tid in used:
                raise PlanError(f"duplicate task id: {tid}")
            used.add(tid)
            deps = t.get("depends_on") or []
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                raise PlanError(f"task {tid}: depends_on must be a list of task ids")
            if tid in deps:
                raise PlanError(f"task {tid} cannot depend on itself")
            tasks.append(Task(id=tid, title=ttitle, depends_on=list(deps)))
        phases.append(Phase(id=f"p{i}", title=title, tasks=tasks))
    all_ids = used
    for p in phases:
        for t in p.tasks:
            for d in t.depends_on:
                if d not in all_ids:
                    raise PlanError(f"task {t.id} depends on unknown task: {d}")
    _check_cycles(phases)
    return phases


def _check_cycles(phases: list[Phase]) -> None:
    """Raise PlanError if the dependency graph contains a cycle."""
    deps = {t.id: set(t.depends_on) for p in phases for t in p.tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in deps}

    def visit(tid: str) -> None:
        color[tid] = GRAY
        for d in deps[tid]:
            if color[d] == GRAY:
                raise PlanError(f"dependency cycle involving task: {d}")
            if color[d] == WHITE:
                visit(d)
        color[tid] = BLACK

    for tid in deps:
        if color[tid] == WHITE:
            visit(tid)


def task_by_id(plan: Plan, task_id: str) -> Task:
    for phase in plan.phases:
        for task in phase.tasks:
            if task.id == task_id:
                return task
    raise PlanError(f"unknown task id: {task_id}")


def phase_status(phase: Phase) -> str:
    """Derive a phase's status from its tasks (never stored, never stale)."""
    if not phase.tasks:
        return TASK_PENDING
    statuses = [t.status for t in phase.tasks]
    if all(s in (TASK_DONE, TASK_SKIPPED) for s in statuses):
        return TASK_SKIPPED if all(s == TASK_SKIPPED for s in statuses) else TASK_DONE
    if any(s == TASK_IN_PROGRESS for s in statuses):
        return TASK_IN_PROGRESS
    if any(s == TASK_BLOCKED for s in statuses):
        return TASK_BLOCKED
    return TASK_PENDING


def plan_progress(plan: Plan) -> tuple[int, int, int]:
    """Return (complete, total, percent) counting done and skipped as complete."""
    tasks = [t for p in plan.phases for t in p.tasks]
    total = len(tasks)
    done = sum(1 for t in tasks if t.status in (TASK_DONE, TASK_SKIPPED))
    pct = round(100 * done / total) if total else 0
    return done, total, pct


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def plan_to_dict(plan: Plan) -> dict:
    return asdict(plan)


def plan_from_dict(data: Any) -> Plan:
    """Parse and validate a plan.json payload; raises PlanError on problems."""
    if not isinstance(data, dict):
        raise PlanError("plan.json must contain a JSON object")
    if data.get("version", PLAN_VERSION) != PLAN_VERSION:
        raise PlanError(f"unsupported plan version: {data.get('version')}")
    goal = data.get("goal")
    if not isinstance(goal, str):
        raise PlanError("plan goal must be a string")
    status = data.get("status", PLAN_ACTIVE)
    if status not in PLAN_STATUSES:
        raise PlanError(f"invalid plan status: {status}")
    phases: list[Phase] = []
    for p in data.get("phases") or []:
        tasks: list[Task] = []
        for t in p.get("tasks") or []:
            tstatus = t.get("status", TASK_PENDING)
            if tstatus not in TASK_STATUSES:
                raise PlanError(f"invalid task status: {tstatus}")
            test: TestResult | None = None
            td = t.get("test")
            if isinstance(td, dict):
                test = TestResult(
                    command=str(td.get("command", "")),
                    ok=bool(td.get("ok")),
                    summary=str(td.get("summary", "")),
                    at=str(td.get("at", "")),
                )
            tasks.append(
                Task(
                    id=str(t.get("id", "")),
                    title=str(t.get("title", "")),
                    status=tstatus,
                    depends_on=list(t.get("depends_on") or []),
                    progress=int(t.get("progress") or 0),
                    notes=[str(n) for n in (t.get("notes") or [])],
                    test=test,
                )
            )
        phases.append(Phase(id=str(p.get("id", "")), title=str(p.get("title", "")), tasks=tasks))
    checkpoints = [
        Checkpoint(commit=str(c.get("commit", "")), message=str(c.get("message", "")), at=str(c.get("at", "")))
        for c in (data.get("checkpoints") or [])
    ]
    return Plan(
        goal=goal,
        version=PLAN_VERSION,
        revision=int(data.get("revision") or 0),
        status=status,
        created=str(data.get("created", "")),
        updated=str(data.get("updated", "")),
        notes=[str(n) for n in (data.get("notes") or [])],
        phases=phases,
        checkpoints=checkpoints,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_MD_MARKS = {
    TASK_PENDING: " ",
    TASK_IN_PROGRESS: "~",
    TASK_DONE: "x",
    TASK_BLOCKED: "b",
    TASK_SKIPPED: "-",
}


def next_eligible_tasks(plan: Plan) -> list[Task]:
    """Return pending tasks whose dependencies are all done or skipped."""
    all_tasks = {t.id: t for p in plan.phases for t in p.tasks}
    eligible = []
    for p in plan.phases:
        for t in p.tasks:
            if t.status != TASK_PENDING:
                continue
            if all(all_tasks[d].status in (TASK_DONE, TASK_SKIPPED) for d in t.depends_on):
                eligible.append(t)
    return eligible


def summarize_plan(plan: Plan) -> str:
    """Compact plan state for injection into the system prompt."""
    done, total, pct = plan_progress(plan)
    lines = [
        f"goal: {plan.goal}",
        f"status: {plan.status} | revision: {plan.revision} | progress: {done}/{total} ({pct}%)",
    ]
    # Current task (in_progress) — the authoritative "what to work on now" signal.
    in_progress = [t for p in plan.phases for t in p.tasks if t.status == TASK_IN_PROGRESS]
    if in_progress:
        t = in_progress[0]
        prog = f" ({t.progress}%)" if t.progress else ""
        lines.append(f"CURRENT: {t.id} {t.title} [{t.status}]{prog}")
    # Next eligible pending tasks (all deps done/skipped).
    eligible = next_eligible_tasks(plan)
    if eligible:
        lines.append("NEXT ELIGIBLE: " + "; ".join(f"{t.id} {t.title}" for t in eligible))
    # Phase details.
    for phase in plan.phases:
        tasks = "; ".join(f"{t.id} {t.title} [{t.status}]" for t in phase.tasks)
        lines.append(f"{phase.id} {phase.title} [{phase_status(phase)}]: {tasks}")
    return "\n".join(lines)


def render_plan(plan: Plan) -> str:
    """Full plan state as text (the plan tool's ``view`` action)."""
    done, total, pct = plan_progress(plan)
    lines = [
        f"plan: {plan.goal}",
        f"status: {plan.status} | revision: {plan.revision} | progress: {done}/{total} ({pct}%) | updated: {plan.updated}",
    ]
    for phase in plan.phases:
        lines.append(f"[{phase_status(phase)}] {phase.id} {phase.title}")
        for t in phase.tasks:
            bits = [f"{t.id} {t.title} [{t.status}]"]
            if t.progress and t.status in (TASK_PENDING, TASK_IN_PROGRESS):
                bits.append(f"{t.progress}%")
            if t.test:
                bits.append(f"test: {'ok' if t.test.ok else 'FAILED'} `{t.test.command}`")
            if t.depends_on:
                bits.append(f"deps: {', '.join(t.depends_on)}")
            if t.notes:
                bits.append(f"note: {t.notes[-1]}")
            lines.append("  - " + " | ".join(bits))
    if plan.notes:
        lines.append("notes:")
        lines.extend(f"  - {n}" for n in plan.notes[-10:])
    if plan.checkpoints:
        last = plan.checkpoints[-1]
        lines.append(f"checkpoints: {len(plan.checkpoints)} (latest {last.commit[:12]} {last.message})")
    return "\n".join(lines)


def render_plan_md(plan: Plan) -> str:
    """Human-readable Markdown view (written to plan.md)."""
    done, total, pct = plan_progress(plan)
    lines = [
        f"# Plan: {plan.goal}",
        "",
        f"- **status**: {plan.status}",
        f"- **revision**: {plan.revision}",
        f"- **progress**: {done}/{total} tasks ({pct}%)",
        f"- **created**: {plan.created}",
        f"- **updated**: {plan.updated}",
        "",
    ]
    for phase in plan.phases:
        lines.append(f"## {phase.title} ({phase.id}) — {phase_status(phase)}")
        lines.append("")
        for t in phase.tasks:
            mark = _MD_MARKS[t.status]
            extra = []
            if t.progress and t.status in (TASK_PENDING, TASK_IN_PROGRESS):
                extra.append(f"{t.progress}%")
            if t.test:
                extra.append(f"test: `{t.test.command}` → {'ok' if t.test.ok else 'FAILED'} ({t.test.at})")
            suffix = f" — {', '.join(extra)}" if extra else ""
            deps = ", ".join(t.depends_on) or "—"
            lines.append(f"- [{mark}] **{t.id}** {t.title}{suffix} (deps: {deps})")
            lines.extend(f"  - {n}" for n in t.notes)
        lines.append("")
    if plan.notes:
        lines.append("## Notes")
        lines.append("")
        lines.extend(f"- {n}" for n in plan.notes)
        lines.append("")
    if plan.checkpoints:
        lines.append("## Checkpoints")
        lines.append("")
        lines.extend(f"- `{c.commit[:12]}` {c.message} ({c.at})" for c in plan.checkpoints)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git checkpoints
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=_CHECKPOINT_TIMEOUT,
    )


def git_checkpoint(root: Path, message: str) -> str:
    """Commit the working tree as a gremlin checkpoint; return the commit hash.

    On a clean tree the current HEAD is recorded without creating a commit.
    Raises PlanError when the project root is not a usable git repository.
    """
    r = _git(root, "rev-parse", "--is-inside-work-tree")
    if r.returncode != 0:
        raise PlanError("checkpoint needs a git repository at the project root")
    r = _git(root, "add", "-A")
    if r.returncode != 0:
        raise PlanError(f"git add failed: {r.stderr.strip()[:200]}")
    r = _git(root, "status", "--porcelain")
    if r.stdout.strip():
        r = _git(root, "commit", "-m", f"gremlin checkpoint: {message}")
        if r.returncode != 0:
            raise PlanError(f"git commit failed: {r.stderr.strip()[:200]}")
    r = _git(root, "rev-parse", "HEAD")
    if r.returncode != 0:
        raise PlanError("git repository has no commits yet; commit something first")
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PlanStore:
    """Load, validate, mutate and persist the plan at ``path`` (plan.json)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.md_path = self.path.with_name(self.path.stem + ".md")

    # -- persistence -------------------------------------------------------

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Plan | None:
        """Return the stored plan, or None when there is none.

        Raises PlanError when the file exists but is corrupt or invalid, so a
        bad plan.json is surfaced instead of silently discarded.
        """
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise PlanError(f"plan.json is unreadable ({e}); recreate it with action=create") from e
        try:
            return plan_from_dict(data)
        except PlanError as e:
            raise PlanError(f"plan.json is invalid: {e}") from e

    def save(self, plan: Plan) -> None:
        plan.updated = now_utc()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, plan_to_dict(plan))
        log.info("plan saved: %s (%s, rev %d)", self.path, plan.status, plan.revision)

    def require(self) -> Plan:
        plan = self.load()
        if plan is None:
            raise PlanError("no plan found; create one first with action=create")
        return plan

    def summarize(self) -> str | None:
        """Compact plan state for the system prompt; None when unusable."""
        if not self.path.exists():
            return None
        try:
            plan = self.load()
        except PlanError as e:
            log.warning("plan.json unusable for prompt injection: %s", e)
            return None
        return summarize_plan(plan) if plan is not None else None

    # -- lifecycle ----------------------------------------------------------

    def create(self, goal: str, phases_in: list[Any], notes: list[str] | None = None) -> Plan:
        """Create a new plan. An existing ACTIVE plan blocks creation."""
        existing = self.load()
        if existing is not None and existing.status == PLAN_ACTIVE:
            raise PlanError("an active plan already exists; revise it (action=revise) or abandon it first")
        goal = (goal or "").strip()
        if not goal:
            raise PlanError("goal must be a non-empty string")
        plan = Plan(goal=goal, notes=[str(n) for n in (notes or [])], phases=build_phases(phases_in))
        self.save(plan)
        return plan

    def revise(self, reason: str, goal: str | None = None, phases_in: list[Any] | None = None) -> Plan:
        """Revise the plan after results invalidate it; bumps the revision.

        Checkpoints survive; structure (phases/tasks) is replaced when given.
        A completed or abandoned plan is revived to active.
        """
        plan = self.require()
        reason = (reason or "").strip()
        if not reason:
            raise PlanError("revise requires a reason")
        if goal is not None:
            g = goal.strip()
            if not g:
                raise PlanError("goal must be a non-empty string")
            plan.goal = g
        if phases_in is not None:
            plan.phases = build_phases(phases_in)
        plan.revision += 1
        plan.status = PLAN_ACTIVE
        plan.notes.append(f"revision {plan.revision}: {reason}")
        self.save(plan)
        return plan

    def finish(self) -> Plan:
        plan = self.require()
        if plan.status != PLAN_ACTIVE:
            raise PlanError(f"plan is already {plan.status}")
        done, total, _ = plan_progress(plan)
        if done != total:
            raise PlanError(f"cannot finish: {total - done} task(s) still not done or skipped")
        plan.status = PLAN_COMPLETED
        self.save(plan)
        return plan

    def abandon(self, reason: str) -> Plan:
        plan = self.require()
        if plan.status != PLAN_ACTIVE:
            raise PlanError(f"plan is already {plan.status}")
        reason = (reason or "").strip()
        if not reason:
            raise PlanError("abandon requires a reason")
        plan.status = PLAN_ABANDONED
        plan.notes.append(f"abandoned: {reason}")
        self.save(plan)
        return plan

    def checkpoint(self, message: str) -> tuple[Plan, str]:
        """Create a git checkpoint at the project root and record it in the plan."""
        plan = self.require()
        message = (message or "").strip()
        if not message:
            raise PlanError("checkpoint requires a message")
        commit = git_checkpoint(self.path.parent, message)
        plan.checkpoints.append(Checkpoint(commit=commit, message=message))
        self.save(plan)
        return plan, commit

    def render(self) -> str:
        """Write plan.md from the current plan; return the path."""
        plan = self.require()
        self.md_path.write_text(render_plan_md(plan), encoding="utf-8")
        return str(self.md_path)

    # -- task mutations -------------------------------------------------------

    def _mutate(self, apply: Callable[[Plan], None]) -> Plan:
        plan = self.require()
        if plan.status != PLAN_ACTIVE:
            raise PlanError(f"plan is {plan.status}; tasks can only be modified while active")
        apply(plan)
        self.save(plan)
        return plan

    def _transition(self, task_id: str, target: str, note: str | None = None) -> Plan:
        def apply(plan: Plan) -> None:
            task = task_by_id(plan, task_id)
            if target not in _TRANSITIONS[task.status]:
                raise PlanError(f"cannot move task {task_id} from {task.status} to {target}")
            if target == TASK_IN_PROGRESS:
                # At most one in_progress task at a time.
                for other in (t for p in plan.phases for t in p.tasks):
                    if other.id != task_id and other.status == TASK_IN_PROGRESS:
                        raise PlanError(
                            f"cannot start {task_id}: task {other.id} is already in_progress; "
                            "complete, skip, or reset it first"
                        )
                for d in task.depends_on:
                    dep = task_by_id(plan, d)
                    if dep.status not in (TASK_DONE, TASK_SKIPPED):
                        raise PlanError(
                            f"task {task_id} cannot start: dependency {d} is {dep.status} "
                            "(finish or skip it first, or revise the plan)"
                        )
            task.status = target
            if target == TASK_DONE:
                task.progress = 100
            elif target == TASK_PENDING:
                task.progress = 0
            if note:
                task.notes.append(note)

        return self._mutate(apply)

    def start(self, task_id: str, note: str | None = None) -> Plan:
        return self._transition(task_id, TASK_IN_PROGRESS, note)

    def complete(self, task_id: str, note: str | None = None) -> Plan:
        return self._transition(task_id, TASK_DONE, note)

    def block(self, task_id: str, note: str) -> Plan:
        if not (note or "").strip():
            raise PlanError("block requires a note explaining what is blocking the task")
        return self._transition(task_id, TASK_BLOCKED, note.strip())

    def skip(self, task_id: str, note: str | None = None) -> Plan:
        return self._transition(task_id, TASK_SKIPPED, note)

    def reset(self, task_id: str) -> Plan:
        def apply(plan: Plan) -> None:
            task = task_by_id(plan, task_id)
            if task.status == TASK_PENDING:
                raise PlanError(f"task {task_id} is already pending")
            task.status = TASK_PENDING
            task.progress = 0

        return self._mutate(apply)

    def set_progress(self, task_id: str, pct: int, note: str | None = None) -> Plan:
        def apply(plan: Plan) -> None:
            task = task_by_id(plan, task_id)
            if task.status not in (TASK_PENDING, TASK_IN_PROGRESS):
                raise PlanError(f"cannot set progress on task {task_id} ({task.status})")
            task.progress = max(0, min(100, int(pct)))
            if note:
                task.notes.append(note)

        return self._mutate(apply)

    def record_test(self, task_id: str, command: str, ok: bool, summary: str = "") -> Plan:
        def apply(plan: Plan) -> None:
            task = task_by_id(plan, task_id)
            if task.status == TASK_SKIPPED:
                raise PlanError(f"task {task_id} is skipped; record tests only for live tasks")
            task.test = TestResult(command=(command or "").strip(), ok=bool(ok), summary=(summary or "").strip())

        return self._mutate(apply)
